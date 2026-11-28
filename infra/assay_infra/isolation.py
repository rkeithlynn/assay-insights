"""The isolation test runner: `tests/isolation`, run inside the VPC (BUILD_SPEC.md section 6).

The TSS and the database are private, so the tests cannot run from a developer's machine.
This Lambda runs them with pytest in the workload subnet; `scripts/run_isolation.py` starts
it and prints the result. It exists only when stack config `isolationTests` is true.

The runner's role is a deliberate probe. Its IAM policy *allows* assuming the tenant role
and reading, writing and tagging tenant buckets and keys, so that the tests show the
resource side refusing it: the tenant role's trust policy, each tenant's bucket policy and
each tenant's key policy. If one of those were ever loosened, this role would get through
and the tests would fail. For the same reason it has no Rekognition permission: a face
collection has no resource policy to refuse it.

It may also call the TSS, under the service name `isolation`, for the two test tenants.

Since stage 2 of build step 6's second half it also holds the worker task's Textract
permission, word for word. The VPC's Textract endpoint admits only the worker task's role, so
the runner's call must be refused there, by the endpoint's policy and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pulumi
import pulumi_aws as aws

from assay_infra.database import Database, db_user_arn
from assay_infra.images import WORKSPACE_MANIFESTS, ContainerImage
from assay_infra.network import Network
from assay_infra.pipeline import textract_statement
from assay_infra.tss import VPC_NETWORK_INTERFACE_STATEMENT, _service_trust
from assay_tss import policies
from assay_tss.naming import tenant_pattern

# The TSS accepts this role's requests only with this service name in the source identity.
SERVICE_NAME = "isolation"
APP_TENANT_DB_USER = "app_tenant"  # assay_db.roles.APP_TENANT
DOCKERFILE = "tests/isolation/Dockerfile"
IMAGE_FILES = (*WORKSPACE_MANIFESTS, "tests/__init__.py")
IMAGE_TREES = ("db/assay_db", "tss/assay_tss", "tests/isolation")
TIMEOUT_SECONDS = 300
LOG_RETENTION_DAYS = 30

# Allowed here on purpose; each must be refused by a tenant resource's own policy.
PROBE_BUCKET_ACTIONS = (
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketPolicy",
    "s3:PutBucketTagging",
)
PROBE_KEY_ACTIONS = (
    "kms:Decrypt",
    "kms:GenerateDataKey",
    "kms:TagResource",
    "kms:UntagResource",
)
PROBE_VECTOR_ACTIONS = ("s3vectors:ListVectors", "s3vectors:GetVectors", "s3vectors:QueryVectors")
PROBE_ASSUME_ACTIONS = ("sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity")


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def runner_role(name: str, account_id: pulumi.Input[str]) -> aws.iam.Role:
    """Created before the TSS, which needs the role to list it as a caller."""
    return aws.iam.Role(
        f"{name}-role",
        description="Isolation test runner: a probe that tenant resources must refuse",
        assume_role_policy=pulumi.Output.from_input(account_id).apply(
            lambda acct: _service_trust("lambda.amazonaws.com", acct)
        ),
    )


def runner_role_policy(
    *,
    region: str,
    account_id: str,
    name_prefix: str,
    log_group: str,
    db_user: str,
    tenant_role: str,
    textract_endpoint: str,
) -> str:
    pattern = tenant_pattern(name_prefix)
    bucket = f"arn:aws:s3:::{pattern}"
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                # The same permission as the worker task's, condition and all (owner,
                # 2026-11-27), so that the Textract endpoint's policy is the only thing left
                # to refuse this role, and the test shows it doing so.
                textract_statement("ProbeTextractEndpoint", textract_endpoint),
                {
                    "Sid": "Logs",
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"{log_group}:*",
                },
                VPC_NETWORK_INTERFACE_STATEMENT,
                {
                    "Sid": "ConnectAsAppTenant",
                    "Effect": "Allow",
                    "Action": "rds-db:connect",
                    "Resource": db_user,
                },
                {
                    "Sid": "ProbeAssumeTheTenantRole",
                    "Effect": "Allow",
                    "Action": list(PROBE_ASSUME_ACTIONS),
                    "Resource": tenant_role,
                },
                {
                    "Sid": "ProbeTenantBuckets",
                    "Effect": "Allow",
                    "Action": list(PROBE_BUCKET_ACTIONS),
                    "Resource": [bucket, f"{bucket}/*"],
                },
                {
                    # Tenant keys only: a key without a tenant_id tag is out of reach.
                    "Sid": "ProbeTenantKeys",
                    "Effect": "Allow",
                    "Action": list(PROBE_KEY_ACTIONS),
                    "Resource": f"arn:aws:kms:{region}:{account_id}:key/*",
                    "Condition": {"Null": {f"aws:ResourceTag/{policies.TENANT_TAG}": "false"}},
                },
                {
                    "Sid": "ProbeTenantVectors",
                    "Effect": "Allow",
                    "Action": list(PROBE_VECTOR_ACTIONS),
                    "Resource": (
                        f"arn:aws:s3vectors:{region}:{account_id}:bucket/{pattern}/index/*"
                    ),
                },
            ],
        }
    )


class IsolationRunner(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        role: aws.iam.Role,
        repo_root: Path,
        region: str,
        account_id: pulumi.Input[str],
        name_prefix: str,
        network: Network,
        database: Database,
        database_name: str,
        tss_endpoint_url: pulumi.Input[str],
        tenant_role_arn: pulumi.Input[str],
        invite_function_arn: pulumi.Input[str],
        # The API's Cognito admin function (build step 5): refused to the runner too.
        api_cognito_function_arn: pulumi.Input[str],
        # The VPC's Textract endpoint, whose policy must refuse the runner (stage 2).
        textract_endpoint_id: pulumi.Input[str],
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:tenancy:IsolationRunner", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.image = ContainerImage(
            name,
            repo_root=repo_root,
            dockerfile=DOCKERFILE,
            files=IMAGE_FILES,
            trees=IMAGE_TREES,
            region=region,
            account_id=account_id,
            lambda_pull=True,
            aws_profile=aws_profile,
            opts=child,
        )
        self.log_group = aws.cloudwatch.LogGroup(
            f"{name}-logs",
            name=f"/aws/lambda/{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        role_policy = aws.iam.RolePolicy(
            f"{name}-policy",
            role=role.id,
            policy=pulumi.Output.all(
                account_id=account_id,
                log_group=self.log_group.arn,
                db_user=db_user_arn(
                    region, account_id, database.cluster_resource_id, APP_TENANT_DB_USER
                ),
                tenant_role=tenant_role_arn,
                textract_endpoint=textract_endpoint_id,
            ).apply(lambda a: runner_role_policy(region=region, name_prefix=name_prefix, **a)),
            opts=child,
        )
        self.function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Runs tests/isolation inside the VPC (development stacks only)",
            role=role.arn,
            package_type="Image",
            image_uri=self.image.image_uri,
            architectures=["x86_64"],
            memory_size=1024,
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
                    "NAME_PREFIX": name_prefix,
                    "TSS_ENDPOINT": tss_endpoint_url,
                    "TSS_SERVICE": SERVICE_NAME,
                    "TENANT_ROLE_ARN": tenant_role_arn,
                    "INVITE_FUNCTION": invite_function_arn,
                    "API_COGNITO_FUNCTION": api_cognito_function_arn,
                    "AWS_STS_REGIONAL_ENDPOINTS": "regional",
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
        self.register_outputs({"function_name": self.function.name})
