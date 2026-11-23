"""The migration Lambda: database roles, Alembic migrations and audit_log partition upkeep.

The image is built from `db/Dockerfile` for linux/amd64 inside Docker (never packaged on the
Windows host) by `scripts/build_image.py` and pushed to a private ECR repository. The
function runs in the workload subnet. It is started by `scripts/migrate.py` after
`pulumi up`, never by `pulumi up` itself, and once a month by EventBridge Scheduler to
create the next audit_log partitions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_command as command

from assay_infra.database import Database, db_user_arn
from assay_infra.network import Network

MIGRATOR_DB_USER = "migrator"  # assay_db.roles.MIGRATOR
LOG_RETENTION_DAYS = 30
IMAGES_KEPT = 10
# 06:00 UTC on the first of every month.
PARTITION_SCHEDULE = "cron(0 6 1 * ? *)"

# The files `.dockerignore` lets into the build context. Their hash tags the image, so the
# tag changes exactly when the image content can change (the repository is tag-immutable).
IMAGE_INPUTS = ("pyproject.toml", "uv.lock", "infra/pyproject.toml", "db/pyproject.toml")
IMAGE_TREES = ("db/assay_db",)
DOCKERFILE = "db/Dockerfile"
BUILD_SCRIPT = "scripts/build_image.py"


def _image_files(repo_root: Path) -> Iterable[Path]:
    for rel in (*IMAGE_INPUTS, DOCKERFILE):
        yield repo_root / rel
    for tree in IMAGE_TREES:
        for path in sorted((repo_root / tree).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                yield path


def image_tag(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for path in _image_files(repo_root):
        digest.update(path.relative_to(repo_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"src-{digest.hexdigest()[:16]}"


def parse_digest(stdout: str) -> str:
    """The build script prints only the pushed image's digest on stdout."""
    lines = stdout.strip().splitlines()
    digest = lines[-1].strip() if lines else ""
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64:
        raise ValueError(f"image build printed no digest: {stdout!r}")
    return digest


def lambda_arns(region: str, account_id: str) -> str:
    return f"arn:aws:lambda:{region}:{account_id}:function:*"


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


