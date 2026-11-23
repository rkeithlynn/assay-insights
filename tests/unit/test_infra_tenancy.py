"""The TSS, provisioning and isolation-runner components, using Pulumi mocks (no AWS calls)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pulumi
import pytest

from assay_infra import alerts, isolation, provisioning, tss
from assay_infra.alerts import Alerts
from assay_infra.auth import Auth
from assay_infra.config import (
    AlertsConfig,
    TenancyConfig,
    validate_alerts,
    validate_tenancy,
)
from assay_infra.database import Database
from assay_infra.isolation import IsolationRunner, runner_role
from assay_infra.network import Network
from assay_infra.provisioning import Provisioning
from assay_infra.tss import TenantStorage
from assay_tss import policies
from tests.unit.conftest import ACCOUNT_ID, REGION, registered
from tests.unit.test_infra_auth_api import AUTH, DNS, FAKE_JWKS
from tests.unit.test_infra_data import DB, NET

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = "assay-dev"
BREAK_GLASS_PRINCIPAL = f"arn:aws:iam::{ACCOUNT_ID}:role/aws-reserved/sso.amazonaws.com/Admin"
TAGS = {"app": "assay-insights", "env": "dev"}
ROLE = f"arn:aws:iam::{ACCOUNT_ID}:role"
ALARM_EMAIL = "alarms@example.test"


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def by_sid(policy: str | dict[str, Any], sid: str) -> dict[str, Any]:
    document = json.loads(policy) if isinstance(policy, str) else policy
    found: list[dict[str, Any]] = [s for s in document["Statement"] if s.get("Sid") == sid]
    [statement] = found
    return statement


def _one(type_token: str, name: str) -> dict[str, Any]:
    matches = [inputs for n, inputs in registered(type_token) if n == name]
    assert len(matches) == 1, f"{type_token} {name}: {len(matches)} registered"
    return matches[0]


class Stack:
    def __init__(self, prefix: str, *, with_runner: bool = True) -> None:
        self.prefix = prefix
        # Resources named after the platform prefix (the tenant role, the break-glass role,
        # the scan role, the Cognito admin Lambda) must be unique to each test's stack.
        self.name_prefix = f"assay-{prefix}"
        self.network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
        self.database = Database(
            f"{prefix}-db", cfg=DB, network=self.network, account_id=ACCOUNT_ID
        )
        self.auth = Auth(
            f"{prefix}-auth",
            cfg=AUTH,
            dns=DNS,
            region=REGION,
            account_id=ACCOUNT_ID,
            repo_root=REPO_ROOT,
            jwks_fetcher=lambda region, pool_id: FAKE_JWKS,
        )
        self.alerts = Alerts(
            f"{prefix}-alarms", email=ALARM_EMAIL, region=REGION, account_id=ACCOUNT_ID
        )
        callers = {}
        if with_runner:
            callers[isolation.SERVICE_NAME] = runner_role(f"{prefix}-isolation", ACCOUNT_ID)
        self.storage = TenantStorage(
            f"{prefix}-tss",
            repo_root=REPO_ROOT,
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=self.name_prefix,
            network=self.network,
            database=self.database,
            database_name=DB.database_name,
            break_glass_principal_arn=BREAK_GLASS_PRINCIPAL,
            alarm_topic_arn=self.alerts.topic.arn,
            callers=callers,
        )
        self.provisioning = Provisioning(
            f"{prefix}-provision",
            repo_root=REPO_ROOT,
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=self.name_prefix,
            network=self.network,
            database=self.database,
            database_name=DB.database_name,
            auth=self.auth,
            storage=self.storage,
            vector_dimensions=1024,
            default_tags=TAGS,
            alarm_topic_arn=self.alerts.topic.arn,
        )
        self.runner: IsolationRunner | None = None
        if with_runner:
            self.runner = IsolationRunner(
                f"{prefix}-isolation",
                role=callers[isolation.SERVICE_NAME],
                repo_root=REPO_ROOT,
                region=REGION,
                account_id=ACCOUNT_ID,
                name_prefix=self.name_prefix,
                network=self.network,
                database=self.database,
                database_name=DB.database_name,
                tss_endpoint_url=self.storage.endpoint_url,
                tenant_role_arn=self.storage.tenant_role.arn,
                invite_function_arn=self.provisioning.invite_function.arn,
            )

    def ready(self) -> pulumi.Output[list[Any]]:
        """Resolves once everything the checks read is registered with the mocks."""
        outputs: list[pulumi.Output[Any]] = [
            self.storage.stage.id,
            self.storage.function.id,
            self.storage.endpoint_url,
            self.storage.break_glass.alarm.id,
            self.provisioning.state_machine.id,
            self.provisioning.state_machine.definition,
            self.provisioning.task_definition.container_definitions,
            self.provisioning.task_role_policy.policy,
            self.provisioning.failure_alarm.id,
            self.provisioning.invite_function.id,
        ]
        if self.runner is not None:
            outputs.append(self.runner.function.id)
        return pulumi.Output.all(*outputs)

    def topic_arn(self) -> str:
        """The alarm topic's ARN, as the mocks build it."""
        return f"arn:aws:mock:{REGION}:{ACCOUNT_ID}:{self.prefix}-alarms-topic"

    def role_policies(self) -> dict[str, dict[str, Any]]:
        """Every inline role policy in this stack: resource name -> document."""
        return {
            name: json.loads(inputs["policy"])
            for name, inputs in registered("aws:iam/rolePolicy:RolePolicy")
            if name.startswith((self.prefix, self.name_prefix))
        }


