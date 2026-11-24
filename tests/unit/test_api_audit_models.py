"""Audit history and Models (build step 5, second half), against a real PostgreSQL 17.

Uses the same database and app as test_api_step5.py (its `world` fixture), so the role
matrix below covers every route of build step 5.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import uuid
from typing import Any

import psycopg
import pytest

from assay_api import audit_read
from tests.unit.pgtest import requires_db
from tests.unit.test_api_step5 import HAIKU, OPUS, SONNET, USERS, World, req, token, world

pytestmark = requires_db
__all__ = ["world"]  # the fixture, re-used

SECRETS = ("Sup3r-Secret!", "eyJ-access-token", "temp-Pass-123", "654321", "refresh-xyz")


def insert_audit(w: World, **fields: Any) -> uuid.UUID:
    event = uuid.uuid4()
    values = {
        "tenant_id": w.a,
        "actor_email": "a-user@example.test",
        "actor_role": "tenant-user",
        "category": "auth",
        "action": "sign_in",
        "target_type": "user",
        "target_label": "a-user@example.test",
        "detail": None,
        "outcome": "success",
        "at": dt.datetime.now(dt.UTC),
    } | fields
    if isinstance(values["detail"], dict):
        values["detail"] = json.dumps(values["detail"])
    with psycopg.connect(w.url) as conn:
        conn.execute(
            "INSERT INTO assay.audit_log (id, tenant_id, at, actor_email, actor_role, category, "
            "action, target_type, target_label, detail, outcome) VALUES (%(id)s, %(tenant_id)s, "
            "%(at)s, %(actor_email)s, %(actor_role)s, %(category)s, %(action)s, %(target_type)s, "
            "%(target_label)s, %(detail)s, %(outcome)s)",
            {"id": event, **values},
        )
    return event


def audit_count(w: World) -> int:
    with psycopg.connect(w.url) as conn:
        row = conn.execute("SELECT count(*) FROM assay.audit_log").fetchone()
    assert row is not None
    return int(row[0])


# --- The role matrix: every route of build step 5 ------------------------------------------

ANY_ID = "00000000-0000-4000-8000-00000000abcd"
Route = tuple[str, str, dict[str, Any] | None]
TENANT_ADMIN_ONLY: list[Route] = [
    ("GET", "/api/users", None),
    ("POST", "/api/users", {}),
    ("PATCH", f"/api/users/{ANY_ID}", {"status": "active"}),
    ("POST", f"/api/users/{ANY_ID}/resend-invite", None),
    ("POST", f"/api/users/{ANY_ID}/reset-password", None),
    ("POST", f"/api/users/{ANY_ID}/reset-mfa", None),
    ("GET", "/api/settings/models", None),
    ("PUT", "/api/settings/models", {}),
]
PLATFORM_MEMBERS: list[Route] = [
    ("GET", "/api/admin/tenants", None),
    ("GET", f"/api/admin/tenants/{ANY_ID}/admins", None),
    ("GET", f"/api/admin/tenants/{ANY_ID}/models", None),
    ("GET", "/api/admin/audit", None),
]
MASTER_ADMIN_ONLY: list[Route] = [
    ("POST", "/api/admin/tenants", {}),
    ("PATCH", f"/api/admin/tenants/{ANY_ID}", {}),
    ("POST", f"/api/admin/tenants/{ANY_ID}/retry", None),
    ("POST", f"/api/admin/users/{ANY_ID}/reset-mfa", None),
    ("PUT", f"/api/admin/tenants/{ANY_ID}/models", {}),
]
EXPECTED: dict[str, dict[str, str | None]] = {
    # role -> refusal code per group (None: let through the pool and role gates)
    "m-admin": {"tenant_admin": "wrong_pool", "platform": None, "master_admin": None},
    "m-user": {"tenant_admin": "wrong_pool", "platform": None, "master_admin": "forbidden"},
    "a-admin": {"tenant_admin": None, "platform": "wrong_pool", "master_admin": "wrong_pool"},
    "a-user": {"tenant_admin": "forbidden", "platform": "wrong_pool", "master_admin": "wrong_pool"},
}
ROUTES = (
    [("tenant_admin", *r) for r in TENANT_ADMIN_ONLY]
    + [("platform", *r) for r in PLATFORM_MEMBERS]
    + [("master_admin", *r) for r in MASTER_ADMIN_ONLY]
)


@pytest.mark.parametrize("sub", list(EXPECTED))
@pytest.mark.parametrize(("group", "method", "path", "body"), ROUTES)
def test_role_matrix(
    world: World, sub: str, group: str, method: str, path: str, body: dict[str, Any] | None
) -> None:
    before = audit_count(world)
    response = req(world, method, path, sub, json=body)
    refused = EXPECTED[sub][group]
    if refused is None:
        # Through the gates: whatever the route says next (404, 409, 422 for the stand-in
        # IDs and empty bodies), it is not a refusal of the caller.
        assert response.status_code not in (401, 403), (response.status_code, response.text)
    else:
        assert (response.status_code, response.json()["detail"]) == (403, refused)
        assert audit_count(world) == before  # a refused call writes nothing


def test_a_tenant_admin_without_mfa_reaches_no_step5_route(world: World) -> None:
    headers = {"Authorization": f"Bearer {token(world, 'a-admin', mfa=False)}"}
    for method, path, body in TENANT_ADMIN_ONLY:
        response = world.client.request(method, path, headers=headers, json=body)
        assert (response.status_code, response.json()["detail"]) == (403, "mfa_required"), path


# --- Audit history --------------------------------------------------------------------------


def test_audit_defaults_to_the_last_seven_days_newest_first(world: World) -> None:
    old = insert_audit(world, at=dt.datetime.now(dt.UTC) - dt.timedelta(days=10), action="old")
    new = insert_audit(world, action="new_one")
    body = req(world, "GET", "/api/admin/audit", "m-user").json()
    assert (
        dt.datetime.fromisoformat(body["end"]) - dt.datetime.fromisoformat(body["start"])
    ) == dt.timedelta(days=7)
    ids = [e["id"] for e in body["events"]]
    assert str(new) in ids and str(old) not in ids
    times = [e["at"] for e in body["events"]]
    assert times == sorted(times, reverse=True)
    start = (dt.datetime.now(dt.UTC) - dt.timedelta(days=11)).isoformat()
    wider = req(world, "GET", "/api/admin/audit", "m-user", params={"from": start}).json()
    assert str(old) in [e["id"] for e in wider["events"]]


def test_tenant_users_email_is_shown_to_both_master_roles(world: World) -> None:
    """Owner decision (2026-11-23): D8 covers the admin list views, not the audit record."""
    event = insert_audit(world, actor_email="only-a-tenant-user@example.test")
    for sub in ("m-admin", "m-user"):
        events = req(world, "GET", "/api/admin/audit", sub).json()["events"]
        [row] = [e for e in events if e["id"] == str(event)]
        assert row["actor_email"] == "only-a-tenant-user@example.test"
        assert row["tenant_name"] == "Tenant A"


@pytest.mark.parametrize("size", [0, 51, 1000])
def test_page_size_is_capped_on_the_server(world: World, size: int) -> None:
    response = req(world, "GET", "/api/admin/audit", "m-admin", params={"page_size": size})
    assert response.status_code == 422


def test_pages_hold_at_most_fifty_rows(world: World) -> None:
    for _ in range(55):
        insert_audit(world, action="bulk")
    first = req(world, "GET", "/api/admin/audit", "m-admin", params={"page_size": 50}).json()
    assert len(first["events"]) == 50 and first["total"] >= 55
    second = req(world, "GET", "/api/admin/audit", "m-admin", params={"page": 2}).json()
    assert {e["id"] for e in first["events"]}.isdisjoint({e["id"] for e in second["events"]})


def test_filters_are_values_never_sql(world: World) -> None:
    marker = insert_audit(
        world, actor_email="Percent%Owner@Example.test", category="user", action="role_changed"
    )
    base = "/api/admin/audit"
    # A case-insensitive contains-match on the actor's email; % and _ match only themselves.
    match = req(world, "GET", base, "m-admin", params={"actor_email": "percent%owner"}).json()
    assert [e["id"] for e in match["events"]] == [str(marker)]
    everything = req(world, "GET", base, "m-admin", params={"actor_email": "%"}).json()
    assert [e["id"] for e in everything["events"]] == [str(marker)]
    for attempt in ("' OR '1'='1", "x'; DROP TABLE assay.audit_log; --", "\\"):
        response = req(world, "GET", base, "m-admin", params={"actor_email": attempt})
        assert response.status_code == 200 and response.json()["total"] == 0, attempt
    by_category = req(world, "GET", base, "m-admin", params={"category": "user"}).json()
    assert all(e["category"] == "user" for e in by_category["events"])
    by_tenant = req(world, "GET", base, "m-admin", params={"tenant_id": str(world.b)}).json()
    assert all(e["tenant_id"] == str(world.b) for e in by_tenant["events"])
    for bad in ({"sort": "at; DROP TABLE x"}, {"category": "everything"}, {"tenant_id": "x"}):
        assert req(world, "GET", base, "m-admin", params=bad).status_code == 422, bad
    with psycopg.connect(world.url) as conn:
        assert conn.execute("SELECT to_regclass('assay.audit_log')").fetchone() == (
            "assay.audit_log",
        )


def test_reading_audit_history_writes_nothing(world: World) -> None:
    before = audit_count(world)
    for sub in ("m-admin", "m-user"):
        assert req(world, "GET", "/api/admin/audit", sub).status_code == 200
    assert audit_count(world) == before


def test_secrets_never_appear_in_the_row_panel_or_the_export(world: World) -> None:
    """Nothing secret is written to audit_log (section 7a). If something ever were, the
    before and after values shown would still not carry it."""
    detail = {
        "before": {"password": SECRETS[0], "temporaryPassword": SECRETS[2], "role": "tenant-user"},
        "after": {
            "nested": [{"access_token": SECRETS[1]}, {"refreshToken": SECRETS[4]}],
            "code": SECRETS[3],
            "error_code": "kept",
            "secret_answer": SECRETS[0],
        },
    }
    event = insert_audit(world, category="user", action="role_changed", detail=detail)
    events = req(world, "GET", "/api/admin/audit", "m-admin").json()["events"]
    [row] = [e for e in events if e["id"] == str(event)]
    assert row["detail"]["before"]["role"] == "tenant-user"
    assert row["detail"]["after"]["error_code"] == "kept"
    assert row["detail"]["before"]["password"] == audit_read.REDACTED
    exported = req(world, "GET", "/api/admin/audit", "m-admin", params={"format": "csv"}).text
    shown = json.dumps(row) + exported
    for secret in SECRETS:
        assert secret not in shown, secret


def test_no_route_of_this_step_writes_a_secret_into_audit_log(world: World) -> None:
    """Every audit row the tests above caused, checked against the same rule."""
    with psycopg.connect(world.url) as conn:
        details = [
            r[0]
            for r in conn.execute("SELECT detail FROM assay.audit_log WHERE detail IS NOT NULL")
        ]
    for detail in details:
        if any(s in json.dumps(detail) for s in SECRETS):
            continue  # the planted rows of the test above
        assert audit_read.redact(detail) == detail, detail


def test_csv_export_is_capped_escaped_and_audited(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_audit(world, target_label='=HYPERLINK("http://x.test")', action="formula")
    response = req(
        world, "GET", "/api/admin/audit", "m-user", params={"format": "csv", "category": "auth"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == list(audit_read.CSV_COLUMNS)
    assert any(r[6] == '\'=HYPERLINK("http://x.test")' for r in rows[1:])
    assert int(response.headers["x-row-count"]) == len(rows) - 1

    with psycopg.connect(world.url) as conn:
        exported = conn.execute(
            "SELECT actor_email, actor_role, detail, tenant_id FROM assay.audit_log "
            "WHERE category = 'audit' AND action = 'exported' ORDER BY at DESC LIMIT 1"
        ).fetchone()
    assert exported is not None
    actor, role, detail, tenant_id = exported
    assert (actor, role, tenant_id) == ("m-user@example.test", "master-user", world.platform)
    assert detail["filters"]["category"] == "auth"
    assert detail["rows"] == len(rows) - 1
    assert detail["limit"] == 10_000

    monkeypatch.setattr(audit_read, "CSV_MAX_ROWS", 3)
    capped = req(world, "GET", "/api/admin/audit", "m-admin", params={"format": "csv"})
    assert len(list(csv.reader(io.StringIO(capped.text)))) == 1 + 3


def test_an_export_row_shows_in_audit_history_under_its_own_category(world: World) -> None:
    req(world, "GET", "/api/admin/audit", "m-admin", params={"format": "csv"})
    events = req(world, "GET", "/api/admin/audit", "m-admin", params={"category": "audit"}).json()[
        "events"
    ]
    assert events and events[0]["action"] == "exported"


# --- Models ----------------------------------------------------------------------------------


def test_master_user_reads_but_cannot_change_a_tenants_models(world: World) -> None:
    body = req(world, "GET", f"/api/admin/tenants/{world.a}/models", "m-user").json()
    assert {m["id"] for m in body["catalog"]} == {str(HAIKU), str(SONNET), str(OPUS)}
    assert body["available"] == {"ingest": [str(HAIKU)], "query": [str(SONNET)]}
    assert body["selected"] == {"ingest": str(HAIKU), "query": str(SONNET)}
    refused = req(
        world,
        "PUT",
        f"/api/admin/tenants/{world.a}/models",
        "m-user",
        json={"ingest": [str(HAIKU)], "query": [str(OPUS)]},
    )
    assert (refused.status_code, refused.json()["detail"]) == (403, "forbidden")
    platform = req(world, "GET", f"/api/admin/tenants/{world.platform}/models", "m-admin")
    assert platform.status_code == 404


def test_opus_for_one_tenant_only(world: World) -> None:
    """Section 13: the master admin makes Opus available to one tenant; that tenant's admin
    can select it for AI query, and the other tenant's admin cannot."""
    response = req(
        world,
        "PUT",
        f"/api/admin/tenants/{world.a}/models",
        "m-admin",
        json={"ingest": [str(HAIKU)], "query": [str(SONNET), str(OPUS)]},
    )
    assert response.status_code == 200, response.text
    assert sorted(response.json()["available"]["query"]) == sorted([str(SONNET), str(OPUS)])

    a_view = req(world, "GET", "/api/settings/models", "a-admin").json()
    assert {m["id"] for m in a_view["available"]["query"]} == {str(SONNET), str(OPUS)}
    b_view = req(world, "GET", "/api/settings/models", "b-admin").json()
    assert {m["id"] for m in b_view["available"]["query"]} == {str(SONNET)}

    refused = req(
        world,
        "PUT",
        "/api/settings/models",
        "b-admin",
        json={"ingest_model": str(HAIKU), "query_model": str(OPUS)},
    )
    assert (refused.status_code, refused.json()["detail"]) == (409, "model_not_available")
    chosen = req(
        world,
        "PUT",
        "/api/settings/models",
        "a-admin",
        json={"ingest_model": str(HAIKU), "query_model": str(OPUS)},
    )
    assert chosen.status_code == 200
    assert chosen.json()["selected"] == {"ingest": str(HAIKU), "query": str(OPUS)}

    with psycopg.connect(world.url) as conn:
        rows = conn.execute(
            "SELECT action, actor_role, detail FROM assay.audit_log WHERE tenant_id = %s "
            "AND action IN ('models_available_changed', 'model_selected') ORDER BY at",
            (world.a,),
        ).fetchall()
    assert [(a, r) for a, r, _ in rows] == [
        ("models_available_changed", "master-admin"),
        ("model_selected", "tenant-admin"),
    ]
    assert rows[0][2]["after"]["query"] == ["us.example.opus-v1", "us.example.sonnet-v1"]
    assert rows[1][2] == {
        "job": "query",
        "before": "us.example.sonnet-v1",
        "after": "us.example.opus-v1",
    }


