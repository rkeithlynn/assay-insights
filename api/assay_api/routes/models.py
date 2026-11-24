"""Models (BUILD_SPEC.md section 7b).

- `GET /admin/tenants/{id}/models` (both master roles) and `PUT` (master admin): which
  catalog models a business tenant may use, per job. Reads go through the platform views
  (`platform.ai_models_v`, `platform.tenant_models_v`) as `platform_reader`; the change runs
  as `platform_writer`. Each job keeps at least one model. If the tenant's selected model is
  removed, the selection moves to the first remaining model in the same transaction, and
  both changes are audited (`models_available_changed`, then `model_selected`).
- `GET /settings/models` and `PUT` (tenant admin): the tenant's available models with their
  prices, and its selection. As `app_tenant`, so only the caller's tenant's list is visible:
  a model available to another tenant is simply not there to select.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from assay_api.database import Databases
from assay_api.requestinfo import request_info
from assay_api.security import Caller, MasterAdmin, PlatformMember, TenantAdmin
from assay_db import audit
from assay_db.models import JOBS

router = APIRouter()

ACTIVE = "active"


def databases(request: Request) -> Databases:
    dbs: Databases = request.app.state.databases
    return dbs


Dbs = Annotated[Databases, Depends(databases)]


class CatalogModel(BaseModel):
    id: uuid.UUID
    name: str
    profile_id: str
    jobs: list[str]
    # US dollars per million tokens.
    price_in: Decimal
    price_out: Decimal


class TenantModels(BaseModel):
    """The catalog, and per job the models available to the tenant and the one selected."""

    catalog: list[CatalogModel]
    available: dict[str, list[uuid.UUID]]
    selected: dict[str, uuid.UUID | None]


class AvailabilityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingest: list[uuid.UUID] = Field(min_length=1, max_length=50)
    query: list[uuid.UUID] = Field(min_length=1, max_length=50)


class SelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingest_model: uuid.UUID
    query_model: uuid.UUID


def _conflict(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)


def _catalog(rows: list[Any]) -> list[CatalogModel]:
    return [
        CatalogModel(
            id=r.model_id,
            name=r.name,
            profile_id=r.profile_id,
            jobs=list(r.jobs),
            price_in=r.price_in,
            price_out=r.price_out,
        )
        for r in rows
    ]


# --- Master roles -----------------------------------------------------------------------------


TENANT = (
    "SELECT id, name, extraction_model, answer_model FROM assay.tenants "
    "WHERE id = :id AND kind = 'business'"
)
TENANT_FOR_UPDATE = TENANT + " FOR UPDATE"


def _business_tenant(conn: Connection, tenant_id: uuid.UUID, *, lock: bool = False) -> Any:
    row = conn.execute(text(TENANT_FOR_UPDATE if lock else TENANT), {"id": tenant_id}).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found")
    return row


def _platform_view(conn: Connection, tenant_id: uuid.UUID) -> TenantModels:
    tenant = _business_tenant(conn, tenant_id)
    catalog = conn.execute(
        text(
            "SELECT model_id, name, profile_id, jobs, price_in, price_out "
            "FROM platform.ai_models_v WHERE status = :active ORDER BY name"
        ),
        {"active": ACTIVE},
    ).all()
    available: dict[str, list[uuid.UUID]] = {job: [] for job in JOBS}
    for row in conn.execute(
        text("SELECT model_id, job FROM platform.tenant_models_v WHERE tenant_id = :id"),
        {"id": tenant_id},
    ).all():
        available[str(row.job)].append(row.model_id)
    return TenantModels(
        catalog=_catalog(list(catalog)),
        available={job: sorted(ids, key=str) for job, ids in available.items()},
        selected={"ingest": tenant.extraction_model, "query": tenant.answer_model},
    )


@router.get("/admin/tenants/{tenant_id}/models", response_model=TenantModels)
def tenant_models(tenant_id: uuid.UUID, caller: PlatformMember) -> TenantModels:
    return _platform_view(caller.conn, tenant_id)


def _profiles(conn: Connection, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    rows = conn.execute(
        text("SELECT id, profile_id FROM assay.ai_models WHERE id = ANY(:ids)"), {"ids": ids}
    ).all()
    return {r.id: str(r.profile_id) for r in rows}


@router.put("/admin/tenants/{tenant_id}/models", response_model=TenantModels)
def set_tenant_models(
    tenant_id: uuid.UUID, body: AvailabilityIn, caller: MasterAdmin, dbs: Dbs, request: Request
) -> TenantModels:
    wanted = {"ingest": list(dict.fromkeys(body.ingest)), "query": list(dict.fromkeys(body.query))}
    account = caller.account
    actor = audit.AuditActor(role=account.role, user_id=account.user_id, email=account.email)
    info = request_info(request)
    with dbs.platform_writer() as writer:
        tenant = _business_tenant(writer, tenant_id, lock=True)
        approved = writer.execute(
            text("SELECT id, jobs FROM assay.ai_models WHERE status = :active"), {"active": ACTIVE}
        ).all()
        jobs_of = {r.id: set(r.jobs) for r in approved}
        for job, ids in wanted.items():
            if any(job not in jobs_of.get(model, set()) for model in ids):
                raise _conflict("model_not_in_catalog")
        before: dict[str, list[uuid.UUID]] = {job: [] for job in JOBS}
        for row in writer.execute(
            text("SELECT model_id, job FROM assay.tenant_models WHERE tenant_id = :id"),
            {"id": tenant_id},
        ).all():
            before[str(row.job)].append(row.model_id)

        for job in JOBS:
            removed = [m for m in before[job] if m not in wanted[job]]
            added = [m for m in wanted[job] if m not in before[job]]
            if removed:
                writer.execute(
                    text(
                        "DELETE FROM assay.tenant_models WHERE tenant_id = :t AND job = :job "
                        "AND model_id = ANY(:ids)"
                    ),
                    {"t": tenant_id, "job": job, "ids": removed},
                )
            for model in added:
                writer.execute(
                    text(
                        "INSERT INTO assay.tenant_models (tenant_id, model_id, job, enabled_by) "
                        "VALUES (:t, :m, :job, :by)"
                    ),
                    {"t": tenant_id, "m": model, "job": job, "by": account.user_id},
                )

        names = _profiles(
            writer, [*before["ingest"], *before["query"], *wanted["ingest"], *wanted["query"]]
        )

        def profiles(by_job: dict[str, list[uuid.UUID]]) -> dict[str, list[str]]:
            return {job: sorted(names[m] for m in by_job[job]) for job in JOBS}

        if profiles(before) != profiles(wanted):
            audit.write(
                writer,
                tenant_id=tenant_id,
                actor=actor,
                category="tenant",
                action="models_available_changed",
                target_type="tenant",
                target_id=str(tenant_id),
                target_label=tenant.name,
                detail={"before": profiles(before), "after": profiles(wanted)},
                request=info,
            )

        # Exactly one selected model per job, from the available ones.
        selected = {"ingest": tenant.extraction_model, "query": tenant.answer_model}
        columns = {"ingest": "extraction_model", "query": "answer_model"}
        for job in JOBS:
            current = selected[job]
            if current in wanted[job]:
                continue
            replacement = wanted[job][0]
            writer.execute(
                text(f"UPDATE assay.tenants SET {columns[job]} = :m WHERE id = :id"),  # noqa: S608  # nosec B608
                {"m": replacement, "id": tenant_id},
            )
            audit.write(
                writer,
                tenant_id=tenant_id,
                actor=actor,
                category="tenant",
                action="model_selected",
                target_type="tenant",
                target_id=str(tenant_id),
                target_label=tenant.name,
                detail={
                    "job": job,
                    "before": names.get(current) if current else None,
                    "after": names[replacement],
                    "reason": "the selected model is no longer available",
                },
                request=info,
            )
    with dbs.platform() as reader:
        return _platform_view(reader, tenant_id)


# --- Tenant admin -----------------------------------------------------------------------------


class AvailableModel(CatalogModel):
    pass


class SettingsModels(BaseModel):
    """Per job, the models available to the caller's tenant and the one selected."""

    available: dict[str, list[AvailableModel]]
    selected: dict[str, uuid.UUID | None]