# --- Config -----------------------------------------------------------------------------------


def tenancy(**changes: Any) -> TenancyConfig:
    values: dict[str, Any] = {
        "break_glass_principal_arn": BREAK_GLASS_PRINCIPAL,
        "vector_dimensions": 1024,
        "isolation_tests": False,
        "default_tags": TAGS,
    }
    return TenancyConfig(**(values | changes))


def test_tenancy_config_accepts_the_dev_values() -> None:
    assert validate_tenancy(tenancy()).vector_dimensions == 1024


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"break_glass_principal_arn": "arn:aws:iam::123:user/someone"}, "IAM role ARN"),
        ({"break_glass_principal_arn": "*"}, "IAM role ARN"),
        ({"vector_dimensions": 1536}, "vectorDimensions"),
        ({"default_tags": {}}, "defaultTags"),
    ],
)
def test_tenancy_config_rejects(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_tenancy(tenancy(**changes))


# --- The provisioning role: scoped, no object access, no decrypt on tenant keys ---------------


def provisioning_policy() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        provisioning.provisioning_role_policy(
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=PREFIX,
            platform_tag="app",
            state_bucket="arn:aws:s3:::state-bucket",
            state_key=f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/state",
            scan_role=f"{ROLE}/scan",
            invite_function=f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:invite",
            db_user=f"arn:aws:rds-db:{REGION}:{ACCOUNT_ID}:dbuser:cluster/platform_writer",
        )
    )
    return document


# The only statements without a resource to scope to, and why each is safe.
UNSCOPED = {
    "CreateTenantKeys": "conditions on the request and resource tags",
    "ListForTheProvider": "list actions only",
    "CreateMalwareProtectionPlans": "a plan has no ARN before it exists",
}


def test_provisioning_role_is_scoped_to_tenant_resources_and_the_state_bucket() -> None:
    policy = provisioning_policy()
    for statement in policy["Statement"]:
        if statement["Effect"] != "Allow":
            continue
        resources = as_list(statement["Resource"])
        if "*" in resources:
            assert statement["Sid"] in UNSCOPED, statement["Sid"]
            continue
        for resource in resources:
            scoped = (
                "assay-dev-t-*" in resource
                or "state" in resource
                or resource.endswith(("role/scan", "function:invite", "/platform_writer"))
                or ":malware-protection-plan/" in resource
            )
            assert scoped, f"{statement['Sid']}: {resource}"
    listing = by_sid(policy, "ListForTheProvider")
    assert all(a.split(":")[1].startswith("List") for a in as_list(listing["Action"]))


def test_provisioning_role_has_no_object_access_to_tenant_buckets() -> None:
    policy = provisioning_policy()
    bucket = "arn:aws:s3:::assay-dev-t-*"
    settings = by_sid(policy, "TenantBucketSettings")
    assert settings["Resource"] == bucket  # the bucket itself, never bucket/*
    for action in as_list(settings["Action"]):
        assert "Object" not in action.removeprefix("s3:GetBucketObjectLock")
        assert "Object" not in action or action.startswith("s3:PutBucket")
    allowed_on_objects = [
        s["Sid"]
        for s in policy["Statement"]
        if s["Effect"] == "Allow"
        and any(r.startswith(f"{bucket}/") for r in as_list(s["Resource"]))
    ]
    assert allowed_on_objects == []
    deny = by_sid(policy, "NeverTenantObjects")
    assert deny["Effect"] == "Deny"
    assert deny["Resource"] == [bucket, f"{bucket}/*"]
    assert set(deny["Action"]) == set(policies.OBJECT_DATA_ACTIONS)
    assert {"s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"} <= set(deny["Action"])


