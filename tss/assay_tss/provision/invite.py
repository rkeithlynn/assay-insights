"""Inviting a tenant's first admin, through the Cognito admin Lambda.

Cognito's API cannot be reached from the VPC (a user pool with a domain is incompatible
with PrivateLink), so the provisioning task invokes a Lambda outside the VPC through the
`lambda` interface endpoint. Only the provisioning role may invoke it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient

OPERATION = "invite"
ROLE = "tenant-admin"  # assay_db.tenants.FIRST_ADMIN_ROLE
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2})


class InviteError(RuntimeError):
    """The invitation was refused. The message is a short code, safe to record."""


@dataclass(frozen=True)
class InviteResult:
    cognito_sub: str
    # False when the user already existed for this tenant (a repeated run).
    created: bool


class Inviter(Protocol):
    def invite(self, *, tenant_id: uuid.UUID, email: str, name: str) -> InviteResult: ...


class LambdaInviter:
    def __init__(self, function: str, *, region: str, client: LambdaClient | None = None) -> None:
        self._function = function
        self._client: LambdaClient = client or boto3.client(
            "lambda", region_name=region, config=CLIENT_CONFIG
        )

    def invite(self, *, tenant_id: uuid.UUID, email: str, name: str) -> InviteResult:
        payload = {
            "operation": OPERATION,
            "tenant_id": str(tenant_id),
            "email": email,
            "name": name,
            "role": ROLE,
        }
        response = self._client.invoke(
            FunctionName=self._function,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        body: Any = json.loads(response["Payload"].read() or b"null")
        if response.get("FunctionError"):
            # An unhandled error in the Lambda. Its message may hold Cognito's text.
            raise InviteError("invite_failed")
        if not isinstance(body, dict):
            raise InviteError("invite_failed")
        if body.get("error"):
            raise InviteError(str(body["error"])[:100])
        sub = body.get("sub")
        if not isinstance(sub, str) or not sub:
            raise InviteError("invite_failed")
        return InviteResult(cognito_sub=sub, created=bool(body.get("created")))
