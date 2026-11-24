"""Object keys, resource names and the TSS's types (BUILD_SPEC.md section 6)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from assay_tss import keys, naming
from assay_tss.models import (
    CREDENTIAL_SECONDS,
    Credentials,
    ResolveRequest,
    StorageAccess,
    StorageContext,
    check_source_identity,
    source_identity,
)

BATCH = uuid.UUID("11111111-1111-4111-8111-111111111111")
DOC = uuid.UUID("22222222-2222-4222-8222-222222222222")
OTHER = uuid.UUID("33333333-3333-4333-8333-333333333333")
FILE = uuid.UUID("44444444-4444-4444-8444-444444444444")
TENANT = uuid.UUID("7f3a91c2-0000-4000-8000-000000000001")


def context(access: StorageAccess = StorageAccess.PIPELINE) -> StorageContext:
    return StorageContext(
        tenant_id=TENANT,
        access=access,
        source_identity="api.job-1",
        region="us-east-1",
        bucket="assay-dev-t-7f3a91c2",
        kms_key_arn="arn:aws:kms:us-east-1:000000000000:key/k",
        vector_bucket="assay-dev-t-7f3a91c2-vectors",
        vector_index="documents",
        face_collection="assay-dev-t-7f3a91c2",
        credentials=Credentials(
            access_key_id="ASIAEXAMPLE",
            secret_access_key="very-secret-key",  # type: ignore[arg-type]
            session_token="very-secret-token",  # type: ignore[arg-type]
            expiration=dt.datetime(2026, 11, 23, 12, 15, tzinfo=dt.UTC),
        ),
    )


# --- Object layout (section 6) ----------------------------------------------------------------


def test_every_key_follows_the_spec_layout() -> None:
    ctx = context()
    assert ctx.inbox_key(BATCH, FILE) == f"inbox/{BATCH}/{FILE}"
    assert ctx.inbox_batch_prefix(BATCH) == f"inbox/{BATCH}/"
    assert ctx.original_key(DOC) == f"originals/{DOC}.pdf"
    assert ctx.companion_key(DOC) == f"originals/{DOC}.companion.json"
    assert ctx.attachment_key(DOC, OTHER, "jpg") == f"originals/{DOC}/attachments/{OTHER}.jpg"
    assert ctx.face_key(OTHER) == f"derived/faces/{OTHER}.png"
    assert ctx.quarantine_key(DOC) == f"quarantine/{DOC}.pdf"
    assert ctx.quarantine_companion_key(FILE) == f"quarantine/{FILE}.companion.json"
    assert ctx.text_key(DOC) == f"derived/text/{DOC}.json"
    assert ctx.fields_key(DOC) == f"derived/fields/{DOC}.json"
    assert ctx.chat_key(BATCH, OTHER) == f"chat/{BATCH}/{OTHER}.json"


def test_every_key_is_under_a_known_prefix() -> None:
    ctx = context()
    built = [
        ctx.inbox_key(BATCH, FILE),
        ctx.original_key(DOC),
        ctx.companion_key(DOC),
        ctx.attachment_key(DOC, OTHER, "png"),
        ctx.face_key(OTHER),
        ctx.quarantine_key(DOC),
        ctx.text_key(DOC),
        ctx.fields_key(DOC),
        ctx.chat_key(BATCH, OTHER),
    ]
    assert all(key.startswith(keys.PREFIXES) for key in built)
    assert not keys.SCAN_VALIDATION_KEY.startswith(keys.PREFIXES)


@pytest.mark.parametrize(
    "file_name", ["smith-package.pdf", "../../originals/x.pdf", "", str(uuid.uuid4())]
)
def test_no_key_can_hold_a_file_name(file_name: str) -> None:
    """An upload is stored under an opaque ID; its name stays in the database."""
    with pytest.raises(keys.InvalidKeyPart):
        keys.inbox_key(BATCH, file_name)  # type: ignore[arg-type]


def test_keys_are_built_from_ids_and_fixed_text_only() -> None:
    ctx = context()
    ids = {str(BATCH), str(DOC), str(OTHER), str(FILE)}
    built = [
        ctx.inbox_key(BATCH, FILE),
        ctx.original_key(DOC),
        ctx.companion_key(DOC),
        ctx.attachment_key(DOC, OTHER, "jpg"),
        ctx.face_key(OTHER),
        ctx.quarantine_key(DOC),
        ctx.text_key(DOC),
        ctx.fields_key(DOC),
        ctx.chat_key(BATCH, OTHER),
    ]
    fixed = {"inbox", "originals", "derived", "quarantine", "chat", "attachments", "faces"}
    fixed |= {"text", "fields", "pdf", "json", "png", "jpg", "companion"}
    for key in built:
        for part in key.replace(".", "/").split("/"):
            assert part in ids or part in fixed, f"{key}: unexpected part {part!r}"


@pytest.mark.parametrize("extension", ["", "JPG", "tar.gz", "a/b", "toolongext"])
def test_attachment_extension_is_checked(extension: str) -> None:
    with pytest.raises(keys.InvalidKeyPart):
        keys.attachment_key(DOC, OTHER, extension)


def test_ids_must_be_uuids() -> None:
    with pytest.raises(keys.InvalidKeyPart):
        keys.original_key("../../other-tenant")  # type: ignore[arg-type]


# --- The reserved isolation-test document ID ------------------------------------------------

RESERVED = keys.RESERVED_DOCUMENT_ID


@pytest.mark.parametrize(
    "build",
    [
        lambda: keys.inbox_key(BATCH, RESERVED),
        lambda: keys.inbox_key(RESERVED, FILE),
        lambda: keys.inbox_batch_prefix(RESERVED),
        lambda: keys.original_key(RESERVED),
        lambda: keys.companion_key(RESERVED),
        lambda: keys.attachment_key(RESERVED, OTHER, "jpg"),
        lambda: keys.attachment_key(DOC, RESERVED, "jpg"),
        lambda: keys.face_key(RESERVED),
        lambda: keys.quarantine_key(RESERVED),
        lambda: keys.quarantine_companion_key(RESERVED),
        lambda: keys.text_key(RESERVED),
        lambda: keys.fields_key(RESERVED),
        lambda: keys.chat_key(RESERVED, OTHER),
        lambda: context().text_key(RESERVED),
        lambda: context().inbox_key(BATCH, RESERVED),
    ],
)
def test_no_key_is_ever_built_from_the_reserved_id(build: object) -> None:
    """The pipeline cannot write under the isolation fixture's ID, whatever it is asked."""
    with pytest.raises(keys.ReservedId):
        build()  # type: ignore[operator]