def test_provisioning_role_cannot_decrypt_with_a_tenant_key() -> None:
    policy = provisioning_policy()
    crypto = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"}
    users = [
        s["Sid"]
        for s in policy["Statement"]
        if s["Effect"] == "Allow" and crypto & set(as_list(s["Action"]))
    ]
    assert users == ["StateKey"]
    assert by_sid(policy, "StateKey")["Resource"].endswith("key/state")
    deny = by_sid(policy, "NeverDecryptWithATenantKey")
    assert deny["Effect"] == "Deny"
    assert "kms:Decrypt" in deny["Action"]
    assert deny["Condition"] == {"Null": {"aws:ResourceTag/tenant_id": "false"}}


def test_provisioning_role_tags_only_new_tenant_keys() -> None:
    """It can create a key tagged with a tenant ID, and cannot tag a platform key."""
    create = by_sid(provisioning_policy(), "CreateTenantKeys")
    assert set(create["Action"]) == {"kms:CreateKey", "kms:TagResource"}
    assert create["Condition"] == {
        "Null": {"aws:RequestTag/tenant_id": "false", "aws:ResourceTag/app": "true"}
    }


def test_provisioning_role_passes_only_the_scan_role_and_only_to_guardduty() -> None:
    statement = by_sid(provisioning_policy(), "PassTheScanRoleToGuardDuty")
    assert statement["Resource"] == f"{ROLE}/scan"
    assert statement["Condition"] == {
        "StringEquals": {"iam:PassedToService": "malware-protection-plan.guardduty.amazonaws.com"}
    }
    iam_actions = {
        a
        for s in provisioning_policy()["Statement"]
        for a in as_list(s["Action"])
        if a.startswith("iam:")
    }
    assert iam_actions == {"iam:PassRole"}


# --- The scan role, the state bucket and the state key ----------------------------------------


def test_scan_role_trust_requires_this_account_as_source() -> None:
    trust = json.loads(provisioning.scan_role_trust_policy(ACCOUNT_ID))
    [statement] = trust["Statement"]
    assert statement["Principal"] == {"Service": "malware-protection-plan.guardduty.amazonaws.com"}
    assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}}


def test_scan_role_reads_and_tags_tenant_buckets_only() -> None:
    policy = json.loads(
        provisioning.scan_role_policy(region=REGION, account_id=ACCOUNT_ID, name_prefix=PREFIX)
    )
    for statement in policy["Statement"]:
        for resource in as_list(statement["Resource"]):
            assert (
                "assay-dev-t-*" in resource
                or ":rule/DO-NOT-DELETE-" in resource
                or ":key/" in resource
            )
    actions = {a for s in policy["Statement"] for a in as_list(s["Action"])}
    assert not actions & {"s3:DeleteObject", "s3:PutBucketPolicy", "kms:TagResource"}
    put = by_sid(policy, "AllowPutValidationObject")
    assert put["Resource"].endswith("/malware-protection-resource-validation-object")
    key = by_sid(policy, "AllowDecryptForMalwareScan")
    assert key["Condition"]["Null"] == {"aws:ResourceTag/tenant_id": "false"}


def test_state_objects_are_readable_only_by_the_provisioning_role() -> None:
    policy = json.loads(
        provisioning.state_bucket_policy(
            bucket="arn:aws:s3:::state-bucket", provisioning_role=f"{ROLE}/provisioning"
        )
    )
    deny = by_sid(policy, "OnlyProvisioningReadsOrWritesState")
    assert deny["Effect"] == "Deny" and deny["Principal"] == "*"
    assert {"s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"} <= set(deny["Action"])
    assert deny["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": f"{ROLE}/provisioning"}}
    assert by_sid(policy, "DenyInsecureTransport")["Effect"] == "Deny"


