"""Reading the audit trail for Audit history (BUILD_SPEC.md sections 7a and 11).

- Every filter is a bound parameter; the sort order comes from a fixed allow-list, so no
  request value is ever written into SQL.
- `redact()` is applied to every row's before and after values before they leave the API.
  Nothing secret is ever written to `audit_log` (section 7a), and this makes sure nothing
  secret is ever shown either: any key that names a password, token, secret, code or
  credential has its value replaced, at any depth.
- CSV cells that a spreadsheet would read as a formula are prefixed with an apostrophe.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Row, text

PAGE_SIZE = 50
MAX_PAGE_SIZE = 50
CSV_MAX_ROWS = 10_000
DEFAULT_DAYS = 7
REDACTED = "[redacted]"

# Keys whose values are never shown, wherever they appear in `detail`. Keys are compared
# lower-cased with separators removed, so `temporaryPassword`, `temporary_password` and
# `TEMPORARY-PASSWORD` are all caught.
_SECRET_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "credential",
    "authorization",
    "apikey",
    "privatekey",
    "session",
)
_SECRET_ENDINGS = ("code", "otp", "pin")
# Keys that end like a secret but hold plain metadata.
_NOT_SECRET = frozenset({"errorcode", "statuscode", "countrycode"})


def is_secret_key(key: str) -> bool:
    normalized = "".join(ch for ch in key.lower() if ch.isalnum())
    if normalized in _NOT_SECRET:
        return False
    return any(part in normalized for part in _SECRET_PARTS) or normalized.endswith(_SECRET_ENDINGS)


# Sort keys the API accepts -> ORDER BY clause. Nothing else reaches the SQL.
SORTS: dict[str, str] = {
    "at": "a.at ASC, a.id ASC",
    "-at": "a.at DESC, a.id DESC",
    "tenant": "t.name ASC, a.at DESC, a.id DESC",
    "-tenant": "t.name DESC, a.at DESC, a.id DESC",
    "actor": "a.actor_email ASC NULLS LAST, a.at DESC, a.id DESC",
    "-actor": "a.actor_email DESC NULLS LAST, a.at DESC, a.id DESC",
    "action": "a.action ASC, a.at DESC, a.id DESC",
    "-action": "a.action DESC, a.at DESC, a.id DESC",
    "category": "a.category ASC, a.at DESC, a.id DESC",
    "-category": "a.category DESC, a.at DESC, a.id DESC",
}
DEFAULT_SORT = "-at"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and is_secret_key(k) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass(frozen=True)
class AuditFilters:
    start: dt.datetime
    end: dt.datetime
    tenant_id: uuid.UUID | None = None
    category: str | None = None
    actor_email: str | None = None

    def as_detail(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "category": self.category,
            "actor_email": self.actor_email,
        }


def _like(value: str) -> str:
    """A contains-match pattern with LIKE's wildcards escaped."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(filters: AuditFilters) -> tuple[str, dict[str, Any]]:
    clauses = ["a.at >= :start", "a.at < :end"]
    params: dict[str, Any] = {"start": filters.start, "end": filters.end}
    if filters.tenant_id is not None:
        clauses.append("a.tenant_id = :tenant_id")
        params["tenant_id"] = filters.tenant_id
    if filters.category is not None:
        clauses.append("a.category = :category")
        params["category"] = filters.category
    if filters.actor_email:
        clauses.append("a.actor_email ILIKE :actor ESCAPE '\\'")
        params["actor"] = _like(filters.actor_email)
    return " AND ".join(clauses), params


SELECT = (
    "SELECT a.id, a.at, a.tenant_id, t.name AS tenant_name, a.actor_email, a.actor_role, "
    "a.category, a.action, a.target_type, a.target_id, a.target_label, a.detail, a.outcome, "
    "host(a.ip_address) AS ip_address, a.user_agent "
    "FROM assay.audit_log a JOIN assay.tenants t ON t.id = a.tenant_id WHERE "
)


def page(
    conn: Connection, filters: AuditFilters, *, sort: str, number: int, size: int
) -> tuple[int, list[Row[Any]]]:
    # Only fixed fragments are joined here; every value is a bound parameter.
    where, params = _where(filters)
    total = conn.execute(
        text("SELECT count(*) FROM assay.audit_log a WHERE " + where),  # noqa: S608  # nosec B608
        params,
    ).scalar_one()
    rows = conn.execute(
        text(SELECT + where + " ORDER BY " + SORTS[sort] + " LIMIT :limit OFFSET :offset"),
        {**params, "limit": size, "offset": (number - 1) * size},
    ).all()
    return int(total), list(rows)


def export(conn: Connection, filters: AuditFilters, *, sort: str) -> list[Row[Any]]:
    where, params = _where(filters)
    return list(
        conn.execute(
            text(SELECT + where + " ORDER BY " + SORTS[sort] + " LIMIT :limit"),
            {**params, "limit": CSV_MAX_ROWS},
        ).all()
    )


def detail_of(row: Row[Any]) -> dict[str, Any] | None:
    raw = row.detail
    if isinstance(raw, str):
        raw = json.loads(raw)
    return redact(raw) if isinstance(raw, dict) else None


CSV_COLUMNS = (
    "time",
    "tenant",
    "actor",
    "role",
    "category",
    "action",
    "target",
    "outcome",
    "ip_address",
    "browser",
    "detail",
)
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    text_value = "" if value is None else str(value)
    return f"'{text_value}" if text_value.startswith(_FORMULA) else text_value


def to_csv(rows: list[Row[Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        detail = detail_of(row)
        writer.writerow(
            [
                _cell(value)
                for value in (
                    row.at.isoformat(),
                    row.tenant_name,
                    row.actor_email,
                    row.actor_role,
                    row.category,
                    row.action,
                    row.target_label or row.target_id,
                    row.outcome,
                    row.ip_address,
                    row.user_agent,
                    json.dumps(detail, sort_keys=True) if detail else "",
                )
            ]
        )
    return out.getvalue()
