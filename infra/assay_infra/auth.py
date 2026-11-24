"""Cognito: the tenant and platform user pools, managed login and the pre-token trigger.

BUILD_SPEC.md section 4, item 4.

- Two pools on the Essentials plan. Self sign-up off, temporary passwords expire in 7 days,
  sign-in by email, account recovery by verified email only, deletion protection on.
- Tenant pool: groups tenant-admin and tenant-user; TOTP MFA optional at pool level; an
  immutable `custom:tenant_id` that no app client can write. A pre-token-generation trigger
  copies it into the access token, with whether the user has TOTP (`assay_api.claims`).
- Platform pool: groups master-admin and master-user; TOTP MFA required.
- Both pools: post-authentication and post-confirmation triggers that queue sign-ins and
  password resets for `audit_log` (`assay_infra.auth_events`).
- Managed login (version 2) on a Cognito prefix domain, branded with the logo
  (`assay_infra.branding`, which checks every asset against Cognito's limits); public app
  clients with no secret, authorization code flow with PKCE, access token 60 minutes,
  refresh token 8 hours.
- Email goes through Cognito's built-in sender. That choice lives in `email_configuration()`
  only, so a later switch to SES is a change there and in stack config.
- No custom message templates. Invitations, verification and password-reset codes use
  Cognito's built-in text. The API reference allows a custom email message and subject
  only with SES (`DEVELOPER`), and the provider sends all three invitation fields (email
  message, subject and SMS message) whenever an invitation template is set, so an unset
  SMS message goes out as "" and Cognito rejects the pool. Add a template with SES, and
  then give every field a valid value (see `email_configuration()`).

Neither pool can be reached through the cognito-idp interface endpoint: AWS documents that a
user pool with a domain is incompatible with PrivateLink, and managed login needs a domain.
So the API gets each pool's JWKS at `pulumi up` time (`fetch_jwks`), and a Cognito signing
key rotation needs a redeploy. The trigger runs outside the VPC.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws

from assay_infra.branding import branding_assets, branding_settings
from assay_infra.config import AuthConfig, DnsConfig
from assay_infra.images import ContainerImage

TENANT_GROUPS = ("tenant-admin", "tenant-user")  # assay_api.claims.TENANT_GROUPS
PLATFORM_GROUPS = ("master-admin", "master-user")  # assay_api.claims.PLATFORM_GROUPS
TENANT_ID_ATTRIBUTE = "tenant_id"  # custom:tenant_id, assay_api.claims.TENANT_ID_ATTRIBUTE

# Portal paths on each host. The portal (build step 5) handles both.
CALLBACK_PATH = "/auth/callback"
SIGNED_OUT_PATH = "/"

# Units of the token validities.
MINUTES = "minutes"
HOURS = "hours"
ACCESS_TOKEN_MINUTES = 60
ID_TOKEN_MINUTES = 60
REFRESH_TOKEN_HOURS = 8
TEMPORARY_PASSWORD_DAYS = 7
PASSWORD_MIN_LENGTH = 12
PASSWORD_HISTORY = 5
OAUTH_SCOPES = ("openid", "email", "profile", "aws.cognito.signin.user.admin")
# The only attribute either app client may write (security condition 1, build step 5).
CLIENT_WRITE_ATTRIBUTES = ("name",)

TRIGGER_DOCKERFILE = "api/pre-token.Dockerfile"
TRIGGER_SOURCES = (
    "api/assay_api/__init__.py",
    "api/assay_api/claims.py",
    "api/assay_api/cognito_pre_token.py",
)
# Cognito waits at most 5 seconds for a trigger.
TRIGGER_TIMEOUT_SECONDS = 5
LOG_RETENTION_DAYS = 30

# --- Pure helpers (unit tested) ---------------------------------------------------------


def email_configuration() -> aws.cognito.UserPoolEmailConfigurationArgs:
    """The one place that picks how Cognito sends email (section 4: no SES yet).

    Switching to SES (`DEVELOPER`) is also the point at which custom invitation and
    verification templates become possible. Every invitation template field, the SMS
    message included, must then be valid: 6 to 140 characters with `{username}` and
    `{####}` for SMS, and `{username}` and `{####}` in the email message.
    """
    return aws.cognito.UserPoolEmailConfigurationArgs(email_sending_account="COGNITO_DEFAULT")


def canonical_jwks(document: Mapping[str, Any]) -> str:
    """Keep only the public RS256 signing keys, in a stable form (no spurious diffs)."""
    keys = []
    for key in document.get("keys", []):
        if key.get("kty") != "RSA" or key.get("alg") != "RS256" or key.get("use") != "sig":
            continue
        keys.append({f: key[f] for f in ("alg", "e", "kid", "kty", "n", "use")})
    if not keys:
        raise ValueError("Cognito JWKS holds no RS256 signing key")
    return json.dumps({"keys": sorted(keys, key=lambda k: k["kid"])}, sort_keys=True)


def jwks_url(region: str, pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"


def fetch_jwks(region: str, pool_id: str) -> str:
    """Read a pool's public signing keys. Runs on the machine running `pulumi up`."""
    url = jwks_url(region, pool_id)
    if not url.startswith("https://"):
        raise ValueError(url)
    with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310  # nosec B310
        return canonical_jwks(json.load(response))


