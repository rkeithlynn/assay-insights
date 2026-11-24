"""Network, database and migration Lambda components, using Pulumi mocks (no AWS calls)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pulumi
import pytest

from assay_infra.config import (
    DatabaseConfig,
    NetworkConfig,
    validate_database,
    validate_network,
)
from assay_infra.database import Database
from assay_infra.images import WORKSPACE_MANIFESTS
from assay_infra.migrations import MigrationRunner, image_tag, parse_digest
from assay_infra.network import INTERFACE_ENDPOINT_SERVICES, Network, plan_subnets
from tests.unit.conftest import ACCOUNT_ID, REGION, registered

REPO_ROOT = Path(__file__).resolve().parents[2]
NET = NetworkConfig(vpc_cidr="10.40.0.0/16", workload_az_id="use1-az2", second_az_id="use1-az4")
DB = DatabaseConfig(
    engine_version="17.11", database_name="assay", min_acu=0, max_acu=4, auto_pause_seconds=300
)


def _stack(prefix: str) -> tuple[Network, Database, MigrationRunner]:
    network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
    database = Database(f"{prefix}-db", cfg=DB, network=network, account_id=ACCOUNT_ID)
    runner = MigrationRunner(
        f"{prefix}-db-migrate",
        repo_root=REPO_ROOT,
        region=REGION,
        account_id=ACCOUNT_ID,
        network=network,
        database=database,
        database_name=DB.database_name,
    )
    return network, database, runner


def _after(*outputs: pulumi.Output[Any]) -> pulumi.Output[list[Any]]:
    """Resolve once every listed resource is registered with the mocks."""
    return pulumi.Output.all(*outputs)


# --- Config ---------------------------------------------------------------------------


def test_subnet_plan() -> None:
    plan = plan_subnets("10.40.0.0/16")
    assert plan.app == ("10.40.0.0/22", "10.40.4.0/22")
    assert plan.data == ("10.40.16.0/24", "10.40.17.0/24")


@pytest.mark.parametrize(
    ("cidr", "message"),
    [("8.8.0.0/16", "private"), ("10.40.0.0/20", "/18 or larger")],
)
def test_network_config_rejects(cidr: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_network(NetworkConfig(cidr, "a", "b"))


def test_network_config_needs_two_zones() -> None:
    with pytest.raises(ValueError, match="must differ"):
        validate_network(NetworkConfig("10.40.0.0/16", "a", "a"))


def test_database_config_rejects_short_auto_pause() -> None:
    with pytest.raises(ValueError, match="dbAutoPauseSeconds"):
        validate_database(DatabaseConfig("17.11", "assay", 0, 4, 60))


# --- Network --------------------------------------------------------------------------


@pulumi.runtime.test
def test_no_public_subnets_and_no_internet_routes() -> Any:
    network, _, _ = _stack("n1")
    subnets = network.app_subnets + network.data_subnets

    def check(_: list[Any]) -> None:
        for _name, inputs in registered("aws:ec2/subnet:Subnet", "n1-"):
            assert inputs["mapPublicIpOnLaunch"] is False
        subnets = registered("aws:ec2/subnet:Subnet", "n1-")
        zones = sorted(i["availabilityZoneId"] for _, i in subnets)
        assert zones == ["use1-az2", "use1-az2", "use1-az4", "use1-az4"]
        # Only the implicit local route: no inline routes, no Route resources, no NAT.
        for _name, inputs in registered("aws:ec2/routeTable:RouteTable", "n1-"):
            assert not inputs.get("routes")
        assert registered("aws:ec2/route:Route", "n1-") == []
        assert registered("aws:ec2/natGateway:NatGateway", "n1-") == []
        assert registered("aws:ec2/eip:Eip", "n1-") == []
        # The internet gateway is attached (for CloudFront VPC origins) and nothing else.
        assert len(registered("aws:ec2/internetGateway:InternetGateway", "n1-")) == 1

    return _after(
        *[s.id for s in subnets],
        network.app_route_table.id,
        network.data_route_table.id,
        network.internet_gateway.id,
    ).apply(check)


@pulumi.runtime.test
def test_interface_endpoints_exactly_as_specified_in_workload_zone() -> Any:
    network, _, _ = _stack("n2")

    def check(args: list[Any]) -> None:
        workload_subnet_id = args[0]
        endpoints = {
            i["serviceName"]: i
            for _, i in registered("aws:ec2/vpcEndpoint:VpcEndpoint", "n2-")
            if i["vpcEndpointType"] == "Interface"
        }
        expected = {f"com.amazonaws.{REGION}.{s}" for s in INTERFACE_ENDPOINT_SERVICES}
        assert set(endpoints) == expected
        for inputs in endpoints.values():
            assert inputs["subnetIds"] == [workload_subnet_id]
            assert inputs["privateDnsEnabled"] is True

    return _after(
        network.workload_subnet.id, *[e.id for e in network.interface_endpoints.values()]
    ).apply(check)


def test_endpoint_list_matches_spec() -> None:
    assert set(INTERFACE_ENDPOINT_SERVICES) == {
        "bedrock-runtime",
        "textract",
        "kms",
        "sts",
        "s3vectors",
        "rekognition",
        "ecr.api",
        "ecr.dkr",
        "logs",
        "secretsmanager",
        "sqs",
        # Added when a call first needed them (build step 4).
        "execute-api",
        "lambda",
        "guardduty",
        # Build step 5: the API and the seed Lambda start provisioning executions.
        "states",
    }


@pulumi.runtime.test
def test_s3_gateway_endpoint_limited_to_own_account() -> Any:
    network, _, _ = _stack("n3")

    def check(args: list[Any]) -> None:
        policy = json.loads(args[0])
        own = policy["Statement"][0]
        assert own["Condition"] == {"StringEquals": {"aws:ResourceAccount": ACCOUNT_ID}}
        others = policy["Statement"][1:]
        assert [s["Action"] for s in others] == ["s3:GetObject"]
        assert others[0]["Resource"] == f"arn:aws:s3:::prod-{REGION}-starport-layer-bucket/*"

    return network.s3_gateway_endpoint.policy.apply(lambda p: check([p]))


@pulumi.runtime.test
def test_default_security_group_has_no_rules_and_db_only_from_workloads() -> Any:
    network, _, _ = _stack("n4")

    def check(_: list[Any]) -> None:
        [(_sg_name, default_sg)] = registered(
            "aws:ec2/defaultSecurityGroup:DefaultSecurityGroup", "n4-"
        )
        assert default_sg.get("ingress", []) == []
        assert default_sg.get("egress", []) == []
        db_in = registered("aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule", "n4-db-")
        assert len(db_in) == 1
        rule = db_in[0][1]
        assert (rule["fromPort"], rule["toPort"]) == (5432, 5432)
        assert rule["referencedSecurityGroupId"] == "n4-workload-sg-id"
        assert "cidrIpv4" not in rule
        # Workloads may reach only the endpoints, S3 and the database.
        egress = registered("aws:vpc/securityGroupEgressRule:SecurityGroupEgressRule", "n4-")
        assert sorted(r["toPort"] for _, r in egress) == [443, 443, 5432]
        assert all("cidrIpv4" not in r for _, r in egress)

    return _after(
        network.default_security_group.id, *[r.id for r in network.security_group_rules]
    ).apply(check)


@pulumi.runtime.test
def test_flow_logs_capture_all_traffic_for_30_days() -> Any:
    network, _, _ = _stack("n5")

    def check(args: list[Any]) -> None:
        traffic, retention = args
        assert traffic == "ALL"
        assert retention == 30

    return _after(network.flow_log.traffic_type, network.flow_log_group.retention_in_days).apply(
        check
    )


# --- Database -------------------------------------------------------------------------


@pulumi.runtime.test
def test_cluster_is_encrypted_private_serverless_and_protected() -> Any:
    _, database, _ = _stack("d1")
    c = database.cluster

    def check(args: list[Any]) -> None:
        (encrypted, kms, key_arn, iam_auth, scaling, deletion, managed_pw, secret_key, version) = (
            args
        )
        assert encrypted is True
        assert kms == key_arn
        assert secret_key == key_arn
        assert iam_auth is True
        assert scaling["min_capacity"] == 0
        assert scaling["max_capacity"] == 4
        assert scaling["seconds_until_auto_pause"] == 300
        assert deletion is True
        assert managed_pw is True
        assert version == "17.11"

    return _after(
        c.storage_encrypted,
        c.kms_key_id,
        database.key.arn,
        c.iam_database_authentication_enabled,
        c.serverlessv2_scaling_configuration.apply(
            lambda s: {
                "min_capacity": s.min_capacity if s else None,
                "max_capacity": s.max_capacity if s else None,
                "seconds_until_auto_pause": s.seconds_until_auto_pause if s else None,
            }
        ),
        c.deletion_protection,
        c.manage_master_user_password,
        c.master_user_secret_kms_key_id,
        c.engine_version,
    ).apply(check)


@pulumi.runtime.test
def test_writer_is_private_in_workload_zone_and_tls_is_forced() -> Any:
    _, database, _ = _stack("d2")

    def check(args: list[Any]) -> None:
        public, zone, instance_class = args[:3]
        assert public is False
        assert zone == "zone-of-use1-az2"
        assert instance_class == "db.serverless"
        [(_, params)] = registered("aws:rds/clusterParameterGroup:ClusterParameterGroup", "d2-")
        assert {"name": "rds.force_ssl", "value": "1", "applyMethod": "pending-reboot"} in params[
            "parameters"
        ]
        assert params["family"] == "aurora-postgresql17"
        [(_, key)] = registered("aws:kms/key:Key", "d2-")
        assert key["enableKeyRotation"] is True

    return _after(
        database.writer.publicly_accessible,
        database.writer.availability_zone,
        database.writer.instance_class,
        database.parameter_group.id,
        database.key.id,
    ).apply(check)


# --- Migration Lambda -----------------------------------------------------------------


@pulumi.runtime.test
def test_migration_lambda_runs_x86_in_workload_subnet_from_pinned_image() -> Any:
    network, _, runner = _stack("m1")
    fn = runner.function

    def check(args: list[Any]) -> None:
        arch, package, uri, vpc, env, subnet_id, sg_id = args[:7]
        assert arch == ["x86_64"]
        assert package == "Image"
        assert uri.endswith("@sha256:" + "0" * 64)
        assert vpc["subnet_ids"] == [subnet_id]
        assert vpc["security_group_ids"] == [sg_id]
        assert set(env) == {"DB_HOST", "DB_PORT", "DB_NAME", "DB_MASTER_SECRET_ARN"}
        [(_cmd, build)] = registered("command:local:Command", "m1-")
        assert build["create"] == "scripts/build_image.py"
        assert build["environment"]["IMAGE_DOCKERFILE"] == "db/Dockerfile"

    return _after(
        fn.architectures,
        fn.package_type,
        fn.image_uri,
        fn.vpc_config.apply(
            lambda v: {
                "subnet_ids": v.subnet_ids if v else None,
                "security_group_ids": v.security_group_ids if v else None,
            }
        ),
        fn.environment.apply(lambda e: dict(e.variables or {}) if e else {}),
        network.workload_subnet.id,
        network.workload_security_group.id,
        runner.image_digest,
    ).apply(check)


@pulumi.runtime.test
def test_migration_role_connects_only_as_migrator() -> Any:
    _, _, runner = _stack("m2")

    def check(_: list[Any]) -> None:
        [(_name, policy)] = registered("aws:iam/rolePolicy:RolePolicy", "m2-db-migrate-role-policy")
        statements = json.loads(policy["policy"])["Statement"]
        connect = [s for s in statements if s["Action"] == "rds-db:connect"]
        assert [s["Resource"] for s in connect] == [
            f"arn:aws:rds-db:{REGION}:{ACCOUNT_ID}:dbuser:cluster-MOCKRESOURCEID/migrator"
        ]
        star = [s for s in statements if s["Resource"] == "*"]
        assert all(str(a).startswith("ec2:") for s in star for a in s["Action"])

    return _after(runner.function.id).apply(check)


def test_image_tag_changes_with_source(tmp_path: Path) -> None:
    for rel in WORKSPACE_MANIFESTS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    (tmp_path / "db/Dockerfile").write_text("FROM scratch")
    (tmp_path / "db/assay_db").mkdir()
    (tmp_path / "db/assay_db/a.py").write_text("one")
    first = image_tag(tmp_path)
    assert first == image_tag(tmp_path)
    (tmp_path / "db/assay_db/a.py").write_text("two")
    assert image_tag(tmp_path) != first


def test_parse_digest_takes_the_last_line_only() -> None:
    digest = "sha256:" + "a" * 64
    assert parse_digest(f"noise\n{digest}\n") == digest
    with pytest.raises(ValueError, match="no digest"):
        parse_digest("Login Succeeded\n")
