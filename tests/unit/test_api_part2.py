"""Build step 6, second half, stage 1 part 2: the tenant admin's document actions through the
API ("Process again", "Mark as reviewed", attach or replace the companion file), against a
real PostgreSQL with fake storage. Owner's conditions: an audit row for each, tenant admins
only, no file name or content anywhere a master role can read."""

from __future__ import annotations

import csv
import io
import json
import uuid
from typing import Any

import pytest

from assay_tss import keys
from tests.unit.pgtest import requires_db
from tests.unit.step6 import minimal_pdf, sha256_hex
from tests.unit.test_api_step6 import World, ready_doc, upload_scan, w

pytestmark = requires_db
__all__ = ["ready_doc", "w"]  # fixtures, used by name below

MISSING = "00000000-0000-4000-8000-0000000000aa"
COMPANION = {
    "ApplicationId": "APP-TEST-004",
    "LoanTerm": 360,
    "LoanInterestRate": 6.125,
    "Signer1Name": "Jane Smith",
    "Branch": "Atlanta",
}


def settle(w: World, document_id: str) -> None:
    """Let the document's last event be old enough that its attempt counts as ended."""
    w.db.execute(
        "UPDATE assay.document_events SET at = at - interval '1 hour' WHERE document_id = %s",
        document_id,
    )
    w.db.execute(
        "UPDATE assay.worker_grants SET ended_at = now() WHERE document_id = %s "
        "AND ended_at IS NULL",
        document_id,
    )


def audit(w: World, document_id: str) -> list[tuple[Any, ...]]:
    return w.db.rows(
        "SELECT action, actor_email, actor_role, detail, host(ip_address), user_agent "
        "FROM assay.audit_log WHERE category = 'document' AND target_id = %s ORDER BY at",
        document_id,
    )


def companion_file(w: World, body: bytes, name: str = "replacement.json") -> dict[str, Any]:
    return {"name": name, "size_bytes": len(body), "sha256": sha256_hex(body)}


def body_for(w: World, **values: object) -> bytes:
    return json.dumps({"TenantID": str(w.db.tenants["a"]), **values}).encode()


# --- Who may ------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["reprocess", "reviewed", "companion"])
@pytest.mark.parametrize(
    ("sub", "status", "code"),
    [
        ("a-user", 403, None),  # a tenant user
        ("m-admin", 403, None),  # no master role, ever (owner decision 3)
        ("m-user", 403, None),
        ("b-admin", 404, "document_not_found"),  # another tenant's admin: it does not exist
        ("s-admin", 403, None),  # a suspended tenant
    ],
)
def test_only_the_documents_own_tenant_admins_may_act_on_it(
    w: World, ready_doc: dict[str, Any], action: str, sub: str, status: int, code: str | None
) -> None:
    before = audit(w, ready_doc["id"])
    attempt = w.db.row("SELECT attempt FROM assay.documents WHERE id = %s", ready_doc["id"])
    payload = companion_file(w, body_for(w)) if action == "companion" else None
    response = w.req("POST", f"/api/documents/{ready_doc['id']}/{action}", sub, json=payload)
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["detail"] == code
    # A refused call changes nothing and writes nothing.
    assert audit(w, ready_doc["id"]) == before
    assert w.db.row("SELECT attempt FROM assay.documents WHERE id = %s", ready_doc["id"]) == attempt


@pytest.mark.parametrize("action", ["reprocess", "reviewed", "companion"])
def test_the_reserved_document_and_a_missing_one_are_not_found(w: World, action: str) -> None:
    payload = companion_file(w, body_for(w)) if action == "companion" else None
    for document in (keys.RESERVED_DOCUMENT_ID, MISSING):
        response = w.req("POST", f"/api/documents/{document}/{action}", "a-admin", json=payload)
        assert (response.status_code, response.json()["detail"]) == (404, "document_not_found")


# --- Process again ------------------------------------------------------------------------------