def test_state_key_is_used_only_by_the_provisioning_role() -> None:
    policy = json.loads(
        provisioning.state_key_policy(
            account_id=ACCOUNT_ID, provisioning_role=f"{ROLE}/provisioning"
        )
    )
    crypto = {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:CreateGrant", "kms:*"}
    account = by_sid(policy, "AccountAdministersWithoutUsing")
    assert not crypto & set(account["Action"])
    use = by_sid(policy, "OnlyProvisioningUses")
    assert use["Principal"] == {"AWS": f"{ROLE}/provisioning"}
    assert {"kms:Decrypt", "kms:GenerateDataKey"} <= set(use["Action"])


# --- The Cognito admin Lambda: only the provisioning role -------------------------------------


def test_lambda_endpoint_allows_one_function_and_one_caller() -> None:
    policy = json.loads(
        provisioning.lambda_endpoint_policy(
            invite_function="arn:fn", provisioning_role=f"{ROLE}/provisioning"
        )
    )
    [statement] = policy["Statement"]
    assert statement["Action"] == "lambda:InvokeFunction"
    assert statement["Resource"] == "arn:fn"
    assert statement["Condition"] == {"ArnEquals": {"aws:PrincipalArn": f"{ROLE}/provisioning"}}


@pulumi.runtime.test
def test_only_the_provisioning_role_may_invoke_the_cognito_admin_lambda() -> Any:
    stack = Stack("p1")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", f"{stack.name_prefix}-cognito-admin-fn")
        assert "vpcConfig" not in function  # outside the VPC: it must reach Cognito
        assert function["architectures"] == ["x86_64"]
        assert set(function["environment"]["variables"]) == {"TENANT_POOL_ID"}

        invokers = sorted(
            name
            for name, document in stack.role_policies().items()
            for statement in document["Statement"]
            if statement["Effect"] == "Allow"
            and "lambda:InvokeFunction" in as_list(statement["Action"])
        )
        assert invokers == ["p1-provision-task-policy"]

        # No resource-based permission: IAM is the only way in.
        permissions = [
            inputs
            for _, inputs in registered("aws:lambda/permission:Permission")
            if "cognito-admin" in str(inputs["function"])
        ]
        assert permissions == []

        cognito = by_sid(
            _one("aws:iam/rolePolicy:RolePolicy", f"{stack.name_prefix}-cognito-admin-policy")[
                "policy"
            ],
            "InviteTenantPoolUsers",
        )
        assert set(cognito["Action"]) == {
            "cognito-idp:AdminCreateUser",
            "cognito-idp:AdminGetUser",
            "cognito-idp:AdminAddUserToGroup",
        }
        assert "tenant" in cognito["Resource"]

        endpoint_policy = _one(
            "aws:ec2/vpcEndpointPolicy:VpcEndpointPolicy", "p1-provision-lambda-endpoint-policy"
        )
        assert endpoint_policy["vpcEndpointId"] == "p1-lambda-id"

    return stack.ready().apply(check)


# --- The tenant role and the TSS --------------------------------------------------------------


@pulumi.runtime.test
def test_tenant_role_trusts_only_the_tss_and_uses_the_one_template() -> Any:
    stack = Stack("t1")

    def check(_: list[Any]) -> None:
        role = _one("aws:iam/role:Role", f"{stack.name_prefix}-tenant-role")
        assert role["name"] == f"{stack.name_prefix}-tenant"
        assert role["maxSessionDuration"] == 3600
        [statement] = json.loads(role["assumeRolePolicy"])["Statement"]
        tss_role = f"{ROLE}/t1-tss-role-a1b2c3d"
        assert statement["Principal"] == {"AWS": tss_role}
        assert "sts:SourceIdentity" in statement["Condition"]["StringLike"]

        policy = _one("aws:iam/rolePolicy:RolePolicy", f"{stack.name_prefix}-tenant-policy")
        assert json.loads(policy["policy"]) == policies.tenant_role_policy(
            region=REGION, account_id=ACCOUNT_ID
        )

        # Only the TSS's own policy lets anything assume the tenant role (the isolation
        # runner's probe aside, which the trust policy refuses).
        assumers = sorted(
            name
            for name, document in stack.role_policies().items()
            for s in document["Statement"]
            if s["Effect"] == "Allow" and "sts:AssumeRole" in as_list(s["Action"])
        )
        assert assumers == ["t1-isolation-policy", "t1-tss-role-policy"]

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_tss_lambda_runs_in_the_vpc_as_database_role_tss() -> Any:
    stack = Stack("t2")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "t2-tss-fn")
        assert function["architectures"] == ["x86_64"]
        assert function["packageType"] == "Image"
        assert function["timeout"] == 29
        assert function["vpcConfig"]["subnetIds"] == ["t2-app-use1-az2-id"]
        variables = function["environment"]["variables"]
        assert variables["NAME_PREFIX"] == stack.name_prefix
        assert variables["TENANT_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-tenant"
        assert json.loads(variables["TSS_CALLERS"]) == {"t2-isolation-role-a1b2c3d": "isolation"}
        assert variables["AWS_STS_REGIONAL_ENDPOINTS"] == "regional"

        policy = _one("aws:iam/rolePolicy:RolePolicy", "t2-tss-role-policy")["policy"]
        assert by_sid(policy, "ConnectAsTss")["Resource"].endswith("/tss")
        assume = by_sid(policy, "AssumeTheTenantRole")
        assert assume["Resource"] == f"{ROLE}/{stack.name_prefix}-tenant"
        document = json.loads(policy)
        services = {a.split(":")[0] for s in document["Statement"] for a in as_list(s["Action"])}
        assert services == {"logs", "ec2", "rds-db", "sts"}  # no S3, no KMS: it holds no data

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_tss_api_is_private_with_iam_authorization() -> Any:
    stack = Stack("t3")

    def check(resolved: list[Any]) -> None:
        api = _one("aws:apigateway/restApi:RestApi", "t3-tss-api")
        assert api["endpointConfiguration"] == {
            "types": "PRIVATE",
            "vpcEndpointIds": ["t3-execute-api-id"],
        }
        policy = json.loads(api["policy"])
        deny = by_sid(policy, "OnlyThroughTheVpcEndpoint")
        assert deny["Effect"] == "Deny" and deny["Principal"] == "*"
        assert deny["Condition"] == {"StringNotEquals": {"aws:SourceVpce": "t3-execute-api-id"}}
        allow = by_sid(policy, "KnownCallers")
        assert allow["Principal"] == {"AWS": [f"{ROLE}/t3-isolation-role-a1b2c3d"]}
        assert allow["Resource"] == "execute-api:/v1/POST/resolve"

        method = _one("aws:apigateway/method:Method", "t3-tss-api-resolve-post")
        assert (method["httpMethod"], method["authorization"]) == ("POST", "AWS_IAM")
        integration = _one("aws:apigateway/integration:Integration", "t3-tss-api-resolve-lambda")
        assert integration["type"] == "AWS_PROXY"
        stage = _one("aws:apigateway/stage:Stage", "t3-tss-api-stage")
        assert stage["stageName"] == "v1"
        assert "userArn" in stage["accessLogSettings"]["format"]

        [permission] = [
            inputs
            for name, inputs in registered("aws:lambda/permission:Permission")
            if name == "t3-tss-api-invoke"
        ]
        assert permission["principal"] == "apigateway.amazonaws.com"
        assert permission["sourceArn"].endswith("/*/POST/resolve")

        caller = _one("aws:iam/rolePolicy:RolePolicy", "t3-tss-caller-isolation")["policy"]
        [statement] = json.loads(caller)["Statement"]
        assert statement["Action"] == "execute-api:Invoke"
        assert statement["Resource"].endswith("/v1/POST/resolve")
        assert resolved[2] == f"https://t3-tss-api-id.execute-api.{REGION}.amazonaws.com/v1"

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_without_the_isolation_runner_the_tss_has_no_caller() -> Any:
    stack = Stack("t4", with_runner=False)

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "t4-tss-fn")
        assert function["environment"]["variables"]["TSS_CALLERS"] == "{}"
        policy = json.loads(_one("aws:apigateway/restApi:RestApi", "t4-tss-api")["policy"])
        assert [s["Effect"] for s in policy["Statement"]] == ["Deny"]
        assert registered("aws:lambda/function:Function", "t4-isolation") == []
        assert "t4-isolation-policy" not in stack.role_policies()

    return stack.ready().apply(check)


