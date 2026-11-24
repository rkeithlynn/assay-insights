"""The TSS Lambda, behind a private API Gateway REST API with IAM authorization.

One route: `POST /resolve`. API Gateway has already verified the SigV4 signature; the
caller's role arrives in `requestContext.identity.userArn`, and decides which service name
the source identity must carry. Credentials are returned in the response body and are never
logged.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import boto3
from botocore.config import Config
from pydantic import ValidationError

from assay_tss.models import ResolveRequest
from assay_tss.service import (
    DbStorageReader,
    Resolver,
    ServiceSettings,
    TssError,
    caller_service,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

APPLICATION_NAME = "assay-tss"
DB_USER = "tss"  # assay_db.roles.TSS
RESOLVE_ROUTE = ("POST", "/resolve")
STS_CONFIG = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})

_resolver: Resolver | None = None
_settings: ServiceSettings | None = None


def _state() -> tuple[ServiceSettings, Resolver]:
    """One database engine and one STS client per execution environment."""
    global _resolver, _settings
    if _resolver is None or _settings is None:
        from assay_db.connect import DbTarget, iam_engine

        _settings = ServiceSettings.from_env()
        engine = iam_engine(
            DbTarget.from_env(), DB_USER, application_name=APPLICATION_NAME, pool_size=1
        )
        sts = boto3.client(
            "sts",
            region_name=_settings.region,
            # The regional endpoint: the only one the VPC's STS endpoint serves.
            endpoint_url=f"https://sts.{_settings.region}.amazonaws.com",
            config=STS_CONFIG,
        )
        _resolver = Resolver(_settings, DbStorageReader(engine), sts)
    return _settings, _resolver


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def _body(event: dict[str, Any]) -> str:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw).decode()
    return str(raw)


def handle(event: dict[str, Any], settings: ServiceSettings, resolver: Resolver) -> dict[str, Any]:
    route = (event.get("httpMethod"), event.get("resource"))
    if route != RESOLVE_ROUTE:
        return _response(404, {"error": "not_found"})
    identity = (event.get("requestContext") or {}).get("identity") or {}
    fields: dict[str, Any] = {"route": "resolve"}
    try:
        caller = caller_service(identity.get("userArn"), settings.callers)
        fields["service"] = caller.service
        request = ResolveRequest.model_validate_json(_body(event))
        fields |= {
            "tenant_id": str(request.tenant_id),
            "access": request.access.value,
            "source_identity": request.source_identity,
        }
        context = resolver.resolve(request, caller=caller)
    except ValidationError as exc:
        log.warning("invalid request", extra=fields | {"errors": exc.error_count()})
        return _response(400, {"error": "invalid_request"})
    except TssError as exc:
        level = logging.ERROR if exc.status >= 500 else logging.WARNING
        log.log(level, "request refused", extra=fields | {"error": exc.code, "detail": str(exc)})
        return _response(exc.status, {"error": exc.code})
    fields["expires"] = context.credentials.expiration.isoformat()
    log.info("storage context issued", extra=fields)
    return _response(200, context.to_wire())


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    try:
        settings, resolver = _state()
        return handle(event, settings, resolver)
    except Exception:
        # Never return a stack trace or a database error to the caller.
        log.exception("unhandled error")
        return _response(500, {"error": "internal_error"})