def test_process_again_starts_an_attempt_queues_it_and_is_audited(
    w: World, ready_doc: dict[str, Any]
) -> None:
    document = ready_doc["id"]
    # Under way a moment ago: refused, and nothing queued.
    queued = len(w.queue.sent)
    busy = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
    assert (busy.status_code, busy.json()["detail"]) == (409, "attempt_running")
    assert len(w.queue.sent) == queued

    settle(w, document)
    before = audit(w, document)
    response = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
    assert response.status_code == 202, response.text
    assert response.json() == {"document_id": document, "attempt": 2}
    # Queued after the commit, as an ordinary ingest job (IDs only).
    assert [(str(j.document_id), str(j.tenant_id)) for j in w.queue.sent[queued:]] == [
        (document, str(w.db.tenants["a"]))
    ]
    [(action, email, role, detail, ip, agent)] = audit(w, document)[len(before) :]
    assert (action, role) == ("reprocess_requested", "tenant-admin")
    assert email and (ip, agent) == ("198.51.100.7", "pytest-browser")
    assert detail == {
        "before": {"status": "processing", "attempt": 1},
        "after": {"status": "processing", "attempt": 2},
    }
    shown = w.req("GET", f"/api/documents/{document}", "a-user").json()
    assert shown["attempt"] == 2
    assert {event["attempt"] for event in shown["events"]} == {1, 2}
    assert shown["events"][-1]["step"] == "queued"
    # And at once again: the new attempt is under way.
    again = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
    assert again.json()["detail"] == "attempt_running"


@pytest.mark.parametrize("status", ["quarantined", "on_hold", "duplicate", "discarded"])
def test_process_again_is_refused_by_status(
    w: World, ready_doc: dict[str, Any], status: str
) -> None:
    document = ready_doc["id"]
    previous = w.db.row("SELECT status FROM assay.documents WHERE id = %s", document)
    assert previous is not None
    w.db.execute("UPDATE assay.documents SET status = %s WHERE id = %s", status, document)
    try:
        response = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
        assert (response.status_code, response.json()["detail"]) == (409, "not_reprocessable")
    finally:
        w.db.execute("UPDATE assay.documents SET status = %s WHERE id = %s", previous[0], document)


# --- Mark as reviewed ---------------------------------------------------------------------------


def test_mark_as_reviewed_names_the_user_and_works_once(
    w: World, ready_doc: dict[str, Any]
) -> None:
    document = ready_doc["id"]
    refused = w.req("POST", f"/api/documents/{document}/reviewed", "a-admin")
    assert (refused.status_code, refused.json()["detail"]) == (409, "not_needs_review")
    previous = w.db.row("SELECT status FROM assay.documents WHERE id = %s", document)
    assert previous is not None
    w.db.execute("UPDATE assay.documents SET status = 'needs_review' WHERE id = %s", document)
    try:
        before = audit(w, document)
        assert w.req("POST", f"/api/documents/{document}/reviewed", "a-admin").status_code == 204
        assert w.req("GET", f"/api/documents/{document}", "a-user").json()["status"] == "ready"
        [(action, email, role, detail, *_)] = audit(w, document)[len(before) :]
        assert (action, role) == ("marked_reviewed", "tenant-admin")
        assert email
        assert detail == {"before": {"status": "needs_review"}, "after": {"status": "ready"}}
        second = w.req("POST", f"/api/documents/{document}/reviewed", "a-admin")
        assert second.json()["detail"] == "not_needs_review"
    finally:
        w.db.execute("UPDATE assay.documents SET status = %s WHERE id = %s", previous[0], document)


# --- The companion file -------------------------------------------------------------------------


