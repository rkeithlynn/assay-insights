"""Can a signed-in user rewrite their own email or tenant? (Owner condition 1, build step 5.)

The portal's access tokens carry `aws.cognito.signin.user.admin`, so a user can call
Cognito's `UpdateUserAttributes` on themselves with their own token. That is safe only if
neither app client may write `email` or `custom:tenant_id` (the clients may write `name`
only). Run from the owner's machine by `scripts/run_isolation.py`, because Cognito cannot be
reached from the VPC.

1. **The deployed clients.** Both portal app clients' write attributes are exactly `name`.
2. **The attempt.** The portal clients allow no programmatic sign-in, and a sign-in on the
   real tenant pool would fire its audit trigger for a user the database does not know. So
   the attempt runs against a throwaway replica: a user pool with the tenant pool's immutable
   `custom:tenant_id` and an app client with the deployed portal client's read and write
   attributes (plus admin password sign-in, which only this probe uses). A user signs in,
   and with their own access token tries to change `custom:tenant_id` and `email`. Both must
   be refused and leave the value unchanged. Changing `name` must succeed, so a run in which
   every call fails cannot pass. The replica is deleted afterwards.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

TENANT_ATTRIBUTE = "custom:tenant_id"
ALLOWED_WRITES = ["name"]
REFUSED_CODES = frozenset({"NotAuthorizedException", "InvalidParameterException"})


@dataclass(frozen=True)
class Check:
    check: str
    expected: str
    outcome: str

    @property
    def ok(self) -> bool:
        return self.expected == self.outcome

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "expected": self.expected,
            "outcome": self.outcome,
            "ok": self.ok,
        }


def _attempt(call: Callable[[], object]) -> str:
    try:
        call()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        return "refused" if code in REFUSED_CODES else f"error {code}"
    return "allowed"


def deployed_clients(cognito: Any, clients: dict[str, tuple[str, str]]) -> list[Check]:
    checks = []
    for kind, (pool_id, client_id) in sorted(clients.items()):
        client = cognito.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
        writes = sorted(client["UserPoolClient"].get("WriteAttributes", []))
        checks.append(
            Check(
                f"{kind} portal client writes only {ALLOWED_WRITES}",
                str(ALLOWED_WRITES),
                str(writes),
            )
        )
    return checks


def replica_attempt(
    session: boto3.Session, cognito: Any, *, tenant_pool: str, tenant_client: str, name_prefix: str
) -> list[Check]:
    real_pool = cognito.describe_user_pool(UserPoolId=tenant_pool)["UserPool"]
    real_client = cognito.describe_user_pool_client(UserPoolId=tenant_pool, ClientId=tenant_client)[
        "UserPoolClient"
    ]
    schema = [a for a in real_pool.get("SchemaAttributes", []) if a["Name"] == TENANT_ATTRIBUTE]
    if not schema or schema[0].get("Mutable", True):
        return [
            Check("tenant pool's custom:tenant_id is immutable", "immutable", "mutable or missing")
        ]
    attribute = schema[0]
    replica = cognito.create_user_pool(
        PoolName=f"{name_prefix}-isolation-replica-{secrets.token_hex(4)}",
        UsernameAttributes=["email"],
        DeletionProtection="INACTIVE",
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        Schema=[
            {
                "Name": "tenant_id",
                "AttributeDataType": "String",
                "Mutable": False,
                "DeveloperOnlyAttribute": False,
                "StringAttributeConstraints": attribute.get(
                    "StringAttributeConstraints", {"MinLength": "36", "MaxLength": "36"}
                ),
            }
        ],
        UserPoolTags={"app": "assay-insights", "purpose": "isolation-tests"},
    )["UserPool"]
    pool_id = replica["Id"]
    checks: list[Check] = []
    try:
        client_id = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName="replica-of-portal-client",
            GenerateSecret=False,
            ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            ReadAttributes=real_client.get("ReadAttributes", []),
            WriteAttributes=real_client.get("WriteAttributes", []),
        )["UserPoolClient"]["ClientId"]
        email = f"probe-{secrets.token_hex(4)}@example.invalid"
        tenant_id = str(uuid.uuid4())
        password = f"Aa1!{secrets.token_urlsafe(24)}"
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": "Probe"},
                {"Name": TENANT_ATTRIBUTE, "Value": tenant_id},
            ],
        )
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=email, Password=password, Permanent=True
        )
        token = cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]["AccessToken"]
        # The user's own call: authorized by the access token only, never by IAM.
        as_user = session.client("cognito-idp", config=Config(signature_version=UNSIGNED))

        def update(name: str, value: str) -> Callable[[], object]:
            return lambda: as_user.update_user_attributes(
                AccessToken=token, UserAttributes=[{"Name": name, "Value": value}]
            )

        checks.append(
            Check(
                "user changes own custom:tenant_id with own token",
                "refused",
                _attempt(update(TENANT_ATTRIBUTE, str(uuid.uuid4()))),
            )
        )
        checks.append(
            Check(
                "user changes own email with own token",
                "refused",
                _attempt(update("email", f"other-{secrets.token_hex(4)}@example.invalid")),
            )
        )
        checks.append(
            Check(
                "user changes own display name with own token",
                "allowed",
                _attempt(update("name", "Renamed")),
            )
        )
        after = {
            a["Name"]: a["Value"]
            for a in cognito.admin_get_user(UserPoolId=pool_id, Username=email)["UserAttributes"]
        }
        checks.append(
            Check("custom:tenant_id unchanged", tenant_id, after.get(TENANT_ATTRIBUTE, ""))
        )
        checks.append(Check("email unchanged", email, after.get("email", "")))
    finally:
        cognito.delete_user_pool(UserPoolId=pool_id)
    return checks


def run(
    session: boto3.Session,
    *,
    tenant: tuple[str, str],
    platform: tuple[str, str],
    name_prefix: str,
) -> list[Check]:
    cognito = session.client("cognito-idp")
    checks = deployed_clients(cognito, {"tenant": tenant, "platform": platform})
    checks += replica_attempt(
        session, cognito, tenant_pool=tenant[0], tenant_client=tenant[1], name_prefix=name_prefix
    )
    return checks
