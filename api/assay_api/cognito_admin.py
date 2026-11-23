"""Cognito admin Lambda: user-pool changes that cannot be made from inside the VPC.

A user pool with a managed login domain cannot be reached through PrivateLink, so this
function runs outside the VPC and is invoked through the `lambda` interface endpoint. It
has no database access and reaches only the tenant user pool.

One operation so far, `invite`: create a tenant-pool user and add it to its group. Cognito
emails the invitation with a temporary password. Only the provisioning role may invoke the
function (the first admin of a new tenant); build step 5 adds the API's user operations.

`custom:tenant_id` is immutable and decides which tenant the user's tokens belong to. This
function sets it once, at creation, from the `tenant_id` in the event. The provisioning
task takes that value from the tenant row it is provisioning, never from user input.

A repeated invitation is safe: an existing user of the same tenant is returned as it is. An
address that already belongs to another tenant is refused, without saying which one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from assay_api.claims import TENANT_GROUPS, TENANT_ID_ATTRIBUTE

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient

log = logging.getLogger()
log.setLevel(logging.INFO)

OPERATIONS = frozenset({"invite"})
CLIENT_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 3})
MAX_NAME = 200
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")

# Returned to the caller as {"error": code}. Never Cognito's own message.
EMAIL_IN_USE = "email_in_use"
INVALID_REQUEST = "invalid_request"

_client: CognitoIdentityProviderClient | None = None


class InvalidRequest(ValueError):
    """The event is not a request this function serves."""


def cognito() -> CognitoIdentityProviderClient:
    global _client  # one client per execution environment
    if _client is None:
        _client = boto3.client("cognito-idp", config=CLIENT_CONFIG)
    return _client


def _attribute(attributes: list[Any], name: str) -> str | None:
    for attribute in attributes:
        if attribute.get("Name") == name:
            return str(attribute.get("Value"))
    return None


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


def invite(pool_id: str, event: dict[str, Any]) -> dict[str, Any]:
    tenant_id, email, name, group = parse_invite(event)
    client = cognito()
    created = True
    try:
        response = client.admin_create_user(
            UserPoolId=pool_id,
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
        if exc.response.get("Error", {}).get("Code") != "UsernameExistsException":
            raise
        created = False
        existing = client.admin_get_user(UserPoolId=pool_id, Username=email)
        attributes = list(existing.get("UserAttributes", []))
        if _attribute(attributes, TENANT_ID_ATTRIBUTE) != tenant_id:
            log.warning(json.dumps({"message": "invite refused", "error": EMAIL_IN_USE}))
            return {"error": EMAIL_IN_USE}
    sub = _attribute(attributes, "sub")
    if not sub:
        raise RuntimeError("Cognito returned a user without a sub")
    # Idempotent: adding a user to a group it is already in succeeds.
    client.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
    log.info(
        json.dumps(
            {"message": "user invited", "tenant_id": tenant_id, "group": group, "created": created}
        )
    )
    return {"sub": sub, "created": created}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    operation = event.get("operation")
    if operation not in OPERATIONS:
        return {"error": INVALID_REQUEST}
    try:
        return invite(os.environ["TENANT_POOL_ID"], event)
    except InvalidRequest as exc:
        log.warning(json.dumps({"message": "invalid request", "detail": str(exc)}))
        return {"error": INVALID_REQUEST}
