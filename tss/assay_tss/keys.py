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
    derived/text/{doc_id}.json                               per-page text
    derived/fields/{doc_id}.json                             extracted fields
    chat/{conversation_id}/{message_id}.json                 question, passages, answer
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

_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")


class InvalidKeyPart(ValueError):
    """Something that may not go into an object key: not an ID, or not a plain extension."""


def _id(value: uuid.UUID) -> str:
    if not isinstance(value, uuid.UUID):
        raise InvalidKeyPart(f"expected a UUID, got {type(value).__name__}")
    return str(value)


def inbox_key(batch_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """An upload, under an opaque ID. Its file name lives only in the database."""
    return f"{INBOX_PREFIX}{_id(batch_id)}/{_id(file_id)}"


def inbox_batch_prefix(batch_id: uuid.UUID) -> str:
    return f"{INBOX_PREFIX}{_id(batch_id)}/"


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


def text_key(doc_id: uuid.UUID) -> str:
    return f"{DERIVED_PREFIX}text/{_id(doc_id)}.json"


def fields_key(doc_id: uuid.UUID) -> str:
    return f"{DERIVED_PREFIX}fields/{_id(doc_id)}.json"


def chat_key(conversation_id: uuid.UUID, message_id: uuid.UUID) -> str:
    return f"{CHAT_PREFIX}{_id(conversation_id)}/{_id(message_id)}.json"
