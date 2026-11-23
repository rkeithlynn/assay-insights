"""Aurora PostgreSQL Serverless v2 cluster, encrypted with a customer-managed KMS key.

BUILD_SPEC.md section 4, item 3. The master password is managed by RDS in Secrets Manager.
Application roles (section 7) sign in with IAM database authentication; they have no
stored passwords. Roles, tables and row-level security are created by the migration
Lambda (assay_infra.migrations), not here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pulumi
import pulumi_aws as aws

from assay_infra.config import DatabaseConfig
from assay_infra.network import Network

MASTER_USERNAME = "assay_admin"
BACKUP_RETENTION_DAYS = 7
LOG_RETENTION_DAYS = 30
CA_CERT_IDENTIFIER = "rds-ca-rsa2048-g1"
PORT = 5432


def key_policy(account_id: str) -> str:
    """Key administration and use stay with IAM policies in this account."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AccountIamPolicies",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                    "Action": "kms:*",
                    "Resource": "*",
                }
            ],
        }
    )


class Database(pulumi.ComponentResource):
    """One Aurora PostgreSQL Serverless v2 writer in the workload zone."""

    def __init__(
        self,
        name: str,
        *,
        cfg: DatabaseConfig,
        network: Network,
        account_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:data:Database", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        protected = pulumi.ResourceOptions(parent=self, protect=True)

        self.key = aws.kms.Key(
            f"{name}-key",
            description=f"{name}: Aurora storage and master secret",
            enable_key_rotation=True,
            rotation_period_in_days=365,
            deletion_window_in_days=30,
            policy=pulumi.Output.from_input(account_id).apply(key_policy),
            opts=protected,
        )
        aws.kms.Alias(
            f"{name}-key-alias",
            name=f"alias/{name}",
            target_key_id=self.key.key_id,
            opts=child,
        )

        self.subnet_group = aws.rds.SubnetGroup(
            f"{name}-subnets",
            name=name,
            description=f"{name}: data subnets",
            subnet_ids=[s.id for s in network.data_subnets],
            opts=child,
        )

        self.parameter_group = aws.rds.ClusterParameterGroup(
            f"{name}-params",
            name=f"{name}-aurora-postgresql{cfg.major_version}",
            family=f"aurora-postgresql{cfg.major_version}",
            description=f"{name}: TLS required",
            parameters=[
                aws.rds.ClusterParameterGroupParameterArgs(
                    name="rds.force_ssl", value="1", apply_method="pending-reboot"
                ),
            ],
            opts=child,
        )

        # Created first so RDS log exports land in a group with 30-day retention.
        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-postgresql-logs",
            name=f"/aws/rds/cluster/{name}/postgresql",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )

        # Minor versions upgrade automatically, so the version is ignored after creation to
        # keep `pulumi preview` clean. Change dbEngineVersion and remove the ignore to upgrade.
        version_ignored = pulumi.ResourceOptions(ignore_changes=["engineVersion"])
        self.cluster = aws.rds.Cluster(
            f"{name}-cluster",
            cluster_identifier=name,
            engine=aws.rds.EngineType.AURORA_POSTGRESQL,
            engine_mode="provisioned",
            engine_version=cfg.engine_version,
            database_name=cfg.database_name,
            port=PORT,
            master_username=MASTER_USERNAME,
            manage_master_user_password=True,
            master_user_secret_kms_key_id=self.key.arn,
            storage_encrypted=True,
            kms_key_id=self.key.arn,
            iam_database_authentication_enabled=True,
            db_subnet_group_name=self.subnet_group.name,
            vpc_security_group_ids=[network.database_security_group.id],
            db_cluster_parameter_group_name=self.parameter_group.name,
            serverlessv2_scaling_configuration=aws.rds.ClusterServerlessv2ScalingConfigurationArgs(
                min_capacity=cfg.min_acu,
                max_capacity=cfg.max_acu,
                seconds_until_auto_pause=cfg.auto_pause_seconds,
            ),
            backup_retention_period=BACKUP_RETENTION_DAYS,
            preferred_backup_window="07:00-08:00",
            preferred_maintenance_window="sun:08:30-sun:09:30",
            copy_tags_to_snapshot=True,
            deletion_protection=True,
            skip_final_snapshot=False,
            final_snapshot_identifier=f"{name}-final",
            enabled_cloudwatch_logs_exports=["postgresql"],
            opts=pulumi.ResourceOptions.merge(
                protected,
                pulumi.ResourceOptions(depends_on=[self.log_group]),
            ).merge(version_ignored),
        )

        self.writer = aws.rds.ClusterInstance(
            f"{name}-writer",
            identifier=f"{name}-1",
            cluster_identifier=self.cluster.id,
            instance_class="db.serverless",
            engine=aws.rds.EngineType.AURORA_POSTGRESQL,
            engine_version=self.cluster.engine_version,
            availability_zone=network.workload_subnet.availability_zone,
            db_subnet_group_name=self.subnet_group.name,
            publicly_accessible=False,
            auto_minor_version_upgrade=True,
            ca_cert_identifier=CA_CERT_IDENTIFIER,
            performance_insights_enabled=False,
            opts=pulumi.ResourceOptions.merge(child, version_ignored),
        )

        self.endpoint = self.cluster.endpoint
        self.port = self.cluster.port
        self.master_secret_arn: pulumi.Output[str] = self.cluster.master_user_secrets.apply(
            _master_secret_arn
        )
        self.cluster_resource_id = self.cluster.cluster_resource_id

        self.register_outputs(
            {
                "endpoint": self.endpoint,
                "master_secret_arn": self.master_secret_arn,
                "cluster_resource_id": self.cluster_resource_id,
            }
        )


def _master_secret_arn(
    secrets: Sequence[aws.rds.outputs.ClusterMasterUserSecret] | None,
) -> str:
    arn = secrets[0].secret_arn if secrets else None
    if not arn:
        raise ValueError("RDS returned no managed master user secret")
    return arn


def db_user_arn(
    region: str, account_id: pulumi.Input[str], cluster_resource_id: pulumi.Input[str], user: str
) -> pulumi.Output[str]:
    """The `rds-db:connect` resource ARN for one database user."""
    return pulumi.Output.format(
        "arn:aws:rds-db:{0}:{1}:dbuser:{2}/{3}", region, account_id, cluster_resource_id, user
    )
