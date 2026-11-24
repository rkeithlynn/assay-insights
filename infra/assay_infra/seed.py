"""The seed Lambda (`assay_api.seed`), the database half of `scripts/seed.py` (section 5).

In the VPC, signed in as `platform_writer`. It adds business tenants through the same code as
`POST /admin/tenants` (so it may start the provisioning state machine) and invites their
users through the same code as `POST /users` (so it may invoke the API's Cognito admin
function). It has no resource-based permission: only the owner, whose IAM role allows
invoking any function, can run it.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

from assay_infra.database import Database, db_user_arn
from assay_infra.network import Network
from assay_infra.tss import VPC_NETWORK_INTERFACE_STATEMENT, _service_trust

PLATFORM_WRITER_DB_USER = "platform_writer"  # assay_db.roles.PLATFORM_WRITER
HANDLER = "assay_api.seed.handler"
TIMEOUT_SECONDS = 120
LOG_RETENTION_DAYS = 30


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def seed_role_policy(
    *, log_group: str, db_user: str, cognito_admin_function: str, state_machine: str
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
                VPC_NETWORK_INTERFACE_STATEMENT,
                {
                    "Sid": "ConnectAsPlatformWriter",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": db_user,
                },
                {
                    "Sid": "InviteTenantUsers",
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunction",
                    "Resource": cognito_admin_function,
                },
                {
                    "Sid": "StartTenantProvisioning",
                    "Effect": "Allow",
                    "Action": "states:StartExecution",
                    "Resource": state_machine,
                },
            ],
        }
    )


class SeedFunction(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        image_uri: pulumi.Input[str],
        region: str,
        account_id: pulumi.Input[str],
        network: Network,
        database: Database,
        database_name: str,
        cognito_admin_function: aws.lambda_.Function,
        state_machine: aws.sfn.StateMachine,
        default_models: tuple[str, str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:data:SeedFunction", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)

        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/lambda/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        self.role = aws.iam.Role(
            f"{name}-role",
            description="Seed Lambda: platform tenant and users, seeded business tenants",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("lambda.amazonaws.com", acct)
            ),
            opts=child,
        )
        policy = aws.iam.RolePolicy(
            f"{name}-policy",
            role=self.role.id,
            policy=pulumi.Output.all(
                log_group=self.log_group.arn,
                db_user=db_user_arn(
                    region, account_id, database.cluster_resource_id, PLATFORM_WRITER_DB_USER
                ),
                cognito_admin_function=cognito_admin_function.arn,
                state_machine=state_machine.arn,
            ).apply(lambda a: seed_role_policy(**a)),
            opts=child,
        )
        self.function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Seed: platform tenant and users, business tenants (scripts/seed.py)",
            role=self.role.arn,
            package_type="Image",
            image_uri=image_uri,
            image_config=aws.lambda_.FunctionImageConfigArgs(commands=[HANDLER]),
            architectures=["x86_64"],
            memory_size=512,
            timeout=TIMEOUT_SECONDS,
            vpc_config=aws.lambda_.FunctionVpcConfigArgs(
                subnet_ids=[network.workload_subnet.id],
                security_group_ids=[network.workload_security_group.id],
            ),
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={
                    "DB_HOST": database.endpoint,
                    "DB_PORT": database.port.apply(str),
                    "DB_NAME": database_name,
                    "COGNITO_ADMIN_FUNCTION": cognito_admin_function.arn,
                    "PROVISIONING_STATE_MACHINE_ARN": state_machine.arn,
                    "DEFAULT_INGEST_MODEL": default_models[0],
                    "DEFAULT_QUERY_MODEL": default_models[1],
                }
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[policy]),
        )
        self.register_outputs({"function_name": self.function.name})