# --- Break glass ------------------------------------------------------------------------------


@pulumi.runtime.test
def test_break_glass_role_is_assumable_by_one_principal_and_alarmed() -> Any:
    stack = Stack("b1")

    def check(_: list[Any]) -> None:
        name = f"{stack.name_prefix}-break-glass"
        role = _one("aws:iam/role:Role", f"{name}-role")
        assert role["name"] == name
        [statement] = json.loads(role["assumeRolePolicy"])["Statement"]
        assert statement["Principal"] == {"AWS": BREAK_GLASS_PRINCIPAL}
        assert statement["Action"] == "sts:AssumeRole"

        policy = json.loads(_one("aws:iam/rolePolicy:RolePolicy", f"{name}-policy")["policy"])
        actions = {a for s in policy["Statement"] for a in as_list(s["Action"])}
        assert "s3:GetObject" in actions
        assert not actions & {"s3:PutObject", "s3:DeleteObject", "s3:PutBucketTagging"}
        assert all(
            f"{stack.name_prefix}-t-*" in r
            for s in policy["Statement"]
            for r in as_list(s["Resource"])
        )

        rule = _one("aws:cloudwatch/eventRule:EventRule", f"{name}-assumed")
        # AssumeRole is a read-only management event; a rule sees it only in this state.
        assert rule["state"] == "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
        pattern = json.loads(rule["eventPattern"])
        assert pattern["detail"]["eventName"] == ["AssumeRole"]
        assert pattern["detail"]["requestParameters"] == {"roleArn": [f"{ROLE}/{name}"]}

        target = _one("aws:cloudwatch/eventTarget:EventTarget", f"{name}-assumed-log")
        assert target["rule"] == f"{name}-assumed"
        alarm = _one("aws:cloudwatch/metricAlarm:MetricAlarm", f"{name}-assumed-alarm")
        assert (alarm["namespace"], alarm["metricName"]) == ("AWS/Events", "MatchedEvents")
        assert alarm["dimensions"] == {"RuleName": f"{name}-assumed"}
        assert alarm["threshold"] == 1 and alarm["treatMissingData"] == "notBreaching"
        assert alarm["alarmActions"] == [stack.topic_arn()]

    return stack.ready().apply(check)


