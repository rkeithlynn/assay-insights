"""Cognito admin Lambda: user-pool changes that cannot be made from inside the VPC.

A user pool with a managed login domain cannot be reached through PrivateLink, so this code
runs outside the VPC and is invoked through the `lambda` interface endpoint. It has no
database access. Two functions run this one image, each limited by `OPERATIONS`:

- `<prefix>-cognito-admin` (build step 4): `invite` only, invoked only by the provisioning
  role, to invite a new tenant's first admin.
- `<prefix>-cognito-admin-api` (build step 5): the API's user operations, invoked only by
  the API task role and the seed Lambda.

The caller decides who may do what (the API enforces every permission). This function
checks, independently, that the target is who the caller says:

- Every tenant-pool operation carries a `tenant_id`. Before acting on an existing user it
  reads the user and refuses unless the user's immutable `custom:tenant_id` equals it and
  the user's `sub` equals the `sub` the caller holds for them. A mismatch is reported as
  `user_not_found`, never which tenant the user belongs to.
- The only platform-pool operation is `reset_mfa`, and only for a user in the
  `master-user` group. No operation creates a platform-pool user.
- `mfa_status` only reads: whether a tenant-pool user has TOTP MFA turned on, so the API
  records an MFA enrolment only when Cognito confirms it.

`custom:tenant_id` is immutable and decides which tenant the user's tokens belong to. It is
set once, at creation (`invite`), from the `tenant_id` in the event, which the API takes
from the verified token and the provisioning task from the tenant row.

A repeated invitation is safe: an existing user of the same tenant is returned as it is. An
address that already belongs to another tenant is refused, without saying which one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from assay_api.claims import PLATFORM_GROUPS, TENANT_GROUPS, TENANT_ID_ATTRIBUTE

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient

log = logging.getLogger()
log.setLevel(logging.INFO)

ALL_OPERATIONS = frozenset(
    {
        "invite",
        "resend_invite",
        "reset_password",
        "disable",
        "enable",
        "set_role",
        "reset_mfa",
        "mfa_status",
    }
)
# The only operation allowed on the platform pool, and the only group it may target there.
PLATFORM_OPERATIONS = frozenset({"reset_mfa"})
PLATFORM_MFA_RESET_GROUP = "master-user"
POOLS = ("tenant", "platform")
TOTP = "SOFTWARE_TOKEN_MFA"

CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 3})
MAX_NAME = 200
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")

# Returned to the caller as {"error": code}. Never Cognito's own message.
EMAIL_IN_USE = "email_in_use"
INVALID_REQUEST = "invalid_request"
NOT_ALLOWED = "operation_not_allowed"
USER_NOT_FOUND = "user_not_found"
WRONG_STATE = "wrong_state"

_client: CognitoIdentityProviderClient | None = None


class InvalidRequest(ValueError):
    """The event is not a request this function serves."""


class Refused(Exception):
    """The request is valid but cannot be carried out. `code` goes back to the caller."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def cognito() -> CognitoIdentityProviderClient:
    global _client  # one client per execution environment
    if _client is None:
        _client = boto3.client("cognito-idp", config=CLIENT_CONFIG)
    return _client


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _attribute(attributes: list[Any], name: str) -> str | None:
    for attribute in attributes:
        if attribute.get("Name") == name:
            return str(attribute.get("Value"))
    return None


def allowed_operations(env: dict[str, str] | None = None) -> frozenset[str]:
    """The operations this function serves, from `OPERATIONS` (comma-separated)."""
    raw = (env if env is not None else os.environ).get("OPERATIONS", "")
    names = frozenset(n.strip() for n in raw.split(",") if n.strip())
    unknown = names - ALL_OPERATIONS
    if unknown or not names:
        raise RuntimeError(f"OPERATIONS is empty or names unknown operations {sorted(unknown)}")
    return names


def pool_id(pool: str) -> str:
    return os.environ["TENANT_POOL_ID"] if pool == "tenant" else os.environ["PLATFORM_POOL_ID"]


# --- Parsing ----------------------------------------------------------------------------


def _email(event: dict[str, Any]) -> str:
    email = str(event.get("email") or "").strip()
    if not _EMAIL.match(email):
        raise InvalidRequest("email address is not valid")
    return email


def _tenant_id(event: dict[str, Any]) -> str:
    try:
        return str(uuid.UUID(str(event["tenant_id"])))
    except KeyError, ValueError:
        raise InvalidRequest("tenant-pool operations need a tenant_id") from None


