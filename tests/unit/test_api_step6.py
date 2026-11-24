"""Build step 6 routes against a real PostgreSQL 17: batches, uploads, history, the view link.

The API runs as `app_tenant` and `platform_reader` exactly as on Aurora. The TSS is a fake
that issues offline credentials, so upload and view links are really signed (no AWS call),
and the tests read the signature's scope from the URL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from assay_api.app import create_app
from assay_api.auth import TokenVerifier
from assay_api.database import Databases
from assay_db import batches
from assay_tss import keys
from assay_tss.models import StorageAccess
from assay_workers import scan
from tests.unit.fakes import FakeCognito, FakeProvisioning
from tests.unit.pgtest import requires_db
from tests.unit.step6 import (
    PREFIX,
    USERS,
    Db,
    FakeQueue,
    FakeStorage,
    checksum,
    minimal_pdf,
    scan_event,
    sha256_hex,
)
from tests.unit.tokens import make_issuer

pytestmark = requires_db

TENANT = make_issuer("tenant")
PLATFORM = make_issuer("platform")
VIEWER = {"CloudFront-Viewer-Address": "198.51.100.7:44321", "User-Agent": "pytest-browser"}


@dataclass
class World:
    db: Db
    client: TestClient
    storage: FakeStorage
    queue: FakeQueue

    def token(self, sub: str) -> str:
        tenant, role = USERS[sub]
        if tenant == "platform":
            return PLATFORM.mint(sub=sub, groups=[role])
        return TENANT.mint(sub=sub, groups=[role], tenant_id=self.db.tenants[tenant], mfa=True)

    def req(self, method: str, path: str, sub: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.token(sub)}", **VIEWER}
        return self.client.request(method, path, headers=headers, **kwargs)


@pytest.fixture(scope="module")
def w(step6_db: Db) -> Iterator[World]:
    storage, queue = FakeStorage(), FakeQueue()
    app = create_app(
        verifier=TokenVerifier([TENANT.settings, PLATFORM.settings]),
        databases=Databases(
            tenant_engine=step6_db.app_tenant,
            platform_engine=step6_db.platform_reader,
            writer_engine=step6_db.platform_writer,
        ),
        cognito=FakeCognito(),
        provisioning=FakeProvisioning(),
        storage=storage,
        ingest=queue,
    )
    with TestClient(app) as client:
        yield World(db=step6_db, client=client, storage=storage, queue=queue)


def fresh_batch(w: World, sub: str = "a-admin", name: str = "Batch") -> dict[str, Any]:
    current = w.req("GET", "/api/batches?status=open&mine=true", sub).json()["batches"]
    for b in current:
        assert w.req("DELETE", f"/api/batches/{b['id']}", sub).status_code == 204
    response = w.req("POST", "/api/batches", sub, json={"name": name})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def register(w: World, batch_id: str, files: list[tuple[str, bytes]], sub: str = "a-admin") -> Any:
    return w.req(
        "POST",
        f"/api/batches/{batch_id}/files",
        sub,
        json={
            "files": [
                {"name": name, "size_bytes": len(body), "sha256": sha256_hex(body)}
                for name, body in files
            ]
        },
    )


def upload_scan(w: World, tenant: str, registered: dict[str, Any], body: bytes) -> str:
    """The browser's PUT, then GuardDuty's clean result."""
    bucket = str(urlsplit(registered["upload"]["url"]).hostname).split(".")[0]
    key = urlsplit(registered["upload"]["url"]).path.lstrip("/")
    version = w.storage.buckets.put(bucket, key, body)
    deps = scan.Deps(engine=w.db.app_tenant, storage=w.storage, queue=w.queue, name_prefix=PREFIX)
    return scan.handle(scan_event(bucket, key, version), deps)


# --- Batches and upload links ----------------------------------------------------------------


def test_opening_a_batch_and_one_open_batch_per_user(w: World) -> None:
    batch = fresh_batch(w, name="  October   intake ")
    assert (batch["status"], batch["name"], batch["mine"]) == ("open", "October intake", True)
    assert batch["closes_by"] is not None
    assert w.req("POST", "/api/batches", "a-admin", json={}).json() == {
        "detail": "batch_already_open"
    }


def test_an_upload_link_is_signed_for_exactly_the_registered_content(w: World) -> None:
    """Condition (a): size, type and SHA-256 are in the signature."""
    batch = fresh_batch(w)
    body = minimal_pdf(text="link")
    response = register(w, batch["id"], [("Smith Package.pdf", body)])
    assert response.status_code == 201, response.text
    [registered] = response.json()["files"]
    url = urlsplit(registered["upload"]["url"])
    query = parse_qs(url.query)
    signed = query["X-Amz-SignedHeaders"][0].split(";")
    assert {"content-length", "content-type", "x-amz-checksum-sha256", "host"} <= set(signed)
    assert int(query["X-Amz-Expires"][0]) <= 900
    assert registered["upload"]["headers"] == {
        "Content-Type": "application/pdf",
        "x-amz-checksum-sha256": checksum(body),
    }
    # A regional, virtual-hosted name, as the CSP admits; the key is IDs only.
    tenant = w.db.tenants["a"]
    assert url.hostname == f"{PREFIX}-t-{tenant.hex[:8]}.s3.us-east-1.amazonaws.com"
    assert url.path == f"/inbox/{batch['id']}/{registered['id']}"
    assert "Smith" not in registered["upload"]["url"]
    # The link was signed with an UPLOAD context for the token's tenant.
    assert w.storage.asked[-1][:2] == (tenant, StorageAccess.UPLOAD)


@pytest.mark.parametrize(
    ("files", "status", "code"),
    [
        ([("a.docx", b"x")], 422, "unsupported_file_type"),
        ([("../a.pdf", b"x")], 422, "invalid_file_name"),
        ([(".pdf", b"x")], 422, "invalid_file_name"),
        ([("big.json", b"x" * (batches.COMPANION_MAX_BYTES + 1))], 422, "file_too_large"),
        ([("a.pdf", b"x"), ("A.PDF", b"y")], 409, "duplicate_file_name"),
    ],
)
def test_registration_limits(
    w: World, files: list[tuple[str, bytes]], status: int, code: str
) -> None:
    batch = fresh_batch(w)
    response = register(w, batch["id"], files)
    assert (response.status_code, response.json()) == (status, {"detail": code})


def test_a_pdf_over_100_mb_and_too_many_files_are_refused(w: World) -> None:
    batch = fresh_batch(w)
    big = {"name": "big.pdf", "size_bytes": batches.PDF_MAX_BYTES + 1, "sha256": "0" * 64}
    response = w.req("POST", f"/api/batches/{batch['id']}/files", "a-admin", json={"files": [big]})
    assert response.status_code == 422
    many = [
        {"name": f"f{i}.pdf", "size_bytes": 10, "sha256": "0" * 64}
        for i in range(batches.MAX_FILES_PER_REQUEST + 1)
    ]
    response = w.req("POST", f"/api/batches/{batch['id']}/files", "a-admin", json={"files": many})
    assert response.status_code == 422


def test_at_most_200_pdfs_in_a_batch(w: World) -> None:
    batch = fresh_batch(w)
    for start in range(0, batches.MAX_PDFS_PER_BATCH, batches.MAX_FILES_PER_REQUEST):
        files = [(f"p{start + i}.pdf", f"body {start + i}".encode()) for i in range(50)]
        assert register(w, batch["id"], files).status_code == 201
    assert register(w, batch["id"], [("one-more.pdf", b"x")]).json() == {"detail": "too_many_pdfs"}


def test_only_the_admin_who_opened_a_batch_may_change_it(w: World) -> None:
    batch = fresh_batch(w, "a-admin")
    path = f"/api/batches/{batch['id']}"
    for method, suffix in (("POST", "/files"), ("POST", "/close"), ("DELETE", "")):
        kwargs: dict[str, Any] = {}
        if suffix == "/files":
            kwargs["json"] = {"files": [{"name": "x.pdf", "size_bytes": 1, "sha256": "0" * 64}]}
        response = w.req(method, path + suffix, "a-admin2", **kwargs)
        assert response.json() == {"detail": "not_batch_owner"}, (method, suffix)


def test_another_tenants_batch_is_not_found(w: World) -> None:
    batch = fresh_batch(w, "a-admin")
    for method, suffix in (("GET", ""), ("POST", "/close"), ("DELETE", "")):
        response = w.req(method, f"/api/batches/{batch['id']}{suffix}", "b-admin")
        assert response.status_code == 404, (method, suffix)


def test_a_document_id_is_never_the_reserved_id(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a document refuses the isolation tests' reserved ID."""
    batch = fresh_batch(w)
    ids = iter([keys.RESERVED_DOCUMENT_ID, uuid.uuid4()])
    monkeypatch.setattr(uuid, "uuid4", lambda: next(ids))
    [registered] = register(w, batch["id"], [("r.pdf", b"%PDF-1.7 r")]).json()["files"]
    assert registered["id"] != str(keys.RESERVED_DOCUMENT_ID)
    assert w.db.row(
        "SELECT count(*) FROM assay.documents WHERE id = %s", keys.RESERVED_DOCUMENT_ID
    ) == (0,)