def test_a_companion_is_attached_through_a_signed_upload_and_shown_to_the_tenant_only(
    w: World, ready_doc: dict[str, Any]
) -> None:
    document = ready_doc["id"]
    content = body_for(w, **COMPANION)
    before = audit(w, document)

    for bad, code in (
        ({"name": "x.pdf", "size_bytes": 10, "sha256": "0" * 64}, "unsupported_file_type"),
        ({"name": "x.json", "size_bytes": 64 * 1024 + 1, "sha256": "0" * 64}, "file_too_large"),
    ):
        refused = w.req("POST", f"/api/documents/{document}/companion", "a-admin", json=bad)
        assert (refused.status_code, refused.json()["detail"]) == (422, code)

    response = w.req(
        "POST",
        f"/api/documents/{document}/companion",
        "a-admin",
        json=companion_file(w, content, "Loan Values (final).json"),
    )
    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["kind"] == "companion"
    # The link is for the inbox of the document's own batch, under an opaque file ID.
    assert f"/inbox/{ready_doc['batch_id']}/{registered['id']}" in registered["upload"]["url"]
    assert "Loan" not in registered["upload"]["url"]
    assert registered["upload"]["headers"]["Content-Type"] == "application/json"

    # One at a time.
    second = w.req(
        "POST", f"/api/documents/{document}/companion", "a-admin", json=companion_file(w, content)
    )
    assert (second.status_code, second.json()["detail"]) == (409, "companion_pending")

    pending = w.req("GET", f"/api/documents/{document}", "a-admin").json()
    assert pending["companion"] is None
    [file] = pending["companion_files"]
    assert (file["status"], file["replacement"], file["current"]) == ("registered", True, False)

    assert upload_scan(w, "a", registered, content) == "clean"

    shown = w.req("GET", f"/api/documents/{document}", "a-user").json()
    assert shown["has_companion"] is True
    assert shown["companion"] == {
        "version": 1,
        "application_ref": "APP-TEST-004",
        "loan_number": None,
        "cosigner_name": None,
        "package_created_on": None,
        "package_completed_on": None,
        "loan_term_months": 360,
        "loan_maturity_date": None,
        "loan_interest_rate": 6.125,
        "signers": [{"name": "Jane Smith", "email": None, "phone": None, "location": None}],
        "labels": [{"key": "Branch", "value": "Atlanta"}],
    }
    [file] = shown["companion_files"]
    assert (file["status"], file["current"]) == ("clean", True)
    assert file["applied_at"] is not None
    assert shown["events"][-1]["step"] == "companion"

    # Two audit rows: the request (the user, their address) and the change (both files).
    requested, applied = audit(w, document)[len(before) :]
    assert requested[0] == "companion_replace_requested"
    assert requested[3] == {"before": None, "after": {"file_id": registered["id"]}}
    assert requested[4] == "198.51.100.7"
    assert applied[0] == "companion_attached"
    assert applied[3]["after"]["file_id"] == registered["id"]
    for row in (requested, applied):
        assert row[1] and row[2] == "tenant-admin"
        text = json.dumps(row[3])
        # No file name and no value from the file, in rows a master admin can read.
        assert "Loan Values" not in text and "APP-TEST-004" not in text and "Atlanta" not in text

    # The batch's own view does not list it.
    batch = w.req("GET", f"/api/batches/{ready_doc['batch_id']}", "a-admin2").json()
    assert registered["id"] not in [f["id"] for f in batch["files"]]


def test_no_master_view_shows_a_companion_value(w: World, ready_doc: dict[str, Any]) -> None:
    """Owner decision 6: extracted and companion values are the tenant's. A master admin sees
    counts, codes and IDs."""
    document = ready_doc["id"]
    if w.req("GET", f"/api/documents/{document}", "a-admin").json()["companion"] is None:
        content = body_for(w, **COMPANION)
        registered = w.req(
            "POST",
            f"/api/documents/{document}/companion",
            "a-admin",
            json=companion_file(w, content),
        ).json()
        upload_scan(w, "a", registered, content)
    pages = [
        w.req("GET", "/api/admin/ingestion", "m-admin"),
        w.req("GET", f"/api/admin/ingestion/{document}", "m-admin"),
        w.req("GET", "/api/admin/ingestion?format=csv", "m-admin"),
        w.req("GET", "/api/admin/audit?category=document", "m-admin"),
        w.req("GET", "/api/admin/audit?category=document&format=csv", "m-admin"),
    ]
    for page in pages:
        assert page.status_code == 200, page.text
        for value in ("APP-TEST-004", "Jane Smith", "Atlanta", "6.125", "replacement.json"):
            assert value not in page.text
    # The audit rows themselves are there for them to read.
    assert "companion_attached" in pages[3].text


def test_a_companion_may_not_be_attached_to_a_document_that_never_ran(
    w: World, ready_doc: dict[str, Any]
) -> None:
    document = ready_doc["id"]
    previous = w.db.row("SELECT status FROM assay.documents WHERE id = %s", document)
    assert previous is not None
    w.db.execute("UPDATE assay.documents SET status = 'quarantined' WHERE id = %s", document)
    try:
        response = w.req(
            "POST",
            f"/api/documents/{document}/companion",
            "a-admin",
            json=companion_file(w, body_for(w), f"{uuid.uuid4().hex}.json"),
        )
        assert (response.status_code, response.json()["detail"]) == (409, "companion_not_allowed")
    finally:
        w.db.execute("UPDATE assay.documents SET status = %s WHERE id = %s", previous[0], document)