def _sub(event: dict[str, Any]) -> str:
    sub = str(event.get("sub") or "")
    if not sub or len(sub) > 128:
        raise InvalidRequest("operation needs the user's sub")
    return sub


def _tenant_role(event: dict[str, Any]) -> str:
    role = str(event.get("role") or "")
    if role not in TENANT_GROUPS:
        raise InvalidRequest("role must be a tenant-pool group")
    return role


def parse_invite(event: dict[str, Any]) -> tuple[str, str, str, str]:
    """(tenant_id, email, name, group), validated."""
    try:
        tenant_id = str(uuid.UUID(str(event["tenant_id"])))
        email = str(event["email"]).strip()
        name = " ".join(str(event["name"]).split())
        group = str(event["role"])
    except KeyError, ValueError:
        raise InvalidRequest("invite needs tenant_id, email, name and role") from None
    if not _EMAIL.match(email):
        raise InvalidRequest("email address is not valid")
    if not name or len(name) > MAX_NAME:
        raise InvalidRequest("name must be 1 to 200 characters")
    if group not in TENANT_GROUPS:
        raise InvalidRequest("role must be a tenant-pool group")
    return tenant_id, email, name, group


@dataclass(frozen=True)
class Target:
    """An existing user, identified by the caller and confirmed against Cognito."""

    pool: str
    pool_id: str
    username: str  # the email address (the pools sign in by email)
    groups: tuple[str, ...]
    enabled: bool
    status: str  # Cognito's UserStatus, for example FORCE_CHANGE_PASSWORD or CONFIRMED
    totp: bool  # TOTP MFA is turned on (in the user's MFA settings)


def confirm_target(event: dict[str, Any]) -> Target:
    """Read the user and refuse unless it is exactly the user the caller described."""
    pool = str(event.get("pool") or "")
    if pool not in POOLS:
        raise InvalidRequest("pool must be tenant or platform")
    email = _email(event)
    sub = _sub(event)
    tenant_id = _tenant_id(event) if pool == "tenant" else None
    pid = pool_id(pool)
    client = cognito()
    try:
        user = client.admin_get_user(UserPoolId=pid, Username=email)
    except ClientError as exc:
        if _code(exc) == "UserNotFoundException":
            raise Refused(USER_NOT_FOUND) from None
        raise
    attributes = list(user.get("UserAttributes", []))
    if _attribute(attributes, "sub") != sub:
        raise Refused(USER_NOT_FOUND)
    if pool == "tenant" and _attribute(attributes, TENANT_ID_ATTRIBUTE) != tenant_id:
        raise Refused(USER_NOT_FOUND)
    if pool == "platform" and _attribute(attributes, TENANT_ID_ATTRIBUTE) is not None:
        raise Refused(USER_NOT_FOUND)
    groups = tuple(
        g["GroupName"]
        for g in client.admin_list_groups_for_user(UserPoolId=pid, Username=email).get("Groups", [])
    )
    expected_groups = TENANT_GROUPS if pool == "tenant" else PLATFORM_GROUPS
    if any(g not in expected_groups for g in groups):
        raise Refused(USER_NOT_FOUND)
    return Target(
        pool=pool,
        pool_id=pid,
        username=email,
        groups=groups,
        enabled=bool(user.get("Enabled", True)),
        status=str(user.get("UserStatus", "")),
        totp=TOTP in user.get("UserMFASettingList", []),
    )


# --- Operations -------------------------------------------------------------------------