def test_close_waits_for_scans_then_queues_after_the_commit(w: World) -> None:
    batch = fresh_batch(w)
    body = minimal_pdf(text="close")
    [registered] = register(w, batch["id"], [("close.pdf", body)]).json()["files"]
    assert w.req("POST", f"/api/batches/{batch['id']}/close", "a-admin").json() == {
        "detail": "batch_not_ready"
    }
    detail = w.req("GET", f"/api/batches/{batch['id']}", "a-admin").json()
    assert detail["problems"] == [{"file_id": registered["id"], "code": "awaiting_scan"}]
    assert upload_scan(w, "a", registered, body) == "clean"
    response = w.req("POST", f"/api/batches/{batch['id']}/close", "a-admin")
    assert response.status_code == 200 and response.json()["status"] == "closed"
    assert [str(j.document_id) for j in w.queue.sent][-1] == registered["id"]


def test_a_companion_shows_as_matched(w: World) -> None:
    batch = fresh_batch(w)
    files = register(w, batch["id"], [("m.pdf", b"%PDF m"), ("M.json", b"{}")]).json()["files"]
    detail = w.req("GET", f"/api/batches/{batch['id']}", "a-admin").json()
    assert {f["name"]: f["matched"] for f in detail["files"]} == {"m.pdf": True, "M.json": True}
    assert (
        w.req("DELETE", f"/api/batches/{batch['id']}/files/{files[1]['id']}", "a-admin").status_code
        == 204
    )
    detail = w.req("GET", f"/api/batches/{batch['id']}", "a-admin").json()
    assert {f["name"]: f["matched"] for f in detail["files"]} == {"m.pdf": False}


