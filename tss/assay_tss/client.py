"""The TSS client: how the API and the workers get a storage context.

Signs each request with SigV4 (the private API uses IAM authorization) and sends it through
the VPC's `execute-api` endpoint. The caller's own role decides which service name the TSS
accepts in the source identity.

A call that never reached the TSS is tried again, a few times, with a short wait in between.
API Gateway answers for itself when it cannot run the TSS's function: 500 "Internal server
error" when Lambda throttles the invocation, 502, 503 or 504 otherwise, and 429 when it
throttles the request. None of those is the TSS's answer, and asking again is safe:
`resolve` changes nothing. On 2026-11-25 one scan result of nine that arrived together got
that 500 (the account allowed ten concurrent Lambda executions and nine were the scan
function's), and without a retry here the scan function failed on it.

The TSS's own answers are never retried: a refusal (403, 404, 409) is a refusal, and its
own 500 names a fault that asking again will not cure.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from assay_tss.models import (
    IsolationMode,
    ResolveRequest,
    StorageAccess,
    StorageContext,
    TenantStorageProfile,
)

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
# API Gateway's own answers when it could not get one from the TSS's function.
GATEWAY_STATUSES = frozenset({429, 502, 503, 504})
# At most this many tries, waiting these seconds (each with jitter) before the next...
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (0.4, 0.8, 1.6, 3.2)
# ...and never starting another try this long after the first began. A throttled call comes
# back in milliseconds, so all five fit; a call that timed out (29 seconds) is not repeated.
RETRY_BUDGET_SECONDS = 20.0


class TssRequestError(RuntimeError):
    """The TSS refused the request. `status` and `code` say why; there is no context.

    `transient` is true when the answer was API Gateway's, not the TSS's: the request never
    reached the service (or its answer never came back), and may be tried again.
    """

    def __init__(self, status: int, code: str, *, transient: bool = False) -> None:
        super().__init__(f"TSS refused the request: {status} {code}")
        self.status = status
        self.code = code
        self.transient = transient


class TssClient:
    """Implements `TenantStorageService.resolve` against the private API."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        region: str,
        session: boto3.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not endpoint_url.startswith("https://"):
            raise ValueError("the TSS endpoint must use https")
        self._endpoint = endpoint_url.rstrip("/")
        self._region = region
        self._session = session or boto3.Session(region_name=region)
        self._sleep = sleep

    def signed_request(self, path: str, body: bytes) -> urllib.request.Request:
        url = f"{self._endpoint}{path}"
        request = AWSRequest(
            method="POST", url=url, data=body, headers={"Content-Type": "application/json"}
        )
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("no AWS credentials to sign the TSS request with")
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", self._region).add_auth(
            request
        )
        return urllib.request.Request(  # noqa: S310  # https only, checked in __init__
            url, data=body, headers=dict(request.headers.items()), method="POST"
        )

    def send(self, request: urllib.request.Request) -> dict[str, Any]:
        """Send the request; try again if it never reached the TSS (see the module's note).

        The same signed request is sent each time: its signature is good for five minutes.
        """
        started = time.monotonic()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._send_once(request)
            except TssRequestError as exc:
                if not exc.transient or attempt == MAX_ATTEMPTS:
                    raise
                # Jitter, so callers that were throttled together do not come back together.
                delay = BACKOFF_SECONDS[attempt - 1] * random.uniform(0.5, 1.5)  # noqa: S311  # nosec B311
                if time.monotonic() - started + delay > RETRY_BUDGET_SECONDS:
                    raise
                # A warning, not an error: nothing has failed yet. Status and code only.
                log.warning(
                    "TSS call did not reach the service; trying again",
                    extra={"status": exc.status, "code": exc.code, "attempt": attempt},
                )
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send_once(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(  # noqa: S310  # nosec B310
                request, timeout=TIMEOUT_SECONDS
            ) as response:
                result: dict[str, Any] = json.load(response)
                return result
        except urllib.error.HTTPError as exc:
            try:
                parsed = json.load(exc)
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            # The TSS answers {"error": code}; API Gateway itself answers {"message": text}.
            code = parsed.get("error") or parsed.get("message") or "unknown"
            # API Gateway's own answer: a gateway status, or a 500 that is not the TSS's
            # (Lambda throttled the invocation). The TSS's own answers always carry "error".
            gateway = exc.code in GATEWAY_STATUSES or (exc.code == 500 and "error" not in parsed)
            raise TssRequestError(exc.code, str(code), transient=gateway) from None

    def resolve(
        self, tenant_id: uuid.UUID, access: StorageAccess, source_identity: str
    ) -> StorageContext:
        request = ResolveRequest(
            tenant_id=tenant_id, access=access, source_identity=source_identity
        )
        body = json.dumps(request.model_dump(mode="json")).encode()
        return StorageContext.from_wire(self.send(self.signed_request("/resolve", body)))

    def resolve_worker(self, document_id: uuid.UUID, attempt: int, token: str) -> StorageContext:
        """The worker task's only way to storage: one document's keys, for the tenant its
        attempt grant names (the TSS reads it; the worker names no tenant)."""
        body = json.dumps(
            {"document_id": str(document_id), "attempt": attempt, "token": token}
        ).encode()
        return StorageContext.from_wire(self.send(self.signed_request("/resolve-worker", body)))

    def provision(self, tenant_id: uuid.UUID, mode: IsolationMode) -> TenantStorageProfile:
        raise NotImplementedError("tenant storage is created only by the provisioning task")

    def offboard(self, tenant_id: uuid.UUID) -> None:
        raise NotImplementedError("offboarding is not part of the first deployment")