def test_the_fixture_keys_are_the_only_keys_with_the_reserved_id() -> None:
    assert keys.isolation_fixture_text_key() == f"derived/text/{RESERVED}.json"
    assert keys.isolation_fixture_quarantine_key() == f"quarantine/{RESERVED}.pdf"
    assert (
        keys.isolation_fixture_text_key(),
        keys.isolation_fixture_quarantine_key(),
    ) == keys.ISOLATION_FIXTURE_KEYS
    # Never in the inbox (owner's condition, build step 4).
    assert not any(k.startswith(keys.INBOX_PREFIX) for k in keys.ISOLATION_FIXTURE_KEYS)


def test_check_not_reserved() -> None:
    keys.check_not_reserved(DOC, FILE)
    with pytest.raises(keys.ReservedId):
        keys.check_not_reserved(DOC, RESERVED)
    assert keys.is_reserved(RESERVED) and not keys.is_reserved(DOC)


def test_quarantined_companion_files_are_keyed_by_their_file_id() -> None:
    assert context().quarantine_companion_key(FILE) == f"quarantine/{FILE}.companion.json"


@pytest.mark.parametrize(
    ("key", "parsed"),
    [
        (f"inbox/{BATCH}/{FILE}", (BATCH, FILE)),
        (f"inbox/{BATCH}/{FILE}/", None),
        (f"inbox/{BATCH}/smith.pdf", None),
        (f"originals/{DOC}.pdf", None),
        (f"inbox/{BATCH}/{FILE}".upper(), None),
        (f"inbox/{BATCH}/{RESERVED}", None),
        (f"inbox/{RESERVED}/{FILE}", None),
        (keys.isolation_fixture_quarantine_key(), None),
        ("", None),
    ],
)
def test_parse_inbox_key(key: str, parsed: tuple[uuid.UUID, uuid.UUID] | None) -> None:
    assert keys.parse_inbox_key(key) == parsed