def test_discarding_leaves_documents_out_of_the_default_history(w: World) -> None:
    batch = fresh_batch(w)
    [registered] = register(w, batch["id"], [("gone.pdf", b"%PDF gone")]).json()["files"]
    assert w.req("DELETE", f"/api/batches/{batch['id']}", "a-admin").status_code == 204
    ids = [d["id"] for d in w.req("GET", "/api/documents", "a-user").json()["documents"]]
    assert registered["id"] not in ids
    shown = w.req("GET", "/api/documents?status=discarded", "a-user").json()["documents"]
    assert registered["id"] in [d["id"] for d in shown]


# --- History, detail and the view link ---------------------------------------------------------


@pytest.fixture(scope="module")
def ready_doc(w: World) -> dict[str, Any]:
    """One document fingerprinted into originals/ in tenant A."""
    from assay_workers import steps

    batch = fresh_batch(w, "a-admin2", "History")
    body = minimal_pdf(text="history")
    [registered] = register(w, batch["id"], [("History=Report.pdf", body)], "a-admin2").json()[
        "files"
    ]
    upload_scan(w, "a", registered, body)
    w.req("POST", f"/api/batches/{batch['id']}/close", "a-admin2")
    steps.handle(
        {
            "step": "fingerprint",
            "tenant_id": str(w.db.tenants["a"]),
            "document_id": registered["id"],
        },
        steps.Deps(engine=w.db.app_tenant, storage=w.storage),
    )
    return {"id": registered["id"], "batch_id": batch["id"]}


def test_history_for_tenant_members(w: World, ready_doc: dict[str, Any]) -> None:
    for sub in ("a-admin", "a-user"):
        page = w.req("GET", f"/api/documents?batch_id={ready_doc['batch_id']}", sub).json()
        [row] = page["documents"]
        assert row["file_name"] == "History=Report.pdf"
        assert (row["status"], row["page_count"], row["uploaded_by"]) == (
            "processing",
            2,
            "Name a-admin2",
        )
    others = w.req("GET", "/api/documents?status=processing", "b-user").json()["documents"]
    assert ready_doc["id"] not in [d["id"] for d in others]


@pytest.mark.parametrize(
    "sort",
    [
        "received",
        "-received",
        "file_name",
        "-status",
        "duration",
        "forms",
        "uploaded_by",
        "-batch",
        "size",
        "pages",
        "companion",
    ],
)
def test_history_sorts_by_every_column(w: World, ready_doc: dict[str, Any], sort: str) -> None:
    response = w.req("GET", f"/api/documents?sort={sort}", "a-user")
    assert response.status_code == 200, response.text


def test_history_csv_escapes_formulas(w: World, ready_doc: dict[str, Any]) -> None:
    response = w.req("GET", f"/api/documents?format=csv&batch_id={ready_doc['batch_id']}", "a-user")
    assert response.headers["content-type"].startswith("text/csv")
    assert "History=Report.pdf" in response.text  # starts with H: not a formula


def test_a_guessed_document_id_from_another_tenant_is_not_found(
    w: World, ready_doc: dict[str, Any]
) -> None:
    for path in (f"/api/documents/{ready_doc['id']}", f"/api/documents/{ready_doc['id']}/pdf-url"):
        assert w.req("GET", path, "b-user").status_code == 404