def test_removing_the_selected_model_moves_the_selection_and_audits_both(world: World) -> None:
    with psycopg.connect(world.url) as conn:
        conn.execute("UPDATE assay.tenants SET answer_model = %s WHERE id = %s", (SONNET, world.b))
    response = req(
        world,
        "PUT",
        f"/api/admin/tenants/{world.b}/models",
        "m-admin",
        json={"ingest": [str(HAIKU)], "query": [str(OPUS)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["selected"]["query"] == str(OPUS)
    with psycopg.connect(world.url) as conn:
        rows = conn.execute(
            "SELECT action, detail FROM assay.audit_log WHERE tenant_id = %s "
            "AND action IN ('models_available_changed', 'model_selected') ORDER BY at",
            (world.b,),
        ).fetchall()
    assert [a for a, _ in rows][-2:] == ["models_available_changed", "model_selected"]
    assert rows[-1][1]["before"] == "us.example.sonnet-v1"
    assert rows[-1][1]["after"] == "us.example.opus-v1"


@pytest.mark.parametrize(
    "body",
    [
        {"ingest": [], "query": ["SONNET"]},
        {"ingest": ["HAIKU"]},
        {"ingest": ["HAIKU"], "query": ["SONNET"], "tenant_id": "x"},
    ],
)
def test_every_job_keeps_at_least_one_model(world: World, body: dict[str, Any]) -> None:
    ids = {"HAIKU": str(HAIKU), "SONNET": str(SONNET)}
    payload = {k: [ids[v] for v in vs] if isinstance(vs, list) else vs for k, vs in body.items()}
    response = req(world, "PUT", f"/api/admin/tenants/{world.a}/models", "m-admin", json=payload)
    assert response.status_code == 422


def test_only_catalog_models_can_be_made_available(world: World) -> None:
    response = req(
        world,
        "PUT",
        f"/api/admin/tenants/{world.a}/models",
        "m-admin",
        json={"ingest": [str(uuid.uuid4())], "query": [str(SONNET)]},
    )
    assert (response.status_code, response.json()["detail"]) == (409, "model_not_in_catalog")


def test_settings_models_are_for_tenant_admins_only(world: World) -> None:
    assert USERS["a-user"][1] == "tenant-user"
    response = req(world, "GET", "/api/settings/models", "a-user")
    assert (response.status_code, response.json()["detail"]) == (403, "forbidden")