JwksFetcher = Callable[[str, str], str]


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def trigger_role_policy(*, log_group: str, user_pool: str) -> str:
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
                    "Sid": "ReadMfaSettings",
                    "Effect": "Allow",
                    "Action": "cognito-idp:AdminGetUser",
                    "Resource": user_pool,
                },
            ],
        }
    )


# --- Components -------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolSpec:
    kind: str  # "tenant" or "platform"
    groups: Sequence[str]
    mfa: str  # "OPTIONAL" or "ON"
    host: str
    domain_prefix: str
    tenant_attribute: bool


class UserPool(pulumi.ComponentResource):
    """One user pool with its groups, managed login domain, app client and branding."""

    def __init__(
        self,
        name: str,
        *,
        spec: PoolSpec,
        repo_root: Path,
        auth_event_lambda_arn: pulumi.Input[str],
        pre_token_lambda_arn: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:auth:UserPool", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        protected = pulumi.ResourceOptions(parent=self, protect=True)

        schemas = []
        if spec.tenant_attribute:
            schemas.append(
                aws.cognito.UserPoolSchemaArgs(
                    name=TENANT_ID_ATTRIBUTE,
                    attribute_data_type="String",
                    mutable=False,
                    required=False,
                    developer_only_attribute=False,
                    string_attribute_constraints=aws.cognito.UserPoolSchemaStringAttributeConstraintsArgs(
                        min_length="36", max_length="36"
                    ),
                )
            )
        # Section 7a: sign-ins and password resets are queued for audit_log by one trigger
        # on both pools (assay_infra.auth_events).
        pre_token = None
        if pre_token_lambda_arn is not None:
            pre_token = aws.cognito.UserPoolLambdaConfigPreTokenGenerationConfigArgs(
                lambda_arn=pre_token_lambda_arn, lambda_version="V2_0"
            )
        lambda_config = aws.cognito.UserPoolLambdaConfigArgs(
            pre_token_generation_config=pre_token,
            post_authentication=auth_event_lambda_arn,
            post_confirmation=auth_event_lambda_arn,
        )

        self.pool = aws.cognito.UserPool(
            f"{name}-pool",
            name=name,
            user_pool_tier="ESSENTIALS",
            deletion_protection="ACTIVE",
            username_attributes=["email"],
            username_configuration=aws.cognito.UserPoolUsernameConfigurationArgs(
                case_sensitive=False
            ),
            auto_verified_attributes=["email"],
            user_attribute_update_settings=aws.cognito.UserPoolUserAttributeUpdateSettingsArgs(
                attributes_require_verification_before_updates=["email"]
            ),
            # No invite_message_template: see the module docstring.
            admin_create_user_config=aws.cognito.UserPoolAdminCreateUserConfigArgs(
                allow_admin_create_user_only=True,
            ),
            password_policy=aws.cognito.UserPoolPasswordPolicyArgs(
                minimum_length=PASSWORD_MIN_LENGTH,
                require_lowercase=True,
                require_uppercase=True,
                require_numbers=True,
                require_symbols=True,
                password_history_size=PASSWORD_HISTORY,
                temporary_password_validity_days=TEMPORARY_PASSWORD_DAYS,
            ),
            account_recovery_setting=aws.cognito.UserPoolAccountRecoverySettingArgs(
                recovery_mechanisms=[
                    aws.cognito.UserPoolAccountRecoverySettingRecoveryMechanismArgs(
                        name="verified_email", priority=1
                    )
                ]
            ),
            mfa_configuration=spec.mfa,
            software_token_mfa_configuration=aws.cognito.UserPoolSoftwareTokenMfaConfigurationArgs(
                enabled=True
            ),
            email_configuration=email_configuration(),
            schemas=schemas or None,
            lambda_config=lambda_config,
            # Protected: replacing a pool deletes every user in it.
            opts=protected,
        )

        self.groups = [
            aws.cognito.UserGroup(
                f"{name}-group-{group}",
                name=group,
                user_pool_id=self.pool.id,
                description=f"Assay Insights {group}",
                precedence=precedence,
                opts=child,
            )
            for precedence, group in enumerate(spec.groups, start=1)
        ]

        self.domain = aws.cognito.UserPoolDomain(
            f"{name}-domain",
            domain=spec.domain_prefix,
            user_pool_id=self.pool.id,
            managed_login_version=2,
            opts=child,
        )

        read_attributes = ["email", "email_verified", "name"]
        if spec.tenant_attribute:
            read_attributes.append(f"custom:{TENANT_ID_ATTRIBUTE}")
        self.client = aws.cognito.UserPoolClient(
            f"{name}-client",
            name=f"{name}-portal",
            user_pool_id=self.pool.id,
            generate_secret=False,
            allowed_oauth_flows_user_pool_client=True,
            allowed_oauth_flows=["code"],
            allowed_oauth_scopes=list(OAUTH_SCOPES),
            supported_identity_providers=["COGNITO"],
            callback_urls=[f"https://{spec.host}{CALLBACK_PATH}"],
            logout_urls=[f"https://{spec.host}{SIGNED_OUT_PATH}"],
            explicit_auth_flows=["ALLOW_REFRESH_TOKEN_AUTH"],
            access_token_validity=ACCESS_TOKEN_MINUTES,
            id_token_validity=ID_TOKEN_MINUTES,
            refresh_token_validity=REFRESH_TOKEN_HOURS,
            token_validity_units=aws.cognito.UserPoolClientTokenValidityUnitsArgs(
                access_token=MINUTES, id_token=MINUTES, refresh_token=HOURS
            ),
            enable_token_revocation=True,
            prevent_user_existence_errors="ENABLED",
            read_attributes=read_attributes,
            # Listed explicitly: an empty list would let the client write every attribute.
            # The access token carries aws.cognito.signin.user.admin, so a user can call
            # UpdateUserAttributes on themselves: only the display name. Never email (it is
            # the sign-in name) and never custom:tenant_id (immutable anyway).
            write_attributes=list(CLIENT_WRITE_ATTRIBUTES),
            opts=child,
        )

        self.branding = aws.cognito.ManagedLoginBranding(
            f"{name}-branding",
            user_pool_id=self.pool.id,
            client_id=self.client.id,
            settings=branding_settings(),
            # Checked against Cognito's limits here, so a bad asset fails the preview.
            assets=[asset.args() for asset in branding_assets(repo_root)],
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.domain]),
        )

        self.register_outputs(
            {"pool_id": self.pool.id, "client_id": self.client.id, "domain": self.domain.domain}
        )


