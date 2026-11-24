"""The API's Cognito admin Lambda, `<prefix>-cognito-admin-api` (build step 5).

The same image as the provisioning role's `<prefix>-cognito-admin` (built in
`assay_infra.provisioning`), deployed as a second function with its own role and its own
`OPERATIONS`. The step 4 function stays exactly as it was: `invite` only, invoked only by the
provisioning role.

This function runs outside the VPC (a user pool with a domain cannot be reached through
PrivateLink) and has no resource-based permission: only IAM policies can let a principal
invoke it, and only the API task role and the seed Lambda's role have one. The `lambda`
endpoint's policy names the same two roles.

What its role may do in Cognito, by pool:

- tenant pool: invite (create a user and add it to a group), resend an invitation, reset a
  password, disable and enable, change the group, and reset MFA (delete the TOTP device and
  sign the user out everywhere), plus reading a user and their groups to check the target;
- platform pool: only reading a user and their groups, deleting a TOTP device and signing a
  user out: the master admin's MFA reset of the master user. It cannot create, enable,
  disable or regroup a platform user.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

from assay_infra.auth import Auth
from assay_infra.tss import _service_trust

OPERATIONS = (
    "invite",
    "resend_invite",
    "reset_password",
    "disable",
    "enable",
    "set_role",
    "reset_mfa",
)
TENANT_POOL_ACTIONS = (
    "cognito-idp:AdminCreateUser",
    "cognito-idp:AdminGetUser",
    "cognito-idp:AdminListGroupsForUser",
    "cognito-idp:AdminAddUserToGroup",
    "cognito-idp:AdminRemoveUserFromGroup",
    "cognito-idp:AdminDisableUser",
    "cognito-idp:AdminEnableUser",
    "cognito-idp:AdminResetUserPassword",
    "cognito-idp:AdminDeleteSoftwareToken",
    "cognito-idp:AdminUserGlobalSignOut",
)
PLATFORM_POOL_ACTIONS = (
    "cognito-idp:AdminGetUser",
    "cognito-idp:AdminListGroupsForUser",
    "cognito-idp:AdminDeleteSoftwareToken",
    "cognito-idp:AdminUserGlobalSignOut",
)
TIMEOUT_SECONDS = 30
LOG_RETENTION_DAYS = 30


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def api_cognito_role_policy(*, log_group: str, tenant_pool: str, platform_pool: str) -> str:
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
                    "Sid": "TenantPoolUserAdministration",
                    "Effect": "Allow",
                    "Action": list(TENANT_POOL_ACTIONS),
                    "Resource": tenant_pool,
                },
                {
                    "Sid": "PlatformPoolMfaResetOnly",
                    "Effect": "Allow",
                    "Action": list(PLATFORM_POOL_ACTIONS),
                    "Resource": platform_pool,
                },
            ],
        }
    )


class CognitoAdminApi(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        image_uri: pulumi.Input[str],
        auth: Auth,
        account_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:auth:CognitoAdminApi", name, None, opts)
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
            description="Cognito admin for the API: tenant-pool users, platform MFA resets",
            assume_role_policy=account.apply(
                lambda acct: _service_trust("lambda.amazonaws.com", acct)
            ),
            opts=child,
        )
        self.role_policy = aws.iam.RolePolicy(
            f"{name}-policy",
            role=self.role.id,
            policy=pulumi.Output.all(
                log_group=self.log_group.arn,
                tenant_pool=auth.tenant.pool.arn,
                platform_pool=auth.platform.pool.arn,
            ).apply(lambda a: api_cognito_role_policy(**a)),
            opts=child,
        )
        # No resource-based permission is ever added: see the module docstring.
        self.function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Cognito admin for the API: user changes the API has authorized",
            role=self.role.arn,
            package_type="Image",
            image_uri=image_uri,
            architectures=["x86_64"],
            memory_size=256,
            timeout=TIMEOUT_SECONDS,
            environment=aws.lambda_.FunctionEnvironmentArgs(
                variables={
                    "TENANT_POOL_ID": auth.tenant.pool.id,
                    "PLATFORM_POOL_ID": auth.platform.pool.id,
                    "OPERATIONS": ",".join(OPERATIONS),
                }
            ),
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.role_policy]),
        )
        self.register_outputs({"function_name": self.function.name})
