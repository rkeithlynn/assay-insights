"""The TSS client: how the API and the workers get a storage context.

Signs each request with SigV4 (the private API uses IAM authorization) and sends it through
the VPC's `execute-api` endpoint. The caller's own role decides which service name the TSS
accepts in the source identity.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
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

TIMEOUT_SECONDS = 30


class TssRequestError(RuntimeError):
    """The TSS refused the request. `status` and `code` say why; there is no context."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"TSS refused the request: {status} {code}")
        self.status = status
        self.code = code


class TssClient:
    """Implements `TenantStorageService.resolve` against the private API."""

    def __init__(
        self, endpoint_url: str, *, region: str, session: boto3.Session | None = None
    ) -> None:
        if not endpoint_url.startswith("https://"):
            raise ValueError("the TSS endpoint must use https")
        self._endpoint = endpoint_url.rstrip("/")
        self._region = region
        self._session = session or boto3.Session(region_name=region)

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
            # The TSS answers {"error": code}; API Gateway itself answers {"message": text}.
            code = parsed.get("error") or parsed.get("message") or "unknown"
            raise TssRequestError(exc.code, str(code)) from None

    def resolve(
        self, tenant_id: uuid.UUID, access: StorageAccess, source_identity: str
    ) -> StorageContext:
        request = ResolveRequest(
            tenant_id=tenant_id, access=access, source_identity=source_identity
        )
        body = json.dumps(request.model_dump(mode="json")).encode()
        return StorageContext.from_wire(self.send(self.signed_request("/resolve", body)))

    def provision(self, tenant_id: uuid.UUID, mode: IsolationMode) -> TenantStorageProfile:
        raise NotImplementedError("tenant storage is created only by the provisioning task")

    def offboard(self, tenant_id: uuid.UUID) -> None:
        raise NotImplementedError("offboarding is not part of the first deployment")
