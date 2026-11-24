"""Writing `audit_log` rows (BUILD_SPEC.md section 7a), inside the caller's transaction.

A change and its audit row commit together. `detail` holds the values before and after,
never a password, token or code. The roles that call this have INSERT on `audit_log` and
nothing else on it: `app_tenant` for its own tenant (row-level security), `platform_writer`
for any tenant.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

MAX_USER_AGENT = 500
OUTCOME_SUCCESS = "success"


@dataclass(frozen=True)
class AuditActor:
    role: str
    user_id: uuid.UUID | None = None
    email: str | None = None
    # The IAM caller, when an operator ran a script (recorded in detail.started_by).
    principal: str | None = None


@dataclass(frozen=True)
class RequestInfo:
    ip_address: str | None = None
    user_agent: str | None = None


NO_REQUEST = RequestInfo()


def write(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    actor: AuditActor,
    category: str,
    action: str,
    target_type: str | None,
    target_id: str | None,
    target_label: str | None,
    detail: dict[str, Any] | None = None,
    outcome: str = OUTCOME_SUCCESS,
    request: RequestInfo = NO_REQUEST,
) -> None:
    body = dict(detail or {})
    if actor.principal:
        body["started_by"] = actor.principal
    conn.execute(
        text(
            "INSERT INTO assay.audit_log (tenant_id, actor_user_id, actor_email, actor_role, "
            "category, action, target_type, target_id, target_label, detail, outcome, "
            "ip_address, user_agent) VALUES (:tenant_id, :actor_user_id, :actor_email, "
            ":actor_role, :category, :action, :target_type, :target_id, :target_label, "
            "CAST(:detail AS jsonb), :outcome, CAST(:ip AS inet), :user_agent)"
        ),
        {
            "tenant_id": tenant_id,
            "actor_user_id": actor.user_id,
            "actor_email": actor.email,
            "actor_role": actor.role,
            "category": category,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "target_label": target_label,
            "detail": json.dumps(body),
            "outcome": outcome,
            "ip": request.ip_address,
            "user_agent": (request.user_agent or None) and request.user_agent[:MAX_USER_AGENT],
        },
    )
