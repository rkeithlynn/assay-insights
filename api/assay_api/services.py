"""The two AWS calls the API makes: the Cognito admin Lambda and the provisioning state machine.

Both are reached through interface endpoints (`lambda`, `states`), whose policies admit only
the API's task role (and the seed Lambda) to exactly these resources. Each sits behind a
small protocol so the route tests run against fakes, with no AWS calls.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import TYPE_CHECKING, Any, Protocol

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient
    from mypy_boto3_stepfunctions import SFNClient

LAMBDA_CONFIG = Config(connect_timeout=3, read_timeout=30, retries={"max_attempts": 2})
SFN_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 3})


class CognitoRefused(Exception):
    """The Cognito admin Lambda refused or failed. `code` is a short code, safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CognitoAdmin(Protocol):
    def call(self, operation: str, **fields: Any) -> dict[str, Any]: ...


class LambdaCognitoAdmin:
    """Invokes the API's Cognito admin function. Never passes Cognito's own message on."""

    def __init__(self, function: str, *, region: str, client: LambdaClient | None = None) -> None:
        self._function = function
        self._client: LambdaClient = client or boto3.client(
            "lambda", region_name=region, config=LAMBDA_CONFIG
        )

    def call(self, operation: str, **fields: Any) -> dict[str, Any]:
        payload = {"operation": operation, **fields}
        response = self._client.invoke(
            FunctionName=self._function,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload, default=str).encode(),
        )
        body: Any = json.loads(response["Payload"].read() or b"null")
        if response.get("FunctionError") or not isinstance(body, dict):
            raise CognitoRefused("cognito_failed")
        if body.get("error"):
            raise CognitoRefused(str(body["error"])[:100])
        return body


class Provisioning(Protocol):
    def start(self, *, tenant_id: uuid.UUID, request: dict[str, Any]) -> str: ...


def execution_name(tenant_id: uuid.UUID, now: dt.datetime) -> str:
    """As scripts/add_tenant.py names them, so its --retry finds the latest one."""
    return f"t-{tenant_id}-{now:%Y%m%d%H%M%S}"


class StepFunctionsProvisioning:
    def __init__(self, state_machine: str, *, region: str, client: SFNClient | None = None) -> None:
        self._state_machine = state_machine
        self._client: SFNClient = client or boto3.client(
            "stepfunctions", region_name=region, config=SFN_CONFIG
        )

    def start(self, *, tenant_id: uuid.UUID, request: dict[str, Any]) -> str:
        started = self._client.start_execution(
            stateMachineArn=self._state_machine,
            name=execution_name(tenant_id, dt.datetime.now(dt.UTC)),
            input=json.dumps({"request": request}),
        )
        return str(started["executionArn"])


def provision_request(
    *,
    tenant_id: uuid.UUID,
    name: str,
    admin_name: str,
    admin_email: str,
    actor: dict[str, Any],
    retry: bool,
) -> dict[str, Any]:
    """The state machine's input (assay_tss.provision.task.ProvisionRequest)."""
    return {
        "tenant_id": str(tenant_id),
        "name": name,
        "first_admin": {"name": admin_name, "email": admin_email},
        "actor": actor,
        "retry": retry,
    }
