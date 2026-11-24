"""The model catalog, `ai_models` (BUILD_SPEC.md section 7b), and a tenant's default models.

- `seed_models` upserts the catalog. It runs as `migrator`, the tables' owner, in the
  migration Lambda (`seed_models` command, started by `scripts/seed.py`). No application
  role can write `ai_models`. The models come from stack config (`aiModels`), never from
  code, and only Bedrock `us.` inference profiles are accepted (section 2).
- `ensure_default_models` gives a business tenant the default ingest and query models when
  it has none: one row each in `tenant_models` and the two selections on `tenants`. It runs
  as `platform_writer`, when provisioning activates a tenant and when `seed.py` backfills
  existing tenants. The defaults are profile IDs from stack config.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Connection, text

from assay_db.models import JOBS

STATUS_ACTIVE = "active"
# Bedrock rule (section 2): call models only through `us.` inference profiles.
_PROFILE_ID = re.compile(r"^us\.[a-z0-9][a-z0-9.:-]{2,200}$")
MAX_NAME = 100


class CatalogError(ValueError):
    """The catalog entry or default model is not acceptable. Nothing was written."""


@dataclass(frozen=True)
class CatalogModel:
    name: str
    profile_id: str
    jobs: tuple[str, ...]
    price_in: Decimal  # US dollars per million input tokens
    price_out: Decimal

    @classmethod
    def parse(cls, raw: Any) -> CatalogModel:
        if not isinstance(raw, dict):
            raise CatalogError("each model must be an object")
        name = " ".join(str(raw.get("name") or "").split())
        if not name or len(name) > MAX_NAME:
            raise CatalogError(f"model name must be 1 to {MAX_NAME} characters")
        profile_id = str(raw.get("profile_id") or "")
        if not _PROFILE_ID.match(profile_id):
            raise CatalogError(f"{name}: profile_id must be a us. inference profile ID")
        jobs = raw.get("jobs")
        if not isinstance(jobs, list) or not jobs or any(job not in JOBS for job in jobs):
            raise CatalogError(f"{name}: jobs must be a non-empty list from {list(JOBS)}")
        prices = []
        for field in ("price_in", "price_out"):
            value = raw.get(field)
            if value is None or str(value).strip() == "":
                raise CatalogError(f"{name}: {field} is not set")
            try:
                price = Decimal(str(value))
            except InvalidOperation:
                raise CatalogError(f"{name}: {field} is not a number") from None
            if not price.is_finite() or price < 0:
                raise CatalogError(f"{name}: {field} must be zero or more")
            prices.append(price)
        return cls(
            name=name,
            profile_id=profile_id,
            jobs=tuple(sorted(set(jobs))),
            price_in=prices[0],
            price_out=prices[1],
        )


def parse_models(raw: Any) -> list[CatalogModel]:
    if not isinstance(raw, list) or not raw:
        raise CatalogError("models must be a non-empty list")
    models = [CatalogModel.parse(item) for item in raw]
    profiles = [m.profile_id for m in models]
    if len(set(profiles)) != len(profiles):
        raise CatalogError("a profile_id appears twice")
    return models


def seed_models(conn: Connection, models: list[CatalogModel]) -> dict[str, list[str]]:
    """Insert or update each model by its profile ID. Models not listed are left alone."""
    inserted: list[str] = []
    updated: list[str] = []
    for model in models:
        was_inserted = conn.execute(
            text(
                "INSERT INTO assay.ai_models (name, profile_id, jobs, price_in, price_out, status) "
                "VALUES (:name, :profile_id, :jobs, :price_in, :price_out, :status) "
                "ON CONFLICT (profile_id) DO UPDATE SET name = EXCLUDED.name, "
                "jobs = EXCLUDED.jobs, price_in = EXCLUDED.price_in, "
                "price_out = EXCLUDED.price_out, status = EXCLUDED.status "
                "RETURNING (xmax = 0)"
            ),
            {
                "name": model.name,
                "profile_id": model.profile_id,
                "jobs": list(model.jobs),
                "price_in": model.price_in,
                "price_out": model.price_out,
                "status": STATUS_ACTIVE,
            },
        ).scalar_one()
        (inserted if was_inserted else updated).append(model.profile_id)
    return {"inserted": inserted, "updated": updated}


@dataclass(frozen=True)
class DefaultModels:
    """Profile IDs of the models a new tenant starts with (stack config)."""

    ingest: str
    query: str

    def __post_init__(self) -> None:
        for profile in (self.ingest, self.query):
            if not _PROFILE_ID.match(profile):
                raise CatalogError("default models must be us. inference profile IDs")


def _model_for(conn: Connection, profile_id: str, job: str) -> uuid.UUID:
    row = conn.execute(
        text(
            "SELECT id FROM assay.ai_models WHERE profile_id = :profile AND status = :status "
            "AND :job = ANY(jobs)"
        ),
        {"profile": profile_id, "status": STATUS_ACTIVE, "job": job},
    ).one_or_none()
    if row is None:
        raise CatalogError(f"default {job} model {profile_id} is not in the catalog")
    return uuid.UUID(str(row[0]))


def ensure_default_models(
    conn: Connection, *, tenant_id: uuid.UUID, defaults: DefaultModels
) -> bool:
    """Give the tenant the default models if it has no selection. True when it changed.

    Runs as platform_writer. Writes a `models_available_changed` audit row (actor: system)
    when it adds anything. A tenant that already has a selection is left alone.
    """
    current = conn.execute(
        text(
            "SELECT name, extraction_model, answer_model FROM assay.tenants "
            "WHERE id = :id AND kind = 'business' FOR UPDATE"
        ),
        {"id": tenant_id},
    ).one_or_none()
    if current is None:
        raise CatalogError("no such business tenant")
    name, extraction, answer = current
    if extraction is not None and answer is not None:
        return False
    ingest = _model_for(conn, defaults.ingest, "ingest")
    query = _model_for(conn, defaults.query, "query")
    before = _available(conn, tenant_id)
    for model_id, job in ((ingest, "ingest"), (query, "query")):
        conn.execute(
            text(
                "INSERT INTO assay.tenant_models (tenant_id, model_id, job) "
                "VALUES (:tenant, :model, :job) "
                "ON CONFLICT (tenant_id, model_id, job) DO NOTHING"
            ),
            {"tenant": tenant_id, "model": model_id, "job": job},
        )
    conn.execute(
        text(
            "UPDATE assay.tenants SET extraction_model = COALESCE(extraction_model, :ingest), "
            "answer_model = COALESCE(answer_model, :query) WHERE id = :id"
        ),
        {"id": tenant_id, "ingest": ingest, "query": query},
    )
    conn.execute(
        text(
            "INSERT INTO assay.audit_log (at, tenant_id, actor_role, category, action, "
            "target_type, target_id, target_label, detail, outcome) VALUES (clock_timestamp(), "
            ":tenant, 'system', 'tenant', 'models_available_changed', 'tenant', :target, "
            ":label, CAST(:detail AS jsonb), 'success')"
        ),
        {
            "tenant": tenant_id,
            "target": str(tenant_id),
            "label": name,
            "detail": json.dumps(
                {
                    "before": before,
                    "after": _available(conn, tenant_id),
                    "reason": "default models",
                }
            ),
        },
    )
    return True


def _available(conn: Connection, tenant_id: uuid.UUID) -> dict[str, list[str]]:
    rows = conn.execute(
        text(
            "SELECT tm.job, m.profile_id FROM assay.tenant_models tm "
            "JOIN assay.ai_models m ON m.id = tm.model_id WHERE tm.tenant_id = :tenant "
            "ORDER BY tm.job, m.profile_id"
        ),
        {"tenant": tenant_id},
    ).all()
    available: dict[str, list[str]] = {job: [] for job in JOBS}
    for job, profile in rows:
        available[str(job)].append(str(profile))
    return available