# --- Names -----------------------------------------------------------------------------------


def test_names_are_built_from_the_tenant_id_never_a_customer_name() -> None:
    assert naming.bucket_name("assay-dev", TENANT) == "assay-dev-t-7f3a91c2"
    assert naming.face_collection_id("assay-dev", TENANT) == "assay-dev-t-7f3a91c2"
    assert naming.vector_bucket_name("assay-dev", TENANT) == "assay-dev-t-7f3a91c2-vectors"
    assert naming.key_alias("assay-dev", TENANT) == "alias/assay-dev-t-7f3a91c2"
    assert naming.tenant_pattern("assay-dev") == "assay-dev-t-*"


@pytest.mark.parametrize("prefix", ["", "Assay", "assay_dev", "assay-dev-", "a*"])
def test_name_prefix_is_checked(prefix: str) -> None:
    with pytest.raises(ValueError, match="name prefix"):
        naming.bucket_name(prefix, TENANT)


# --- Source identity --------------------------------------------------------------------------


def test_source_identity_names_service_and_job() -> None:
    assert source_identity("api", "batch-42") == "api.batch-42"
    assert check_source_identity("worker.doc-7.step-3") == ("worker", "doc-7.step-3")


@pytest.mark.parametrize(
    "value", ["api", "api.", ".job", "API.job", "api job", "api.job/1", "a." + "x" * 64, "a.b"]
)
def test_source_identity_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="source identity"):
        check_source_identity(value)


def test_resolve_request_rejects_extra_fields_and_bad_values() -> None:
    good = {"tenant_id": str(TENANT), "access": "read", "source_identity": "api.job-1"}
    assert ResolveRequest.model_validate(good).access is StorageAccess.READ
    for bad in (
        {**good, "bucket": "someone-elses"},
        {**good, "access": "admin"},
        {**good, "tenant_id": "not-a-uuid"},
        {**good, "source_identity": "nodot"},
    ):
        with pytest.raises(ValidationError):
            ResolveRequest.model_validate(bad)


# --- Credentials ------------------------------------------------------------------------------


def test_credentials_last_15_minutes() -> None:
    assert CREDENTIAL_SECONDS == 900


def test_secrets_never_appear_in_repr_or_dump() -> None:
    ctx = context()
    for text in (repr(ctx), str(ctx), ctx.model_dump_json()):
        assert "very-secret" not in text


def test_wire_format_round_trips_with_the_secrets() -> None:
    ctx = context(StorageAccess.READ)
    wire = ctx.to_wire()
    assert wire["credentials"]["secret_access_key"] == "very-secret-key"
    again = StorageContext.from_wire(wire)
    assert again == ctx
    assert again.credentials.session_token.get_secret_value() == "very-secret-token"


def test_context_knows_when_it_is_about_to_expire() -> None:
    ctx = context()
    expiry = ctx.credentials.expiration
    assert not ctx.expired(now=expiry - dt.timedelta(minutes=5))
    assert ctx.expired(now=expiry - dt.timedelta(seconds=30))
    assert ctx.expired(now=expiry + dt.timedelta(seconds=1))
