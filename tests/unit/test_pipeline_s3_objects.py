"""The pipeline's S3 calls, as sent to S3 (botocore's Stubber checks every parameter)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from botocore.stub import Stubber

from assay_tss.models import Credentials, StorageAccess, StorageContext
from assay_workers import runtime

BUCKET = "assay-test-t-0123abcd"


def context(access: StorageAccess = StorageAccess.PIPELINE) -> StorageContext:
    return StorageContext(
        tenant_id=uuid.UUID("0123abcd-0000-4000-8000-000000000001"),
        access=access,
        source_identity="pipeline:test",
        region="us-east-1",
        bucket=BUCKET,
        kms_key_arn="arn:aws:kms:us-east-1:000000000000:key/k",
        vector_bucket=f"{BUCKET}-vectors",
        vector_index="chunks",
        face_collection=BUCKET,
        credentials=Credentials(
            access_key_id="AKIA0000000000000000",
            secret_access_key="secret",  # type: ignore[arg-type]
            session_token="token",  # type: ignore[arg-type]
            expiration=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15),
        ),
    )


def test_a_copy_takes_no_tags_from_its_scanned_source() -> None:
    """GuardDuty tags every scanned upload. A copy that took the source's tags (S3's default)
    needs tagging permissions no tenant session has, and S3 refused it (2026-11-24)."""
    objects = runtime.S3Objects(context())
    client: Any = objects._s3
    with Stubber(client) as stub:
        stub.add_response(
            "copy_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": "originals/doc.pdf",
                "CopySource": {"Bucket": BUCKET, "Key": "inbox/b/f", "VersionId": "v1"},
                "ContentType": "application/pdf",
                "MetadataDirective": "REPLACE",
                "TaggingDirective": "REPLACE",
                "ChecksumAlgorithm": "SHA256",
            },
        )
        objects.copy(
            source_key="inbox/b/f",
            source_version="v1",
            target_key="originals/doc.pdf",
            content_type="application/pdf",
        )
        stub.assert_no_pending_responses()


def test_only_pipeline_contexts_reach_objects() -> None:
    with pytest.raises(ValueError, match="PIPELINE"):
        runtime.S3Objects(context(StorageAccess.READ))