def test_the_view_link_is_recorded_short_lived_and_always_a_pdf(
    w: World, ready_doc: dict[str, Any]
) -> None:
    """Condition (f)."""
    response = w.req("GET", f"/api/documents/{ready_doc['id']}/pdf-url", "a-user")
    assert response.status_code == 200, response.text
    link = response.json()
    url = urlsplit(link["url"])
    query = parse_qs(url.query)
    assert url.path == f"/originals/{ready_doc['id']}.pdf"
    assert int(query["X-Amz-Expires"][0]) <= 300
    assert query["response-content-type"] == ["application/pdf"]
    assert query["response-content-disposition"][0].startswith("inline;")
    assert w.storage.asked[-1][1] is StorageAccess.READ
    [viewed] = w.db.rows(
        "SELECT detail FROM assay.document_events WHERE document_id = %s AND step = 'viewed'",
        uuid.UUID(ready_doc["id"]),
    )
    assert viewed[0]["viewer"] == "a-user@example.test"
    assert viewed[0]["user_id"] == str(w.db.users["a-user"])
    assert (viewed[0]["ip_address"], viewed[0]["user_agent"]) == ("198.51.100.7", "pytest-browser")
    detail = w.req("GET", f"/api/documents/{ready_doc['id']}", "a-admin").json()
    assert detail["pdf_available"] is True
    assert [e["viewer"] for e in detail["events"] if e["step"] == "viewed"] == [
        "a-user@example.test"
    ]


def test_the_view_link_is_refused_for_anything_not_in_originals(w: World) -> None:
    batch = fresh_batch(w)
    [registered] = register(w, batch["id"], [("raw.pdf", b"%PDF raw")]).json()["files"]
    response = w.req("GET", f"/api/documents/{registered['id']}/pdf-url", "a-admin")
    assert response.json() == {"detail": "pdf_not_available"}
    w.db.execute(
        "UPDATE assay.documents SET status = 'quarantined', s3_key = %s WHERE id = %s",
        keys.quarantine_key(uuid.UUID(registered["id"])),
        uuid.UUID(registered["id"]),
    )
    response = w.req("GET", f"/api/documents/{registered['id']}/pdf-url", "a-admin")
    assert response.json() == {"detail": "pdf_not_available"}


# --- The master view -------------------------------------------------------------------------


def test_the_master_view_shows_metadata_only(w: World, ready_doc: dict[str, Any]) -> None:
    for sub in ("m-admin", "m-user"):
        response = w.req("GET", f"/api/admin/ingestion?tenant_id={w.db.tenants['a']}", sub)
        assert response.status_code == 200, response.text
        rows = response.json()["documents"]
        row = next(r for r in rows if r["document_id"] == ready_doc["id"])
        assert row["tenant_name"] == "Tenant A"
        text = response.text
        assert (
            "History=Report" not in text and "file_name" not in text and "uploaded_by" not in text
        )
        csv = w.req("GET", "/api/admin/ingestion?format=csv", sub).text
        assert "History=Report" not in csv
    detail = w.req("GET", f"/api/admin/ingestion/{ready_doc['id']}", "m-user").json()
    steps_seen = [e["step"] for e in detail["events"]]
    assert "fingerprint" in steps_seen and "viewed" not in steps_seen


def test_the_master_view_filters_by_tenant(w: World, ready_doc: dict[str, Any]) -> None:
    other = w.req("GET", f"/api/admin/ingestion?tenant_id={w.db.tenants['b']}", "m-user").json()
    assert all(r["tenant_id"] == str(w.db.tenants["b"]) for r in other["documents"])


@pytest.mark.parametrize(
    ("sub", "method", "path", "status"),
    [
        ("a-user", "POST", "/api/batches", 403),
        ("a-user", "GET", "/api/batches/00000000-0000-4000-8000-000000000001", 403),
        ("a-user", "GET", "/api/batches", 200),
        ("m-admin", "GET", "/api/documents", 403),
        ("m-admin", "POST", "/api/batches", 403),
        ("a-admin", "GET", "/api/admin/ingestion", 403),
        ("a-user", "GET", "/api/admin/ingestion", 403),
        ("s-admin", "POST", "/api/batches", 403),  # suspended tenant: no uploads
        ("s-admin", "GET", "/api/documents", 403),
    ],
)
def test_role_matrix(w: World, sub: str, method: str, path: str, status: int) -> None:
    kwargs: dict[str, Any] = {"json": {}} if method == "POST" else {}
    assert w.req(method, path, sub, **kwargs).status_code == status


def test_a_range_must_have_a_time_zone(w: World) -> None:
    naive = dt.datetime(2026, 11, 1).isoformat()
    assert w.req("GET", f"/api/documents?from={naive}", "a-user").status_code == 422