# --- The provisioning task and state machine --------------------------------------------------


def task_environment() -> dict[str, str]:
    return dict.fromkeys(provisioning.TASK_VARIABLES, "x")


def test_task_container_is_locked_down() -> None:
    [container] = json.loads(
        provisioning.container_definitions(
            image="repo@sha256:abc",
            log_group="/aws/ecs/p",
            region=REGION,
            environment=task_environment(),
        )
    )
    assert container["user"] == "10001:10001"
    assert container["readonlyRootFilesystem"] is True
    assert container["privileged"] is False
    assert container["linuxParameters"] == {"capabilities": {"add": [], "drop": ["ALL"]}}
    assert container["mountPoints"] == [
        {"sourceVolume": "work", "containerPath": "/work", "readOnly": False}
    ]
    assert container["portMappings"] == []
    names = [e["name"] for e in container["environment"]]
    assert names == sorted(names)


def test_task_is_never_defined_without_its_settings() -> None:
    environment = task_environment()
    del environment["STATE_BUCKET"]
    environment["SCAN_ROLE_ARN"] = ""
    with pytest.raises(provisioning.TaskEnvironmentError, match="SCAN_ROLE_ARN, STATE_BUCKET"):
        provisioning.container_definitions(
            image="i", log_group="g", region=REGION, environment=environment
        )


def test_state_machine_records_a_failure_and_then_fails() -> None:
    definition = json.loads(
        provisioning.state_machine_definition(
            cluster="cluster-arn",
            task_definition="task-arn",
            subnet="subnet-1",
            security_group="sg-1",
        )
    )
    states = definition["States"]
    assert definition["StartAt"] == "Provision"

    provision = states["Provision"]
    assert provision["Resource"] == "arn:aws:states:::ecs:runTask.sync"
    assert provision["End"] is True
    assert provision["Catch"] == [
        {"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure"}
    ]
    network = provision["Parameters"]["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network == {
        "Subnets": ["subnet-1"],
        "SecurityGroups": ["sg-1"],
        "AssignPublicIp": "DISABLED",
    }
    [override] = provision["Parameters"]["Overrides"]["ContainerOverrides"]
    assert override["Name"] == "provision"
    assert {"Name": "PROVISION_COMMAND", "Value": "provision"} in override["Environment"]
    assert {
        "Name": "PROVISION_REQUEST",
        "Value.$": "States.JsonToString($.request)",
    } in override["Environment"]

    record = states["RecordFailure"]
    [override] = record["Parameters"]["Overrides"]["ContainerOverrides"]
    assert {"Name": "PROVISION_COMMAND", "Value": "fail"} in override["Environment"]
    assert {"Name": "PROVISION_ERROR", "Value.$": "$.error.Error"} in override["Environment"]
    assert record["Next"] == "Failed"
    assert states["Failed"]["Type"] == "Fail"