def test_a_pdf_is_never_an_original_twice(w: World, ready_doc: dict[str, Any]) -> None:
    """The fixture's PDF, for the record: one copy into originals/, whatever ran after."""
    key = keys.original_key(uuid.UUID(ready_doc["id"]))
    copies = [c for c in w.storage.buckets.copies if c[3] == key]
    assert len(copies) == 1
    assert minimal_pdf(text="history")  # the bytes the fixture uploaded


# --- Duration: one definition, everywhere it shows ------------------------------------------------

QUEUED_AT = "(SELECT max(at) FROM assay.document_events WHERE document_id = %s AND step = 'queued')"


def durations(w: World, ready_doc: dict[str, Any]) -> dict[str, Any]:
    """The document's duration in every place it shows: the tenant history's table, panel and
    CSV export, and the master history's table and CSV export (for both master roles)."""
    document, batch = ready_doc["id"], ready_doc["batch_id"]
    tenant = w.db.tenants["a"]
    shown: dict[str, Any] = {}

    listed = w.req("GET", f"/api/documents?batch_id={batch}", "a-user").json()["documents"]
    [row] = [d for d in listed if d["id"] == document]
    shown["tenant table"] = row["duration_seconds"]
    panel = w.req("GET", f"/api/documents/{document}", "a-user").json()
    shown["tenant panel"] = panel["duration_seconds"]
    exported = w.req("GET", f"/api/documents?format=csv&batch_id={batch}", "a-user").text
    [line] = [
        r for r in csv.DictReader(io.StringIO(exported)) if r["file_name"] == panel["file_name"]
    ]
    shown["tenant csv"] = line["duration_seconds"]

    for sub in ("m-admin", "m-user"):
        page = w.req("GET", f"/api/admin/ingestion?tenant_id={tenant}&batch_id={batch}", sub)
        [master] = [d for d in page.json()["documents"] if d["document_id"] == document]
        shown[f"master table ({sub})"] = master["duration_seconds"]
        exported = w.req(
            "GET", f"/api/admin/ingestion?format=csv&tenant_id={tenant}&batch_id={batch}", sub
        ).text
        [line] = [r for r in csv.DictReader(io.StringIO(exported)) if r["document_id"] == document]
        shown[f"master csv ({sub})"] = line["duration_seconds"]
    return shown


def agreed(shown: dict[str, Any]) -> int | None:
    """The one duration every view and export shows; fails, naming them, if they differ."""
    values = {None if value in (None, "") else int(value) for value in shown.values()}
    assert len(values) == 1, f"the views disagree: {shown}"
    assert len(shown) == 7
    return values.pop()


