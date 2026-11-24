"""Object keys inside a tenant bucket (BUILD_SPEC.md section 6).

This module is the only place that builds an S3 key. Callers reach it through a
`StorageContext`, never by formatting a string themselves.

No key contains a file name. Every part of a key is an ID or fixed text, plus a short
extension for attachments; an upload's own name is kept only in the database. A role that
can list a bucket's keys (the provisioning role can) therefore learns nothing about what
a tenant uploaded.

    inbox/{batch_id}/{file_id}                               uploads, before scanning
    originals/{doc_id}.pdf                                   clean PDFs, under Object Lock
    originals/{doc_id}.companion.json                        the companion file, if any
    originals/{doc_id}/attachments/{attachment_id}.{ext}     stipulations, under Object Lock
    derived/faces/{face_id}.png                              face crops
    quarantine/{doc_id}.pdf                                  infected files
    quarantine/{file_id}.companion.json                      infected companion files
    derived/text/{doc_id}.json                               per-page text
    derived/fields/{doc_id}.json                             extracted fields
    chat/{conversation_id}/{message_id}.json                 question, passages, answer

A PDF's document ID is its upload's file ID, so `inbox/{batch_id}/{doc_id}` and
`quarantine/{doc_id}.pdf` name the same document. A companion file is paired with its PDF
only when the batch closes, so an infected one is quarantined under its own file ID.

The reserved document ID
------------------------
`RESERVED_DOCUMENT_ID` belongs to the isolation tests (tests/isolation). Each test tenant's
bucket keeps two fixture objects under it, written by those tests and kept by Object Lock:
`derived/text/{id}.json` and `quarantine/{id}.pdf`. It has no `documents` row and must never
get one. So every key builder below refuses it, the only keys ever built from it are the
two `isolation_fixture_*` keys, and the API and the pipeline refuse to create or process a
document with it (`check_not_reserved`). It is never in `inbox/`.
"""

from __future__ import annotations

import re
import uuid

INBOX_PREFIX = "inbox/"
ORIGINALS_PREFIX = "originals/"
DERIVED_PREFIX = "derived/"
QUARANTINE_PREFIX = "quarantine/"
CHAT_PREFIX = "chat/"
PREFIXES = (INBOX_PREFIX, ORIGINALS_PREFIX, DERIVED_PREFIX, QUARANTINE_PREFIX, CHAT_PREFIX)

# GuardDuty writes this one object at the bucket root when a malware protection plan is
# created. Only the scan role may write it.
SCAN_VALIDATION_KEY = "malware-protection-resource-validation-object"

# The isolation tests' fixture document. Defined here and nowhere else.
RESERVED_DOCUMENT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000f1")

_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_INBOX_KEY = re.compile(rf"^inbox/({_UUID})/({_UUID})$")


class InvalidKeyPart(ValueError):
    """Something that may not go into an object key: not an ID, or not a plain extension."""


class ReservedId(InvalidKeyPart):
    """The isolation tests' reserved document ID. No document may have it."""


def is_reserved(value: uuid.UUID) -> bool:
    return value == RESERVED_DOCUMENT_ID


def check_not_reserved(*values: uuid.UUID) -> None:
    """Raise ReservedId if any of these IDs is the reserved one."""
    for value in values:
        if is_reserved(value):
            raise ReservedId("the reserved isolation-test document ID is never used for a document")


def _id(value: uuid.UUID) -> str:
    if not isinstance(value, uuid.UUID):
        raise InvalidKeyPart(f"expected a UUID, got {type(value).__name__}")
    check_not_reserved(value)
    return str(value)


def inbox_key(batch_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """An upload, under an opaque ID. Its file name lives only in the database."""
    return f"{INBOX_PREFIX}{_id(batch_id)}/{_id(file_id)}"


def inbox_batch_prefix(batch_id: uuid.UUID) -> str:
    return f"{INBOX_PREFIX}{_id(batch_id)}/"


def parse_inbox_key(key: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(batch_id, file_id) of an inbox key, or None if the key is not one.

    Reading a key from a scan event is not building one: this never produces a key, and a
    key with the reserved ID is reported as not an inbox key.
    """
    match = _INBOX_KEY.match(key)
    if match is None:
        return None
    batch_id, file_id = uuid.UUID(match.group(1)), uuid.UUID(match.group(2))
    if is_reserved(batch_id) or is_reserved(file_id):
        return None
    return batch_id, file_id


def original_key(doc_id: uuid.UUID) -> str:
    return f"{ORIGINALS_PREFIX}{_id(doc_id)}.pdf"


def companion_key(doc_id: uuid.UUID) -> str:
    return f"{ORIGINALS_PREFIX}{_id(doc_id)}.companion.json"


def attachment_key(doc_id: uuid.UUID, attachment_id: uuid.UUID, extension: str) -> str:
    if not _EXTENSION.match(extension):
        raise InvalidKeyPart("extension must be 1 to 8 lower-case letters or digits")
    return f"{ORIGINALS_PREFIX}{_id(doc_id)}/attachments/{_id(attachment_id)}.{extension}"


def face_key(face_id: uuid.UUID) -> str:
    return f"{DERIVED_PREFIX}faces/{_id(face_id)}.png"


def quarantine_key(doc_id: uuid.UUID) -> str:
    return f"{QUARANTINE_PREFIX}{_id(doc_id)}.pdf"


def quarantine_companion_key(file_id: uuid.UUID) -> str:
    return f"{QUARANTINE_PREFIX}{_id(file_id)}.companion.json"


def text_key(doc_id: uuid.UUID) -> str:
    return f"{DERIVED_PREFIX}text/{_id(doc_id)}.json"


def fields_key(doc_id: uuid.UUID) -> str:
    return f"{DERIVED_PREFIX}fields/{_id(doc_id)}.json"


def chat_key(conversation_id: uuid.UUID, message_id: uuid.UUID) -> str:
    return f"{CHAT_PREFIX}{_id(conversation_id)}/{_id(message_id)}.json"


# --- The isolation tests' two fixture objects: the only keys built from the reserved ID ----


def isolation_fixture_text_key() -> str:
    """`derived/text/{reserved}.json`: read back by the tenant, refused to everyone else."""
    return f"{DERIVED_PREFIX}text/{RESERVED_DOCUMENT_ID}.json"


def isolation_fixture_quarantine_key() -> str:
    """`quarantine/{reserved}.pdf`: shows that only pipeline sessions read quarantine/."""
    return f"{QUARANTINE_PREFIX}{RESERVED_DOCUMENT_ID}.pdf"


ISOLATION_FIXTURE_KEYS = (isolation_fixture_text_key(), isolation_fixture_quarantine_key())
