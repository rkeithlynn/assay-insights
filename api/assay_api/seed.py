"""Seed Lambda: the database half of `scripts/seed.py` (BUILD_SPEC.md section 5).

Runs in the VPC as `platform_writer`; only the owner can invoke it (it has no resource-based
permission, and no role's policy allows invoking it). Every command can be repeated.

- `platform`: the platform tenant (kind `platform`, `active`) and its users' rows. The
  Cognito users are created by `scripts/seed.py` itself, with the owner's credentials: no
  deployed function can create a platform-pool user.
- `tenants`: each business tenant not yet present (by name), added through
  `assay_api.tenants.create_tenant`, the path `POST /admin/tenants` uses.
- `status`: the status of the named business tenants.
- `invite_users`: users of one active business tenant, through `assay_api.users.invite_user`,
  the path `POST /users` uses (Cognito emails each invitation).
- `defaults`: the default ingest and query models for every active business tenant that has
  no selection yet (the isolation fixture tenants predate the catalog).

The actor recorded in `audit_log` is the operator, with the IAM caller `scripts/seed.py`
reports as `started_by`.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any

from sqlalchemy import Connection, Engine, text

from assay_api.services import CognitoRefused, LambdaCognitoAdmin, StepFunctionsProvisioning
from assay_api.tenants import create_tenant
from assay_api.users import TENANT_ROLES, invite_user
from assay_db import audit, tenants
from assay_db.catalog import DefaultModels, ensure_default_models
from assay_db.connect import DbTarget, iam_engine
from assay_db.roles import PLATFORM_WRITER

log = logging.getLogger()
log.setLevel(logging.INFO)

PLATFORM_ROLES = ("master-admin", "master-user")
COMMANDS = frozenset({"platform", "tenants", "status", "invite_users", "defaults"})
_ARN = re.compile(r"^arn:aws:(iam|sts)::\d{12}:[\w+=,.@/-]{1,512}$")

_engine: Engine | None = None


class SeedError(ValueError):
    """The request cannot be carried out. Nothing was written for the item concerned."""


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = iam_engine(DbTarget.from_env(), PLATFORM_WRITER, application_name="assay-seed")
    return _engine


def operator(event: dict[str, Any]) -> audit.AuditActor:
    principal = str(event.get("started_by") or "")
    if not _ARN.match(principal):
        raise SeedError("started_by must be the caller's IAM ARN")
    return audit.AuditActor(role=tenants.ROLE_OPERATOR, principal=principal)


def _list(event: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = event.get(key)
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise SeedError(f"{key} must be a list of objects")
    return items


# --- platform ---------------------------------------------------------------------------


def seed_platform(
    conn: Connection, event: dict[str, Any], actor: audit.AuditActor
) -> dict[str, Any]:
    name = tenants.check_name(str(event.get("tenant_name") or ""), "platform tenant name")
    row = conn.execute(
        text("SELECT id, name FROM assay.tenants WHERE kind = 'platform' FOR UPDATE")
    ).all()
    if len(row) > 1:
        raise SeedError("more than one platform tenant exists")
    if row:
        tenant_id, existing = uuid.UUID(str(row[0][0])), str(row[0][1])
        if existing != name:
            raise SeedError("a platform tenant with another name exists")
        tenant_created = False
    else:
        tenant_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO assay.tenants (id, name, kind, status) "
                "VALUES (:id, :name, 'platform', 'active')"
            ),
            {"id": tenant_id, "name": name},
        )
        audit.write(
            conn,
            tenant_id=tenant_id,
            actor=actor,
            category="tenant",
            action="created",
            target_type="tenant",
            target_id=str(tenant_id),
            target_label=name,
            detail={"after": {"name": name, "kind": "platform", "status": "active"}},
        )
        tenant_created = True

    created_users: list[str] = []
    for user in _list(event, "users"):
        email = tenants.check_email(str(user.get("email") or ""))
        user_name = tenants.check_name(str(user.get("name") or email), "user name")
        role = str(user.get("role") or "")
        sub = str(user.get("sub") or "")
        if role not in PLATFORM_ROLES or not sub:
            raise SeedError("each platform user needs a platform role and a Cognito sub")
        user_id = tenants.first_admin_user_id(tenant_id, sub)
        inserted = conn.execute(
            text(
                "INSERT INTO assay.users (id, tenant_id, cognito_sub, email, name, role, status) "
                "VALUES (:id, :tenant, :sub, :email, :name, :role, 'invited') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": user_id,
                "tenant": tenant_id,
                "sub": sub,
                "email": email,
                "name": user_name,
                "role": role,
            },
        ).rowcount
        if inserted == 1:
            audit.write(
                conn,
                tenant_id=tenant_id,
                actor=actor,
                category="user",
                action="invited",
                target_type="user",
                target_id=str(user_id),
                target_label=email,
                detail={"after": {"name": user_name, "email": email, "role": role}},
            )
            created_users.append(email)
    return {
        "tenant_id": str(tenant_id),
        "tenant_created": tenant_created,
        "users_created": created_users,
    }


# --- business tenants -------------------------------------------------------------------


def _business(conn: Connection, name: str) -> tuple[uuid.UUID, str] | None:
    rows = conn.execute(
        text("SELECT id, status FROM assay.tenants WHERE kind = 'business' AND name = :name"),
        {"name": name},
    ).all()
    if len(rows) > 1:
        raise SeedError(f"more than one business tenant is named {name!r}")
    return (uuid.UUID(str(rows[0][0])), str(rows[0][1])) if rows else None


def seed_tenants(db: Engine, event: dict[str, Any], actor: audit.AuditActor) -> dict[str, Any]:
    provisioning = StepFunctionsProvisioning(
        os.environ["PROVISIONING_STATE_MACHINE_ARN"], region=os.environ["AWS_REGION"]
    )
    results: list[dict[str, Any]] = []
    for item in _list(event, "tenants"):
        name = tenants.check_name(str(item.get("name") or ""), "tenant name")
        admin = item.get("admin")
        if not isinstance(admin, dict):
            raise SeedError(f"{name}: admin must be an object")
        with db.begin() as conn:
            found = _business(conn, name)
            if found is not None:
                results.append(
                    {"name": name, "tenant_id": str(found[0]), "status": found[1], "created": False}
                )
                continue
            email = str(admin.get("email") or "")
            tenant_id = create_tenant(
                conn,
                provisioning,
                name=name,
                admin_name=str(admin.get("name") or email),
                admin_email=email,
                actor=actor,
            )
        results.append(
            {"name": name, "tenant_id": str(tenant_id), "status": "provisioning", "created": True}
        )
    return {"tenants": results}


def tenant_status(conn: Connection, event: dict[str, Any]) -> dict[str, Any]:
    names = event.get("names")
    if not isinstance(names, list):
        raise SeedError("names must be a list")
    statuses: dict[str, Any] = {}
    for name in names:
        found = _business(conn, str(name))
        statuses[str(name)] = None if found is None else {"id": str(found[0]), "status": found[1]}
    return {"tenants": statuses}


def seed_users(conn: Connection, event: dict[str, Any], actor: audit.AuditActor) -> dict[str, Any]:
    name = tenants.check_name(str(event.get("tenant_name") or ""), "tenant name")
    found = _business(conn, name)
    if found is None or found[1] != tenants.STATUS_ACTIVE:
        raise SeedError(f"{name} is not an active business tenant")
    tenant_id = found[0]
    cognito = LambdaCognitoAdmin(
        os.environ["COGNITO_ADMIN_FUNCTION"], region=os.environ["AWS_REGION"]
    )
    results: list[dict[str, Any]] = []
    for user in _list(event, "users"):
        email = tenants.check_email(str(user.get("email") or ""))
        role = str(user.get("role") or "")
        if role not in TENANT_ROLES:
            raise SeedError(f"{email}: role must be tenant-admin or tenant-user")
        invited = invite_user(
            conn,
            cognito,
            tenant_id=tenant_id,
            name=tenants.check_name(str(user.get("name") or email), "user name"),
            email=email,
            role=role,
            invited_by=None,
            actor=actor,
        )
        results.append({"email": email, "created": invited.created})
    return {"tenant_id": str(tenant_id), "users": results}


def seed_defaults(conn: Connection) -> dict[str, Any]:
    defaults = DefaultModels(
        ingest=os.environ["DEFAULT_INGEST_MODEL"], query=os.environ["DEFAULT_QUERY_MODEL"]
    )
    rows = conn.execute(
        text(
            "SELECT id FROM assay.tenants WHERE kind = 'business' AND status = 'active' "
            "AND (extraction_model IS NULL OR answer_model IS NULL) ORDER BY created_at"
        )
    ).all()
    changed = [
        str(r[0])
        for r in rows
        if ensure_default_models(conn, tenant_id=uuid.UUID(str(r[0])), defaults=defaults)
    ]
    return {"tenants_given_defaults": changed}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    command = event.get("command")
    if command not in COMMANDS:
        return {"error": f"unknown command; expected one of {sorted(COMMANDS)}"}
    try:
        db = engine()
        if command == "tenants":
            result = seed_tenants(db, event, operator(event))
        else:
            with db.begin() as conn:
                if command == "platform":
                    result = seed_platform(conn, event, operator(event))
                elif command == "status":
                    result = tenant_status(conn, event)
                elif command == "invite_users":
                    result = seed_users(conn, event, operator(event))
                else:
                    result = seed_defaults(conn)
    except (SeedError, ValueError, CognitoRefused) as exc:
        log.warning("seed command refused", extra={"command": command, "error": str(exc)})
        return {"error": str(exc)}
    log.info("seed command finished", extra={"command": command})
    return {"command": command, **result}
