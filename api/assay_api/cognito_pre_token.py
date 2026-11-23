"""Tenant pool pre-token-generation trigger (event version V2_0).

Adds two claims to the access token:

- `tenant_id`, copied from the user's immutable `custom:tenant_id`. The API takes the tenant
  only from this verified claim (BUILD_SPEC.md section 6, rule 4). A user without a valid
  tenant ID gets no token at all.
- `mfa`, true when the user has TOTP MFA turned on. Tenant pool MFA is optional at pool
  level, but once a user has TOTP, Cognito requires it at every sign-in (remembered devices
  are off), so `mfa` means this session passed MFA. The API refuses tenant-admin requests
  without it.

Runs outside the VPC: it needs Cognito's API, which a user pool with a domain cannot reach
through PrivateLink. It never touches the database. Cognito waits at most 5 seconds for a
trigger, so the Cognito call has short timeouts; if it fails, `mfa` is false (fail closed)
and the sign-in itself still goes ahead.

This module and `claims.py` are the only API files in the trigger image, which uses the
boto3 bundled with the digest-pinned Lambda base image.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from assay_api.claims import MFA_CLAIM, TENANT_ID_ATTRIBUTE, TENANT_ID_CLAIM

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient

log = logging.getLogger()
log.setLevel(logging.INFO)

EVENT_VERSION = "2"
TOTP = "SOFTWARE_TOKEN_MFA"
CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 2})

_client: CognitoIdentityProviderClient | None = None


class TenantIdMissing(PermissionError):
    """The user has no valid tenant ID. Cognito refuses to issue tokens."""


def cognito() -> CognitoIdentityProviderClient:
    global _client  # one client per execution environment
    if _client is None:
        _client = boto3.client("cognito-idp", config=CLIENT_CONFIG)
    return _client


def tenant_id_of(attributes: dict[str, str]) -> str:
    raw = attributes.get(TENANT_ID_ATTRIBUTE, "")
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise TenantIdMissing("user has no valid tenant ID") from None


def has_totp(user_pool_id: str, username: str) -> bool:
    try:
        user = cognito().admin_get_user(UserPoolId=user_pool_id, Username=username)
    except (BotoCoreError, ClientError) as exc:
        log.error(json.dumps({"message": "AdminGetUser failed; mfa=false", "error": str(exc)}))
        return False
    return TOTP in user.get("UserMFASettingList", [])


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    if event.get("version") != EVENT_VERSION:
        # A V1 response shape would be silently ignored, leaving tokens without tenant_id.
        raise ValueError(f"expected pre-token event version {EVENT_VERSION}")
    tenant_id = tenant_id_of(event["request"]["userAttributes"])
    mfa = has_totp(event["userPoolId"], event["userName"])
    event.setdefault("response", {})["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {
            "claimsToAddOrOverride": {TENANT_ID_CLAIM: tenant_id, MFA_CLAIM: mfa},
        }
    }
    log.info(
        json.dumps(
            {
                "message": "tenant claims added",
                "trigger_source": event.get("triggerSource"),
                "tenant_id": tenant_id,
                MFA_CLAIM: mfa,
            }
        )
    )
    return event
