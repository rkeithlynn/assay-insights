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
3. **The refresh-token rotation probe** (build step 6, second half; not pass or fail). Before
   rotation is switched on for the real app clients, the owner needs to see what Cognito does
   with a client that has rotation on and no refresh flow: AWS documents that rotation
   "isn't compatible with" `REFRESH_TOKEN_AUTH`, and that a client with no explicit flows
   gets three by default, the refresh flow and two programmatic sign-ins among them. So the
   replica pool gets one more client per candidate setting, and the run prints what Cognito
   stored for each and how it answers each kind of programmatic sign-in. The portal clients
   allow none today, and must allow none afterwards.
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
ROTATION = {"Feature": "ENABLED", "RetryGracePeriodSeconds": 10}
# What the real clients could be given once the refresh flow is removed.
ROTATION_CANDIDATES: tuple[tuple[str, list[str]], ...] = (
    ("no explicit flows", []),
    ("ALLOW_CUSTOM_AUTH only (no custom-auth trigger exists)", ["ALLOW_CUSTOM_AUTH"]),
    ("the refresh flow kept", ["ALLOW_REFRESH_TOKEN_AUTH"]),
)


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


def _answer(call: Callable[[], Any]) -> str:
    """How Cognito answered, in its own words (an error's code and message, never a token)."""
    try:
        response = call()
    except ClientError as exc:
        error = exc.response.get("Error", {})
        return f"refused: {error.get('Code', '')}: {str(error.get('Message', ''))[:140]}"
    if "AuthenticationResult" in response:
        return "ALLOWED: tokens issued"
    return f"ALLOWED: challenge {response.get('ChallengeName', 'unknown')}"


def _sign_in(
    client: Any, client_id: str, flow: str, parameters: dict[str, str]
) -> Callable[[], Any]:
    return lambda: client.initiate_auth(
        ClientId=client_id, AuthFlow=flow, AuthParameters=parameters
    )


def rotation_probe(
    session: boto3.Session,
    cognito: Any,
    *,
    pool_id: str,
    real_client: dict[str, Any],
    email: str,
    password: str,
) -> list[str]:
    """What Cognito stores and allows for each candidate client. Lines to print."""
    as_anyone = session.client("cognito-idp", config=Config(signature_version=UNSIGNED))
    lines: list[str] = []
    for label, flows in ROTATION_CANDIDATES:
        try:
            created = cognito.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName=f"rotation-probe-{len(lines)}",
                GenerateSecret=False,
                ExplicitAuthFlows=flows,
                RefreshTokenRotation=ROTATION,
                ReadAttributes=real_client.get("ReadAttributes", []),
                WriteAttributes=real_client.get("WriteAttributes", []),
            )["UserPoolClient"]
        except ClientError as exc:
            error = exc.response.get("Error", {})
            lines.append(
                f"{label}: Cognito refused the client "
                f"({error.get('Code', '')}: {str(error.get('Message', ''))[:160]})"
            )
            continue
        stored = sorted(created.get("ExplicitAuthFlows", []))
        lines.append(
            f"{label}: Cognito stored flows {stored}, rotation "
            f"{created.get('RefreshTokenRotation')}"
        )
        attempts: tuple[tuple[str, dict[str, str]], ...] = (
            ("USER_PASSWORD_AUTH", {"USERNAME": email, "PASSWORD": password}),
            ("USER_SRP_AUTH", {"USERNAME": email, "SRP_A": "a" * 512}),
            ("CUSTOM_AUTH", {"USERNAME": email}),
            ("USER_AUTH", {"USERNAME": email}),
        )
        for flow, parameters in attempts:
            answer = _answer(_sign_in(as_anyone, created["ClientId"], flow, parameters))
            lines.append(f"    sign-in with {flow}: {answer}")
    return lines


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
    session: boto3.Session,
    cognito: Any,
    *,
    tenant_pool: str,
    tenant_client: str,
    name_prefix: str,
    probe: list[str] | None = None,
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
        if probe is not None:
            probe += rotation_probe(
                session,
                cognito,
                pool_id=pool_id,
                real_client=real_client,
                email=email,
                password=password,
            )
    finally:
        cognito.delete_user_pool(UserPoolId=pool_id)
    return checks


def run(
    session: boto3.Session,
    *,
    tenant: tuple[str, str],
    platform: tuple[str, str],
    name_prefix: str,
    probe: list[str] | None = None,
) -> list[Check]:
    """`probe`, if given, receives the rotation probe's lines (see the module docstring)."""
    cognito = session.client("cognito-idp")
    checks = deployed_clients(cognito, {"tenant": tenant, "platform": platform})
    checks += replica_attempt(
        session,
        cognito,
        tenant_pool=tenant[0],
        tenant_client=tenant[1],
        name_prefix=name_prefix,
        probe=probe,
    )
    return checks