class PreTokenTrigger(pulumi.ComponentResource):
    """The tenant pool's pre-token-generation Lambda (outside the VPC, no database)."""

    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        region: str,
        account_id: pulumi.Input[str],
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:auth:PreTokenTrigger", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)

        self.image = ContainerImage(
            name,
            repo_root=repo_root,
            dockerfile=TRIGGER_DOCKERFILE,
            files=TRIGGER_SOURCES,
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
        self.role = aws.iam.Role(
            f"{name}-role",
            description="Tenant pool pre-token trigger: tenant_id and mfa claims",
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
        self.function = aws.lambda_.Function(
            f"{name}-fn",
            name=name,
            description="Tenant pool pre-token trigger: adds tenant_id and mfa to access tokens",
            role=self.role.arn,
            package_type="Image",
            image_uri=self.image.image_uri,
            architectures=["x86_64"],
            memory_size=256,
            timeout=TRIGGER_TIMEOUT_SECONDS,
            logging_config=aws.lambda_.FunctionLoggingConfigArgs(
                log_format="JSON",
                log_group=self.log_group.name,
                application_log_level="INFO",
                system_log_level="WARN",
            ),
            opts=child,
        )
        self.register_outputs({"function_name": self.function.name})

    def attach(self, name: str, pool: aws.cognito.UserPool, account_id: pulumi.Input[str]) -> None:
        """Let Cognito invoke the trigger, and let the trigger read this pool's MFA settings."""
        child = pulumi.ResourceOptions(parent=self)
        aws.iam.RolePolicy(
            f"{name}-policy",
            role=self.role.id,
            policy=pulumi.Output.all(log_group=self.log_group.arn, user_pool=pool.arn).apply(
                lambda a: trigger_role_policy(**a)
            ),
            opts=child,
        )
        aws.lambda_.Permission(
            f"{name}-invoke",
            action="lambda:InvokeFunction",
            function=self.function.name,
            principal="cognito-idp.amazonaws.com",
            source_arn=pool.arn,
            source_account=account_id,
            opts=child,
        )


class Auth(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        cfg: AuthConfig,
        dns: DnsConfig,
        region: str,
        account_id: pulumi.Input[str],
        repo_root: Path,
        # The post-authentication and post-confirmation trigger (assay_infra.auth_events).
        auth_event_lambda_arn: pulumi.Input[str],
        aws_profile: str | None = None,
        jwks_fetcher: JwksFetcher = fetch_jwks,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:auth:Auth", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.pre_token = PreTokenTrigger(
            f"{name}-pre-token",
            repo_root=repo_root,
            region=region,
            account_id=account_id,
            aws_profile=aws_profile,
            opts=child,
        )
        self.tenant = UserPool(
            f"{name}-tenant",
            spec=PoolSpec(
                kind="tenant",
                groups=TENANT_GROUPS,
                mfa="OPTIONAL",
                host=dns.tenant_host,
                domain_prefix=cfg.tenant_domain_prefix,
                tenant_attribute=True,
            ),
            repo_root=repo_root,
            auth_event_lambda_arn=auth_event_lambda_arn,
            pre_token_lambda_arn=self.pre_token.function.arn,
            opts=child,
        )
        self.pre_token.attach(f"{name}-pre-token-tenant", self.tenant.pool, account_id)
        self.platform = UserPool(
            f"{name}-platform",
            spec=PoolSpec(
                kind="platform",
                groups=PLATFORM_GROUPS,
                mfa="ON",
                host=dns.admin_host,
                domain_prefix=cfg.platform_domain_prefix,
                tenant_attribute=False,
            ),
            repo_root=repo_root,
            auth_event_lambda_arn=auth_event_lambda_arn,
            opts=child,
        )

        self.tenant_jwks = self.tenant.pool.id.apply(lambda pid: jwks_fetcher(region, pid))
        self.platform_jwks = self.platform.pool.id.apply(lambda pid: jwks_fetcher(region, pid))

        self.register_outputs(
            {
                "tenant_pool_id": self.tenant.pool.id,
                "platform_pool_id": self.platform.pool.id,
            }
        )