@pulumi.runtime.test
def test_provisioning_task_and_state_machine() -> Any:
    stack = Stack("p2")

    def check(_: list[Any]) -> None:
        task = _one("aws:ecs/taskDefinition:TaskDefinition", "p2-provision-task")
        assert task["requiresCompatibilities"] == ["FARGATE"]
        assert task["runtimePlatform"] == {
            "cpuArchitecture": "X86_64",
            "operatingSystemFamily": "LINUX",
        }
        assert (task["cpu"], task["memory"]) == ("1024", "4096")
        assert task["volumes"] == [{"name": "work"}]
        [container] = json.loads(task["containerDefinitions"])
        environment = {e["name"]: e["value"] for e in container["environment"]}
        assert set(environment) == set(provisioning.TASK_VARIABLES)
        assert environment["TENANT_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-tenant"
        assert environment["BREAK_GLASS_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-break-glass"
        assert environment["PROVISIONING_ROLE_ARN"] == f"{ROLE}/p2-provision-task-role-a1b2c3d"
        assert environment["VECTOR_DIMENSIONS"] == "1024"
        assert json.loads(environment["DEFAULT_TAGS"]) == TAGS
        assert environment["STATE_BUCKET"] == "p2-provision-state-a1b2c3d"

        machine = _one("aws:sfn/stateMachine:StateMachine", "p2-provision-sfn")
        assert machine["type"] == "STANDARD"
        # The execution's input holds the first admin's name and email: never logged.
        assert machine["loggingConfiguration"]["includeExecutionData"] is False
        definition = json.loads(machine["definition"])
        parameters = definition["States"]["Provision"]["Parameters"]
        assert parameters["TaskDefinition"].endswith("task-definition/p2-provision:1")
        assert parameters["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"] == [
            "p2-app-use1-az2-id"
        ]

        sfn_policy = _one("aws:iam/rolePolicy:RolePolicy", "p2-provision-sfn-policy")["policy"]
        passed = by_sid(sfn_policy, "PassTheTaskRoles")
        assert passed["Condition"] == {
            "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
        }
        assert len(passed["Resource"]) == 2

        alarm = _one("aws:cloudwatch/metricAlarm:MetricAlarm", "p2-provision-failed-alarm")
        metrics = {q["metric"]["metricName"] for q in alarm["metricQueries"] if q.get("metric")}
        assert metrics == {"ExecutionsFailed", "ExecutionsTimedOut", "ExecutionsAborted"}
        assert alarm["alarmActions"] == [stack.topic_arn()]

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_state_bucket_is_private_versioned_and_encrypted_with_its_own_key() -> Any:
    stack = Stack("p3")

    def check(_: list[Any]) -> None:
        versioning = _one(
            "aws:s3/bucketVersioning:BucketVersioning", "p3-provision-state-versioning"
        )
        assert versioning["versioningConfiguration"] == {"status": "Enabled"}
        block = _one(
            "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock",
            "p3-provision-state-public-access",
        )
        assert all(
            block[k]
            for k in (
                "blockPublicAcls",
                "blockPublicPolicy",
                "ignorePublicAcls",
                "restrictPublicBuckets",
            )
        )
        encryption = _one(
            "aws:s3/bucketServerSideEncryptionConfiguration:BucketServerSideEncryptionConfiguration",
            "p3-provision-state-encryption",
        )
        [rule] = encryption["rules"]
        assert rule["bucketKeyEnabled"] is True
        default = rule["applyServerSideEncryptionByDefault"]
        assert default["sseAlgorithm"] == "aws:kms"
        assert default["kmsMasterKeyId"].endswith("key/p3-provision-state-key-key-id")
        key = _one("aws:kms/key:Key", "p3-provision-state-key")
        assert key["enableKeyRotation"] is True
        use = by_sid(key["policy"], "OnlyProvisioningUses")
        assert use["Principal"] == {"AWS": f"{ROLE}/p3-provision-task-role-a1b2c3d"}
        policy = _one("aws:s3/bucketPolicy:BucketPolicy", "p3-provision-state-policy")["policy"]
        deny = by_sid(policy, "OnlyProvisioningReadsOrWritesState")
        assert deny["Condition"] == {
            "ArnNotEquals": {"aws:PrincipalArn": f"{ROLE}/p3-provision-task-role-a1b2c3d"}
        }

    return stack.ready().apply(check)


# --- The isolation runner ---------------------------------------------------------------------


def test_runner_probes_are_limited_to_tenant_resources() -> None:
    policy = json.loads(
        isolation.runner_role_policy(
            region=REGION,
            account_id=ACCOUNT_ID,
            name_prefix=PREFIX,
            log_group="arn:logs",
            db_user="arn:dbuser/app_tenant",
            tenant_role=f"{ROLE}/{PREFIX}-tenant",
        )
    )
    keys = by_sid(policy, "ProbeTenantKeys")
    # Without this condition the probe would reach keys whose policies trust IAM.
    assert keys["Condition"] == {"Null": {"aws:ResourceTag/tenant_id": "false"}}
    buckets = by_sid(policy, "ProbeTenantBuckets")
    assert all("assay-dev-t-*" in r for r in buckets["Resource"])
    services = {a.split(":")[0] for s in policy["Statement"] for a in as_list(s["Action"])}
    # No Rekognition (a collection has no resource policy to refuse it) and no Lambda.
    assert services == {"logs", "ec2", "rds-db", "sts", "s3", "kms", "s3vectors"}
    assert by_sid(policy, "ConnectAsAppTenant")["Resource"].endswith("/app_tenant")


@pulumi.runtime.test
def test_isolation_runner_runs_in_the_vpc() -> Any:
    stack = Stack("i1")

    def check(_: list[Any]) -> None:
        function = _one("aws:lambda/function:Function", "i1-isolation-fn")
        assert function["architectures"] == ["x86_64"]
        assert function["vpcConfig"]["subnetIds"] == ["i1-app-use1-az2-id"]
        variables = function["environment"]["variables"]
        assert variables["TSS_ENDPOINT"].startswith("https://")
        assert variables["TSS_SERVICE"] == "isolation"
        assert variables["TENANT_ROLE_ARN"] == f"{ROLE}/{stack.name_prefix}-tenant"
        build = _one("command:local:Command", "i1-isolation-image")
        assert build["environment"]["IMAGE_DOCKERFILE"] == "tests/isolation/Dockerfile"

    return stack.ready().apply(check)


@pulumi.runtime.test
def test_new_endpoints_exist_for_the_tss_and_provisioning() -> Any:
    stack = Stack("e1")

    def check(_: list[Any]) -> None:
        services = {
            inputs["serviceName"]
            for _, inputs in registered("aws:ec2/vpcEndpoint:VpcEndpoint", "e1-")
        }
        for service in ("execute-api", "lambda", "guardduty"):
            assert f"com.amazonaws.{REGION}.{service}" in services

    return stack.ready().apply(check)


def test_tss_access_log_holds_no_request_body() -> None:
    assert not any("body" in value.lower() for value in tss.ACCESS_LOG_FORMAT.values())


# --- The alarm topic ---------------------------------------------------------------------------


def test_alarm_email_comes_from_config_and_is_checked() -> None:
    assert validate_alerts(AlertsConfig(email="alarms@example.test")).email == "alarms@example.test"
    for bad in ("", "not-an-address", "two words@example.test"):
        with pytest.raises(ValueError, match="alarmEmail"):
            validate_alerts(AlertsConfig(email=bad))


def test_only_cloudwatch_alarms_in_this_account_publish_to_the_topic() -> None:
    policy = json.loads(
        alerts.topic_policy(topic="arn:topic", region=REGION, account_id=ACCOUNT_ID)
    )
    publish = by_sid(policy, "CloudWatchAlarmsInThisAccountPublish")
    assert publish["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert publish["Action"] == "sns:Publish"
    assert publish["Condition"] == {
        "ArnLike": {"aws:SourceArn": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT_ID}:alarm:*"},
        "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
    }
    allows = [s for s in policy["Statement"] if s["Effect"] == "Allow"]
    assert len(allows) == 1
    assert by_sid(policy, "DenyInsecureTransport")["Effect"] == "Deny"


def test_alarm_topic_key_lets_cloudwatch_publish() -> None:
    """CloudWatch cannot publish to a topic encrypted with the AWS-managed SNS key."""
    statement = by_sid(alerts.key_policy(ACCOUNT_ID), "CloudWatchAlarmsPublish")
    assert statement["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert set(statement["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey*"}


@pulumi.runtime.test
def test_alarm_topic_is_encrypted_and_emails_the_configured_address() -> Any:
    stack = Stack("a1")

    def check(_: list[Any]) -> None:
        topic = _one("aws:sns/topic:Topic", "a1-alarms-topic")
        assert topic["name"] == "a1-alarms"
        assert topic["kmsMasterKeyId"] == "a1-alarms-key-key-id"
        key = _one("aws:kms/key:Key", "a1-alarms-key")
        assert key["enableKeyRotation"] is True
        subscription = _one("aws:sns/topicSubscription:TopicSubscription", "a1-alarms-email")
        assert (subscription["protocol"], subscription["endpoint"]) == ("email", ALARM_EMAIL)
        assert subscription["topic"] == stack.topic_arn()
        policy = _one("aws:sns/topicPolicy:TopicPolicy", "a1-alarms-topic-policy")
        assert policy["arn"] == stack.topic_arn()
        # Every alarm in the stack notifies the topic.
        alarms = [
            (name, inputs)
            for name, inputs in registered("aws:cloudwatch/metricAlarm:MetricAlarm")
            if name.startswith(("a1-", "assay-a1-"))
        ]
        assert len(alarms) == 2
        for name, inputs in alarms:
            assert inputs.get("alarmActions") == [stack.topic_arn()], name

    return pulumi.Output.all(
        stack.ready(), stack.alerts.subscription.id, stack.alerts.topic.arn
    ).apply(check)