def invite(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("pool", "tenant") != "tenant":
        raise InvalidRequest("invite creates tenant-pool users only")
    tenant_id, email, name, group = parse_invite(event)
    pid = pool_id("tenant")
    client = cognito()
    created = True
    try:
        response = client.admin_create_user(
            UserPoolId=pid,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": name},
                {"Name": TENANT_ID_ATTRIBUTE, "Value": tenant_id},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        attributes: list[Any] = list(response["User"].get("Attributes", []))
    except ClientError as exc:
        if _code(exc) != "UsernameExistsException":
            raise
        created = False
        existing = client.admin_get_user(UserPoolId=pid, Username=email)
        attributes = list(existing.get("UserAttributes", []))
        if _attribute(attributes, TENANT_ID_ATTRIBUTE) != tenant_id:
            raise Refused(EMAIL_IN_USE) from None
    sub = _attribute(attributes, "sub")
    if not sub:
        raise RuntimeError("Cognito returned a user without a sub")
    # Idempotent: adding a user to a group it is already in succeeds.
    client.admin_add_user_to_group(UserPoolId=pid, Username=email, GroupName=group)
    log.info(
        json.dumps(
            {"message": "user invited", "tenant_id": tenant_id, "group": group, "created": created}
        )
    )
    return {"sub": sub, "created": created}


def _tenant_only(target: Target) -> None:
    if target.pool != "tenant":
        raise Refused(NOT_ALLOWED)


def resend_invite(event: dict[str, Any]) -> dict[str, Any]:
    target = confirm_target(event)
    _tenant_only(target)
    if target.status != "FORCE_CHANGE_PASSWORD":
        raise Refused(WRONG_STATE)
    cognito().admin_create_user(
        UserPoolId=target.pool_id,
        Username=target.username,
        MessageAction="RESEND",
        DesiredDeliveryMediums=["EMAIL"],
    )
    return {"ok": True}


def reset_password(event: dict[str, Any]) -> dict[str, Any]:
    target = confirm_target(event)
    _tenant_only(target)
    if target.status == "FORCE_CHANGE_PASSWORD" or not target.enabled:
        raise Refused(WRONG_STATE)
    cognito().admin_reset_user_password(UserPoolId=target.pool_id, Username=target.username)
    return {"ok": True}


def disable(event: dict[str, Any]) -> dict[str, Any]:
    target = confirm_target(event)
    _tenant_only(target)
    client = cognito()
    client.admin_disable_user(UserPoolId=target.pool_id, Username=target.username)
    client.admin_user_global_sign_out(UserPoolId=target.pool_id, Username=target.username)
    return {"ok": True}


def enable(event: dict[str, Any]) -> dict[str, Any]:
    target = confirm_target(event)
    _tenant_only(target)
    cognito().admin_enable_user(UserPoolId=target.pool_id, Username=target.username)
    return {"ok": True}


def set_role(event: dict[str, Any]) -> dict[str, Any]:
    role = _tenant_role(event)
    target = confirm_target(event)
    _tenant_only(target)
    client = cognito()
    client.admin_add_user_to_group(
        UserPoolId=target.pool_id, Username=target.username, GroupName=role
    )
    for group in target.groups:
        if group != role:
            client.admin_remove_user_from_group(
                UserPoolId=target.pool_id, Username=target.username, GroupName=group
            )
    return {"ok": True}


def reset_mfa(event: dict[str, Any]) -> dict[str, Any]:
    """Delete the user's TOTP device and sign them out everywhere.

    With the device gone, a pool that requires MFA returns MFA_SETUP at the next sign-in;
    in the tenant pool the portal makes a tenant-admin set it up again.
    """
    target = confirm_target(event)
    if target.pool == "platform" and target.groups != (PLATFORM_MFA_RESET_GROUP,):
        raise Refused(NOT_ALLOWED)
    client = cognito()
    had_totp = True
    try:
        client.admin_delete_software_token(UserPoolId=target.pool_id, Username=target.username)
    except ClientError as exc:
        # Nothing to delete: the user never set up TOTP. Still sign them out.
        if _code(exc) not in {"ResourceNotFoundException", "SoftwareTokenMFANotFoundException"}:
            raise
        had_totp = False
    client.admin_user_global_sign_out(UserPoolId=target.pool_id, Username=target.username)
    return {"ok": True, "had_totp": had_totp}


def mfa_status(event: dict[str, Any]) -> dict[str, Any]:
    """Whether a tenant-pool user has TOTP MFA turned on. Reads only; changes nothing."""
    target = confirm_target(event)
    _tenant_only(target)
    return {"totp": target.totp}


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "invite": invite,
    "resend_invite": resend_invite,
    "reset_password": reset_password,
    "disable": disable,
    "enable": enable,
    "set_role": set_role,
    "reset_mfa": reset_mfa,
    "mfa_status": mfa_status,
}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    operation = event.get("operation")
    allowed = allowed_operations()
    if operation not in HANDLERS:
        return {"error": INVALID_REQUEST}
    if operation not in allowed:
        log.warning(json.dumps({"message": "operation refused", "operation": operation}))
        return {"error": NOT_ALLOWED}
    pool = event.get("pool", "tenant")
    if pool == "platform" and operation not in PLATFORM_OPERATIONS:
        return {"error": NOT_ALLOWED}
    try:
        result = HANDLERS[str(operation)](event)
    except InvalidRequest as exc:
        log.warning(json.dumps({"message": "invalid request", "detail": str(exc)}))
        return {"error": INVALID_REQUEST}
    except Refused as exc:
        log.warning(json.dumps({"message": "refused", "operation": operation, "error": exc.code}))
        return {"error": exc.code}
    log.info(json.dumps({"message": "done", "operation": operation, "pool": pool}))
    return result