def test_every_view_and_both_csv_exports_show_the_same_duration(
    w: World, ready_doc: dict[str, Any]
) -> None:
    """Owner, 2026-11-25. A document uploaded the day before and processed again showed
    55,492 seconds, the time since its upload. One definition now, for the tenant view, the
    master view and both CSV exports: from the document's latest `queued` event to when it
    finished; from when it was received only if it was never queued."""
    document = ready_doc["id"]
    previous = w.db.row(
        "SELECT status, received_at, completed_at FROM assay.documents WHERE id = %s", document
    )
    assert previous is not None
    try:
        # A first attempt: received 20 minutes before its batch was closed (when it was
        # queued), and finished 90 seconds after that. 90 seconds, not 21 and a half minutes.
        w.db.execute(
            "UPDATE assay.documents SET status = 'ready', "
            f"received_at = {QUEUED_AT} - interval '20 minutes', "
            f"completed_at = {QUEUED_AT} + interval '90 seconds' WHERE id = %s",
            document,
            document,
            document,
        )
        assert agreed(durations(w, ready_doc)) == 90

        settle(w, document)
        started = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
        assert started.status_code == 202, started.text
        # Under way: no duration yet, anywhere.
        assert agreed(durations(w, ready_doc)) is None

        # It finishes 42 seconds after "Process again": 42, however long ago the upload was.
        w.db.execute(
            "UPDATE assay.documents SET status = 'ready', received_at = now() - interval "
            f"'15 hours', completed_at = {QUEUED_AT} + interval '42 seconds' WHERE id = %s",
            document,
            document,
        )
        assert agreed(durations(w, ready_doc)) == 42

        # Never queued (held at its scan, say): timed from when it was received.
        w.db.execute(
            "UPDATE assay.document_events SET step = 'queued-hidden' "
            "WHERE document_id = %s AND step = 'queued'",
            document,
        )
        try:
            w.db.execute(
                "UPDATE assay.documents SET completed_at = received_at + interval '7 seconds' "
                "WHERE id = %s",
                document,
            )
            assert agreed(durations(w, ready_doc)) == 7
        finally:
            w.db.execute(
                "UPDATE assay.document_events SET step = 'queued' "
                "WHERE document_id = %s AND step = 'queued-hidden'",
                document,
            )

        # Sorting by duration works in both views (it uses the same expression).
        for order in ("duration", "-duration"):
            batch = ready_doc["batch_id"]
            tenant_page = w.req("GET", f"/api/documents?batch_id={batch}&sort={order}", "a-user")
            assert tenant_page.status_code == 200, tenant_page.text
            master_page = w.req("GET", f"/api/admin/ingestion?sort={order}", "m-user")
            assert master_page.status_code == 200, master_page.text
    finally:
        w.db.execute(
            "UPDATE assay.documents SET status = %s, received_at = %s, completed_at = %s "
            "WHERE id = %s",
            *previous,
            document,
        )


def test_both_views_build_their_duration_from_one_definition() -> None:
    """The two SQL expressions differ only in where they read the events from."""
    from assay_api import ingestion_read

    tenant, master = ingestion_read._STARTED, ingestion_read._MSTARTED
    assert tenant == ingestion_read._started(
        events="assay.document_events",
        same_document="e.tenant_id = d.tenant_id AND e.document_id = d.id",
        received="d.received_at",
    )
    assert master == ingestion_read._started(
        events="platform.ingestion_events_v",
        same_document="e.document_id = v.document_id",
        received="v.received_at",
    )
    for expression in (tenant, master):
        assert "max(e.at)" in expression and "e.step = 'queued'" in expression
    # Every use of a duration goes through it: the columns, and both views' sort orders.
    assert tenant in ingestion_read.TENANT_SELECT and master in ingestion_read.MASTER_SELECT
    assert tenant in ingestion_read.TENANT_SORTS["duration"]
    assert master in ingestion_read.MASTER_SORTS["duration"]


# --- Last in this module: it starts a new attempt of the shared document ------------------------


def test_process_again_is_accepted_straight_after_a_companion_replacement(
    w: World, ready_doc: dict[str, Any]
) -> None:
    """Owner, 2026-11-27. A replacement that has taken effect is complete: nothing is running,
    and "Process again" is accepted at once. (It was refused for five minutes, because the
    replacement's event counted as the current attempt's activity.)"""
    document = ready_doc["id"]
    settle(w, document)
    attempt = w.req("GET", f"/api/documents/{document}", "a-admin").json()["attempt"]
    content = body_for(w, ApplicationId="APP-AT-ONCE", LoanTerm=84)
    registered = w.req(
        "POST", f"/api/documents/{document}/companion", "a-admin", json=companion_file(w, content)
    )
    assert registered.status_code == 201, registered.text
    assert upload_scan(w, "a", registered.json(), content) == "clean"
    shown = w.req("GET", f"/api/documents/{document}", "a-admin").json()
    assert shown["companion"]["application_ref"] == "APP-AT-ONCE"
    # The replacement's event is there for the tenant to see, a moment old.
    assert shown["events"][-1]["step"] == "companion"

    response = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
    assert response.status_code == 202, response.text
    assert response.json() == {"document_id": document, "attempt": attempt + 1}
    # The new attempt is under way, and is held as any attempt is.
    again = w.req("POST", f"/api/documents/{document}/reprocess", "a-admin")
    assert (again.status_code, again.json()["detail"]) == (409, "attempt_running")