class MigrationRunner(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        region: str,
        account_id: pulumi.Input[str],
        network: Network,
        database: Database,
        database_name: str,
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:data:MigrationRunner", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)

        # --- Image --------------------------------------------------------------------
        self.repository = aws.ecr.Repository(
            f"{name}-repo",
            name=f"{name}",
            image_tag_mutability="IMMUTABLE",
            image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
                scan_on_push=True
            ),
            encryption_configurations=[
                aws.ecr.RepositoryEncryptionConfigurationArgs(encryption_type="KMS")
            ],
            force_delete=False,
            opts=child,
        )
        aws.ecr.LifecyclePolicy(
            f"{name}-repo-lifecycle",
            repository=self.repository.name,
            policy=_json(
                {
                    "rules": [
                        {
                            "rulePriority": 1,
                            "description": f"Keep the last {IMAGES_KEPT} images",
                            "selection": {
                                "tagStatus": "any",
                                "countType": "imageCountMoreThan",
                                "countNumber": IMAGES_KEPT,
                            },
                            "action": {"type": "expire"},
                        }
                    ]
                }
            ),
            opts=child,
        )
        aws.ecr.RepositoryPolicy(
            f"{name}-repo-policy",
            repository=self.repository.name,
            policy=account.apply(
                lambda acct: _json(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "LambdaPull",
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                                "Condition": {
                                    "StringLike": {"aws:sourceArn": lambda_arns(region, acct)}
                                },
                            }
                        ],
                    }
                )
            ),
            opts=child,
        )

        # Built and pushed by scripts/build_image.py during `pulumi up` (never during preview),
        # and again only when the image's source files change. See that script for why the
        # build runs through the Docker CLI with attestations turned off.
        tag = image_tag(repo_root)
        environment: dict[str, pulumi.Input[str]] = {
            "IMAGE_REPOSITORY_URL": self.repository.repository_url,
            "IMAGE_TAG": tag,
            "IMAGE_DOCKERFILE": DOCKERFILE,
            "IMAGE_CONTEXT": repo_root.as_posix(),
            "AWS_REGION": region,
        }
        if aws_profile:
            environment["AWS_PROFILE"] = aws_profile
        self.image_build = command.local.Command(
            f"{name}-image",
            dir=repo_root.as_posix(),
            interpreter=["uv", "run", "--frozen", "python"],
            create=BUILD_SCRIPT,
            environment=environment,
            triggers=[self.repository.repository_url, tag],
            logging=command.local.Logging.STDERR,
            opts=child,
        )
        self.image_digest = self.image_build.stdout.apply(parse_digest)

        # --- Function -----------------------------------------------------------------
        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/lambda/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )

        self.role = aws.iam.Role(
            f"{name}-role",
            description="Migration Lambda: roles, Alembic, audit_log partitions",
            assume_role_policy=account.apply(
                lambda acct: _json(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "lambda.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                                "Condition": {"StringEquals": {"aws:SourceAccount": acct}},
                            }
                        ],
                    }
                )
            ),
            opts=child,
        )
        role_policy = aws.iam.RolePolicy(
            f"{name}-role-policy",
            role=self.role.id,
            policy=pulumi.Output.all(
                log_group=self.log_group.arn,
                secret=database.master_secret_arn,
                key=database.key.arn,
                migrator=db_user_arn(
                    region, account_id, database.cluster_resource_id, MIGRATOR_DB_USER
                ),
            ).apply(lambda a: migration_role_policy(region=region, **a)),
            opts=child,
        )

        self.function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Database roles, Alembic migrations and audit_log partitions",
            role=self.role.arn,
            package_type="Image",
            image_uri=pulumi.Output.concat(self.repository.repository_url, "@", self.image_digest),
            architectures=["x86_64"],
            memory_size=512,
            timeout=300,
            vpc_config=aws.lambda_.FunctionVpcConfigArgs(
                subnet_ids=[network.workload_subnet.id],
                security_group_ids=[network.workload_security_group.id],
            ),
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={
                    "DB_HOST": database.endpoint,
                    "DB_PORT": database.port.apply(lambda port: str(port)),
                    "DB_NAME": database_name,
                    "DB_MASTER_SECRET_ARN": database.master_secret_arn,
                }
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[role_policy]),
        )

        # --- Monthly audit_log partition upkeep ------------------------------------------
        scheduler_role = aws.iam.Role(
            f"{name}-scheduler-role",
            description="EventBridge Scheduler: start the migration Lambda's maintain command",
            assume_role_policy=account.apply(
                lambda acct: _json(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "scheduler.amazonaws.com"},
                                "Action": "sts:AssumeRole",
                                "Condition": {"StringEquals": {"aws:SourceAccount": acct}},
                            }
                        ],
                    }
                )
            ),
            opts=child,
        )
        aws.iam.RolePolicy(
            f"{name}-scheduler-policy",
            role=scheduler_role.id,
            policy=self.function.arn.apply(
                lambda arn: _json(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": arn}
                        ],
                    }
                )
            ),
            opts=child,
        )
        self.schedule = aws.scheduler.Schedule(
            f"{name}-partitions",
            name=f"{name}-audit-partitions",
            description="Create the next audit_log partitions",
            schedule_expression=PARTITION_SCHEDULE,
            schedule_expression_timezone="UTC",
            flexible_time_window=aws.scheduler.ScheduleFlexibleTimeWindowArgs(mode="OFF"),
            target=aws.scheduler.ScheduleTargetArgs(
                arn=self.function.arn,
                role_arn=scheduler_role.arn,
                input=_json({"command": "maintain"}),
                retry_policy=aws.scheduler.ScheduleTargetRetryPolicyArgs(maximum_retry_attempts=2),
            ),
            opts=child,
        )

        self.register_outputs({"function_name": self.function.name})


def migration_role_policy(
    *, region: str, log_group: str, secret: str, key: str, migrator: str
) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "Logs",
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"{log_group}:*",
                },
                {
                    # Lambda VPC networking. These actions do not support resource scoping.
                    "Sid": "VpcNetworkInterfaces",
                    "Effect": "Allow",
                    "Action": [
                        "ec2:CreateNetworkInterface",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DescribeSubnets",
                        "ec2:DeleteNetworkInterface",
                        "ec2:AssignPrivateIpAddresses",
                        "ec2:UnassignPrivateIpAddresses",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "MasterSecret",
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": secret,
                },
                {
                    "Sid": "MasterSecretKey",
                    "Effect": "Allow",
                    "Action": "kms:Decrypt",
                    "Resource": key,
                    "Condition": {
                        "StringEquals": {"kms:ViaService": f"secretsmanager.{region}.amazonaws.com"}
                    },
                },
                {
                    "Sid": "ConnectAsMigrator",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": migrator,
                },
            ],
        }
    )