def _settings(caller: Caller) -> SettingsModels:
    conn = caller.conn
    rows = conn.execute(
        text(
            "SELECT tm.job, m.id AS model_id, m.name, m.profile_id, m.jobs, m.price_in, "
            "m.price_out FROM assay.tenant_models tm JOIN assay.ai_models m ON m.id = tm.model_id "
            "WHERE m.status = :active ORDER BY m.name"
        ),
        {"active": ACTIVE},
    ).all()
    available: dict[str, list[AvailableModel]] = {job: [] for job in JOBS}
    for r in rows:
        available[str(r.job)].append(AvailableModel(**_catalog([r])[0].model_dump()))
    tenant = conn.execute(
        text("SELECT extraction_model, answer_model FROM assay.tenants WHERE id = :id"),
        {"id": caller.account.tenant_id},
    ).one()
    return SettingsModels(
        available=available,
        selected={"ingest": tenant.extraction_model, "query": tenant.answer_model},
    )


@router.get("/settings/models", response_model=SettingsModels)
def settings_models(caller: TenantAdmin) -> SettingsModels:
    return _settings(caller)


@router.put("/settings/models", response_model=SettingsModels)
def select_models(body: SelectionIn, caller: TenantAdmin, request: Request) -> SettingsModels:
    conn = caller.conn
    current = _settings(caller)
    chosen = {"ingest": body.ingest_model, "query": body.query_model}
    names: dict[uuid.UUID, str] = {}
    for job, model in chosen.items():
        options = {m.id: m.profile_id for m in current.available[job]}
        if model not in options:
            # Not available to this tenant for this job (another tenant's list is not visible).
            raise _conflict("model_not_available")
        names.update(options)
    previous_ids = [m for m in current.selected.values() if m is not None]
    names.update({k: v for k, v in _profiles(conn, previous_ids).items() if k not in names})
    for job, column in (("ingest", "extraction_model"), ("query", "answer_model")):
        previous = current.selected[job]
        if chosen[job] == previous:
            continue
        conn.execute(
            text(f"UPDATE assay.tenants SET {column} = :m WHERE id = :id"),  # noqa: S608  # nosec B608
            {"m": chosen[job], "id": caller.account.tenant_id},
        )
        account = caller.account
        audit.write(
            conn,
            tenant_id=account.tenant_id,
            actor=audit.AuditActor(role=account.role, user_id=account.user_id, email=account.email),
            category="tenant",
            action="model_selected",
            target_type="tenant",
            target_id=str(account.tenant_id),
            target_label=account.tenant_name,
            detail={
                "job": job,
                "before": names.get(previous) if previous else None,
                "after": names[chosen[job]],
            },
            request=request_info(request),
        )
    return _settings(caller)
