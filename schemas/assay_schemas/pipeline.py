"""Messages between the parts of the ingestion pipeline (BUILD_SPEC.md section 8).

A pipeline job carries IDs only, never a file name or any document content. Its tenant ID is
one of the two sources section 6 (rule 4) allows; the other is a verified token.
"""

from __future__ import annotations

import base64
import uuid

from pydantic import BaseModel, ConfigDict


def checksum_b64(sha256_hex: str) -> str:
    """S3's form of a SHA-256 (`x-amz-checksum-sha256`): base64 of the digest."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


class IngestJob(BaseModel):
    """One eligible PDF, sent to the ingest queue; the starter turns it into one execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    document_id: uuid.UUID

    def execution_name(self) -> str:
        """One execution per document: a second start of the same document is a no-op."""
        return str(self.document_id)
