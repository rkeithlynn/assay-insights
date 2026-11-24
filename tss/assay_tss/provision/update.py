"""`PROVISION_COMMAND=update`: apply a tenant stack program change to existing tenants.

A change to the tenant stack program reaches existing tenants only when their stack runs
again. This command does that for each active business tenant, or only those named in
`UPDATE_TENANTS` (a comma-separated list of tenant IDs), as the provisioning role. It writes
nothing to the database and invites no one.

For each tenant it first previews the stack and checks every planned change against
`ALLOWED_CHANGES`, the changes the program change is meant to make. Build step 6 allows
exactly two:

- create `bucket-cors` (the bucket's CORS rule for the tenant portal's origin);
- update `malware-protection`, its protected resource only (scan `inbox/` only).

Any other planned change (another resource, a replacement, a deletion, a drifted setting)
refuses that tenant: the changes are logged and nothing is applied. When every change is
allowed, `up` runs with `--target` set to exactly those resources, so nothing else can change
even if the state moves between the preview and the update. With `UPDATE_APPLY=false` it
only previews and logs.

Started by `scripts/update_tenants.py`. Exits 1 if any tenant was refused or failed; the
others still run.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Protocol

from sqlalchemy import Engine, text

from assay_tss import naming
from assay_tss.provision.settings import ProvisionSettings
from assay_tss.provision.stack import PlannedChange, StackOutputs

log = logging.getLogger("assay_tss.provision.update")

# (resource, operation) -> the inputs that may change (None: any, for a resource created).
# Change this together with the tenant stack program.
ALLOWED_CHANGES: Mapping[tuple[str, str], frozenset[str] | None] = {
    ("bucket-cors", "create"): None,
    ("malware-protection", "update"): frozenset({"protectedResource"}),
}


class Stacks(Protocol):
    def plan(self, tenant_id: uuid.UUID) -> list[PlannedChange]: ...

    def apply(self, tenant_id: uuid.UUID, urns: Sequence[str]) -> StackOutputs: ...


def parse_tenants(value: str) -> tuple[uuid.UUID, ...]:
    return tuple(uuid.UUID(part.strip()) for part in value.split(",") if part.strip())


def parse_apply(value: str) -> bool:
    if value.strip().lower() in ("", "true", "1", "yes"):
        return True
    if value.strip().lower() in ("false", "0", "no"):
        return False
    raise ValueError("UPDATE_APPLY must be true or false")


def active_tenants(engine: Engine) -> list[uuid.UUID]:
    """Business tenants that are active, oldest first (as platform_writer)."""
    with engine.begin() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(
            text(
                "SELECT id FROM assay.tenants WHERE kind = 'business' AND status = 'active' "
                "ORDER BY created_at, id"
            )
        ).all()
    return [uuid.UUID(str(r.id)) for r in rows]


def unexpected(changes: Sequence[PlannedChange]) -> list[PlannedChange]:
    out = []
    for change in changes:
        key = (change.name, change.op)
        if key not in ALLOWED_CHANGES:
            out.append(change)
            continue
        allowed = ALLOWED_CHANGES[key]
        if allowed is not None and not set(change.diffs) <= allowed:
            out.append(change)
    return out


def _check_names(settings: ProvisionSettings, tenant_id: uuid.UUID, outputs: StackOutputs) -> None:
    expected = {
        "bucket": naming.bucket_name(settings.name_prefix, tenant_id),
        "vector_bucket": naming.vector_bucket_name(settings.name_prefix, tenant_id),
        "vector_index": naming.VECTOR_INDEX,
        "face_collection": naming.face_collection_id(settings.name_prefix, tenant_id),
    }
    wrong = sorted(k for k, v in expected.items() if getattr(outputs, k) != v)
    if wrong:
        raise RuntimeError(f"tenant stack returned unexpected {', '.join(wrong)}")


def update_tenant(
    settings: ProvisionSettings, stacks: Stacks, tenant_id: uuid.UUID, *, apply: bool
) -> bool:
    """True when the tenant ended as intended (updated, previewed, or nothing to do)."""
    fields = {"tenant_id": str(tenant_id)}
    try:
        changes = stacks.plan(tenant_id)
    except Exception as exc:
        log.error("tenant preview failed", extra=fields | {"error": type(exc).__name__})
        return False
    for change in changes:
        log.info(
            "planned change",
            extra=fields
            | {
                "resource": change.name,
                "type": change.type,
                "op": change.op,
                "diffs": change.diffs,
            },
        )
    refused = unexpected(changes)
    if refused:
        log.error(
            "unexpected changes; tenant not updated",
            extra=fields | {"changes": [f"{c.op} {c.name} {list(c.diffs)}" for c in refused]},
        )
        return False
    if not changes:
        log.info("nothing to change", extra=fields)
        return True
    if not apply:
        log.info("previewed; not applied", extra=fields | {"changes": len(changes)})
        return True
    try:
        _check_names(settings, tenant_id, stacks.apply(tenant_id, [c.urn for c in changes]))
    except Exception as exc:
        log.error("tenant update failed", extra=fields | {"error": type(exc).__name__})
        return False
    log.info("tenant updated", extra=fields | {"changes": len(changes)})
    return True


def run_update(
    settings: ProvisionSettings,
    engine: Engine,
    stacks: Stacks,
    only: Sequence[uuid.UUID] = (),
    *,
    apply: bool = True,
) -> int:
    tenants = active_tenants(engine)
    ok = True
    if only:
        unknown = sorted(str(t) for t in set(only) - set(tenants))
        if unknown:
            log.error("not active business tenants; not updated", extra={"tenants": unknown})
            ok = False
        tenants = [t for t in tenants if t in set(only)]
    failed = sum(not update_tenant(settings, stacks, t, apply=apply) for t in tenants)
    log.info(
        "update finished",
        extra={"tenants": len(tenants), "failed": failed, "applied": apply},
    )
    return 0 if ok and not failed else 1
