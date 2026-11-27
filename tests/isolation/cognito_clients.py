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
3. **Refresh-token rotation and programmatic sign-in** (build step 6, second half; owner
   decision of 2026-11-25, after this run's earlier probe). A portal client with rotation on
   has ALLOW_CUSTOM_AUTH as its only explicit flow: Cognito refuses rotation together with
   the refresh flow, and an empty flow list still accepts an SRP sign-in. Custom auth cannot
   start in a pool without custom-auth triggers, so no programmatic sign-in is left. Checked
   here, against Cognito:
   - each deployed client has the flows and rotation setting the stack says it should;
   - neither deployed pool has a define, create or verify auth-challenge trigger;
   - each deployed client refuses all four programmatic sign-ins (password, SRP, custom,
     `USER_AUTH`) because the flow is not enabled, asked for a user that does not exist, so
     nothing is signed in and no trigger runs;
   - on the replica, whose client follows the tenant client: a refresh returns a new
     refresh token; the old `REFRESH_TOKEN_AUTH` flow answers `UnsupportedOperationException`,
     for a valid token and for one that is not valid alike; the old refresh token, used
     again after the grace period, answers `RefreshTokenReuseException`; and that reuse
     revokes nothing else (the new refresh token and the access token go on working). Each
     answer is required by name: another refusal fails the check;
   - on the replica, the way back: a client with rotation on is changed in one update to
     rotation off with the refresh flow restored, which is what the stack's switch does.
"""

from __future__ import annotations

import re
import secrets
import time
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
ROTATION_GRACE_SECONDS = 10
ROTATION = {"Feature": "ENABLED", "RetryGracePeriodSeconds": ROTATION_GRACE_SECONDS}
# A portal client's only explicit flow, with rotation on and without (assay_infra.auth).
ROTATED_FLOWS = ["ALLOW_CUSTOM_AUTH"]
UNROTATED_FLOWS = ["ALLOW_REFRESH_TOKEN_AUTH"]
# What keeps ALLOW_CUSTOM_AUTH from being a sign-in: a pool with none of these.
CUSTOM_AUTH_TRIGGERS = ("DefineAuthChallenge", "CreateAuthChallenge", "VerifyAuthChallengeResponse")
PROGRAMMATIC_FLOWS = ("USER_PASSWORD_AUTH", "USER_SRP_AUTH", "CUSTOM_AUTH", "USER_AUTH")
# Cognito's words when a flow cannot be used with a client at all.
FLOW_NOT_AVAILABLE = re.compile(r"not enabled|not configured", re.IGNORECASE)
FLOW_REFUSED = "flow refused"
# Cognito's answers on a client with rotation on, by name. Each is required exactly: any
# other answer, another refusal included, fails its check (owner, 2026-11-27, after the first
# run against a rotated client met both and the checks, written for a guess, failed).
#
# - The old refresh flow (`InitiateAuth` with `REFRESH_TOKEN_AUTH`). AWS: "You can't
#   authenticate with REFRESH_TOKEN_AUTH in app clients with refresh token rotation enabled",
#   and of the exception, "thrown when you attempt to perform an operation that isn't enabled
#   for the user pool client." AWS does not say whether the token is looked at first, so a
#   check asks with a token that is not valid and requires the same answer.
OLD_FLOW_NOT_ENABLED = "UnsupportedOperationException"
# - A refresh token used again after it was rotated out and its grace period is over. AWS:
#   "thrown when your application requests token refresh with a refresh token that has been
#   invalidated by refresh-token rotation." AWS does not say that anything else is revoked,
#   so checks require the new refresh token and the access token to go on working.
ROTATED_OUT = "RefreshTokenReuseException"
ALLOWED = "allowed"


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


def _answer(call: Callable[[], object]) -> str:
    """Cognito's answer by name: `allowed`, or the error's code exactly as Cognito gives it."""
    try:
        call()
    except ClientError as exc:
        return str(exc.response.get("Error", {}).get("Code", "")) or "an error with no code"
    return ALLOWED


def _altered(token: str) -> str:
    """A refresh token that is not valid: the real one with one character changed, so it has
    the right shape and length and no longer decrypts."""
    middle = len(token) // 2
    other = "A" if token[middle] != "A" else "B"
    return token[:middle] + other + token[middle + 1 :]


def _flow_answer(call: Callable[[], Any]) -> str:
    """`flow refused` only if Cognito said the flow cannot be used with this client. Any other
    error is reported by its code and fails the check: it is not known to be a refusal of the
    flow itself, which is what a portal client must give."""
    try:
        response = call()
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code, message = str(error.get("Code", "")), str(error.get("Message", ""))
        if code == "InvalidParameterException" and FLOW_NOT_AVAILABLE.search(message):
            return FLOW_REFUSED
        return f"not refused as a flow ({code})"
    if "AuthenticationResult" in response:
        return "ALLOWED: tokens issued"
    return f"ALLOWED: challenge {response.get('ChallengeName', 'unknown')}"


def _sign_in(
    client: Any, client_id: str, flow: str, parameters: dict[str, str]
) -> Callable[[], Any]:
    return lambda: client.initiate_auth(
        ClientId=client_id, AuthFlow=flow, AuthParameters=parameters
    )


def sign_in_checks(as_anyone: Any, label: str, client_id: str) -> list[Check]:
    """All four programmatic sign-ins, for a user that does not exist: nobody is signed in
    and no trigger runs, whatever the answer. Each must be refused as a flow."""
    name = f"nobody-{secrets.token_hex(6)}@example.invalid"
    parameters: dict[str, dict[str, str]] = {
        "USER_PASSWORD_AUTH": {"USERNAME": name, "PASSWORD": f"Aa1!{secrets.token_urlsafe(12)}"},
        "USER_SRP_AUTH": {"USERNAME": name, "SRP_A": "a" * 512},
        "CUSTOM_AUTH": {"USERNAME": name},
        "USER_AUTH": {"USERNAME": name},
    }
    return [
        Check(
            f"{label}: programmatic sign-in with {flow}",
            FLOW_REFUSED,
            _flow_answer(_sign_in(as_anyone, client_id, flow, parameters[flow])),
        )
        for flow in PROGRAMMATIC_FLOWS
    ]


def _rotation_text(client: dict[str, Any]) -> str:
    rotation = client.get("RefreshTokenRotation") or {}
    if rotation.get("Feature") == "ENABLED":
        return f"on, {rotation.get('RetryGracePeriodSeconds')} second grace"
    return "off"


def deployed_clients(
    cognito: Any,
    as_anyone: Any,
    clients: dict[str, tuple[str, str]],
    rotated: dict[str, bool],
) -> list[Check]:
    """The two portal app clients as they are deployed. `rotated` says which of them the
    stack means to have refresh-token rotation on."""
    checks = []
    for kind, (pool_id, client_id) in sorted(clients.items()):
        client = cognito.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)[
            "UserPoolClient"
        ]
        label = f"{kind} portal client"
        writes = sorted(client.get("WriteAttributes", []))
        checks.append(
            Check(f"{label} writes only {ALLOWED_WRITES}", str(ALLOWED_WRITES), str(writes))
        )
        on = rotated.get(kind, False)
        checks.append(
            Check(
                f"{label}: explicit flows",
                str(ROTATED_FLOWS if on else UNROTATED_FLOWS),
                str(sorted(client.get("ExplicitAuthFlows", []))),
            )
        )
        checks.append(
            Check(
                f"{label}: refresh-token rotation",
                f"on, {ROTATION_GRACE_SECONDS} second grace" if on else "off",
                _rotation_text(client),
            )
        )
        triggers = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"].get(
            "LambdaConfig", {}
        )
        checks.append(
            Check(
                f"{kind} pool has no custom-auth trigger",
                "[]",
                str(sorted(set(triggers) & set(CUSTOM_AUTH_TRIGGERS))),
            )
        )
        checks += sign_in_checks(as_anyone, label, client_id)
    return checks


def refresh_checks(
    cognito: Any, as_anyone: Any, *, client_id: str, refresh_token: str
) -> list[Check]:
    """On a client with rotation on, in order:

    1. a refresh gives a new refresh token;
    2. the old refresh flow answers `UnsupportedOperationException`, for the user's current
       refresh token and, just the same, for a token that is not valid: so the answer is
       about the client and never about the token;
    3. once the grace period is over, the old refresh token answers
       `RefreshTokenReuseException`;
    4. and that reuse revoked nothing else: the new refresh token still refreshes, and the
       access token issued before the reuse is still accepted. (Two tabs that share one
       refresh token: the tab that refreshes second is sent to sign-in; the first stays
       signed in.)

    Every expected answer is an exact name. Any other answer fails.
    """
    checks = []
    refreshed = as_anyone.get_tokens_from_refresh_token(
        RefreshToken=refresh_token, ClientId=client_id
    )["AuthenticationResult"]
    new_token = refreshed.get("RefreshToken")
    rotated = bool(new_token) and new_token != refresh_token
    checks.append(
        Check(
            "replica: a refresh returns a new refresh token",
            "a new token",
            "a new token" if rotated else "the same token, or none",
        )
    )
    current = new_token if rotated else refresh_token

    def old_flow(token: str) -> str:
        return _answer(
            _sign_in(as_anyone, client_id, "REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": token})
        )

    checks.append(
        Check(
            "replica: REFRESH_TOKEN_AUTH with the user's current refresh token",
            OLD_FLOW_NOT_ENABLED,
            old_flow(current),
        )
    )
    checks.append(
        Check(
            "replica: REFRESH_TOKEN_AUTH with a token that is not valid gets the same answer",
            OLD_FLOW_NOT_ENABLED,
            old_flow(_altered(current)),
        )
    )
    time.sleep(ROTATION_GRACE_SECONDS + 3)
    checks.append(
        Check(
            "replica: the old refresh token, used again after the grace period",
            ROTATED_OUT,
            _answer(
                lambda: as_anyone.get_tokens_from_refresh_token(
                    RefreshToken=refresh_token, ClientId=client_id
                )
            ),
        )
    )
    if rotated:
        # After the reuse above, on purpose: what it left standing.
        checks.append(
            Check(
                "replica: after that reuse, the new refresh token still refreshes",
                ALLOWED,
                _answer(
                    lambda: as_anyone.get_tokens_from_refresh_token(
                        RefreshToken=new_token, ClientId=client_id
                    )
                ),
            )
        )
        checks.append(
            Check(
                "replica: after that reuse, the access token issued before it is still accepted",
                ALLOWED,
                _answer(lambda: as_anyone.get_user(AccessToken=refreshed["AccessToken"])),
            )
        )
    return checks


def way_back_rehearsal(cognito: Any, *, pool_id: str, real_client: dict[str, Any]) -> Check:
    """The rollback, rehearsed on the replica pool: a client with rotation on and
    ALLOW_CUSTOM_AUTH is changed, in one update, to rotation explicitly off with the refresh
    flow back. That is the update `tenantRefreshTokenRotation: "false"` makes on a real
    client, so if sign-in ever breaks the way back is known to be open."""
    attributes = {
        "ReadAttributes": real_client.get("ReadAttributes", []),
        "WriteAttributes": real_client.get("WriteAttributes", []),
    }
    try:
        client_id = cognito.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName="way-back-rehearsal",
            GenerateSecret=False,
            ExplicitAuthFlows=ROTATED_FLOWS,
            RefreshTokenRotation=ROTATION,
            **attributes,
        )["UserPoolClient"]["ClientId"]
        after = cognito.update_user_pool_client(
            UserPoolId=pool_id,
            ClientId=client_id,
            ClientName="way-back-rehearsal",
            ExplicitAuthFlows=UNROTATED_FLOWS,
            RefreshTokenRotation={"Feature": "DISABLED"},
            **attributes,
        )["UserPoolClient"]
        outcome = f"{sorted(after.get('ExplicitAuthFlows', []))}, rotation {_rotation_text(after)}"
    except ClientError as exc:
        error = exc.response.get("Error", {})
        outcome = f"refused ({error.get('Code', '')}: {str(error.get('Message', ''))[:120]})"
    return Check(
        "replica: the way back (rotation off, the refresh flow restored) is accepted in one update",
        f"{UNROTATED_FLOWS}, rotation off",
        outcome,
    )


def replica_attempt(
    session: boto3.Session,
    cognito: Any,
    *,
    tenant_pool: str,
    tenant_client: str,
    name_prefix: str,
) -> list[Check]:
    real_pool = cognito.describe_user_pool(UserPoolId=tenant_pool)["UserPool"]
    real_client = cognito.describe_user_pool_client(UserPoolId=tenant_pool, ClientId=tenant_client)[
        "UserPoolClient"
    ]
    # The replica's client follows the deployed tenant client: its flows, and its rotation.
    rotated = (real_client.get("RefreshTokenRotation") or {}).get("Feature") == "ENABLED"
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
            # The deployed tenant client's flows and rotation, plus admin password sign-in,
            # which only this probe uses.
            ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", *real_client["ExplicitAuthFlows"]],
            **({"RefreshTokenRotation": ROTATION} if rotated else {}),
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
        signed_in = cognito.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": email, "PASSWORD": password},
        )["AuthenticationResult"]
        token = signed_in["AccessToken"]
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
        checks.append(way_back_rehearsal(cognito, pool_id=pool_id, real_client=real_client))
        if rotated:
            checks += refresh_checks(
                cognito, as_user, client_id=client_id, refresh_token=signed_in["RefreshToken"]
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
    rotated: dict[str, bool],
) -> list[Check]:
    """`rotated`: which of the two clients the stack means to have refresh-token rotation on
    (stack outputs `tenant_refresh_token_rotation` and `platform_refresh_token_rotation`)."""
    cognito = session.client("cognito-idp")
    as_anyone = session.client("cognito-idp", config=Config(signature_version=UNSIGNED))
    checks = deployed_clients(cognito, as_anyone, {"tenant": tenant, "platform": platform}, rotated)
    checks += replica_attempt(
        session, cognito, tenant_pool=tenant[0], tenant_client=tenant[1], name_prefix=name_prefix
    )
    return checks
