"""Textract is the worker task's alone (build step 6, second half, stage 2).

The VPC's Textract endpoint admits one role, the pipeline's worker task, and one action. This
Lambda's role is not that role, and its own IAM policy *allows* the same call the worker
makes, with the same condition (owner, 2026-11-27): so the endpoint's policy is the only
thing left that can refuse it, and the test requires that refusal by name.

The picture sent is drawn here: a blank white rectangle, nothing of any tenant's. If the
endpoint's policy were ever loosened, that is all Textract would be given.

The worker's own side (its one operation works; the others, a tenant's bucket and the
document's storage credentials are refused) is tried by its probe, as the worker task
(`assay_workers.worker.probe`).
"""

from __future__ import annotations

import zlib
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from tests.isolation.conftest import Env

# Fail fast: an endpoint that cannot be reached must not look like a refusal.
CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
WIDTH, HEIGHT = 200, 60


def blank_png() -> bytes:
    """A small all-white greyscale PNG, written with `zlib` alone."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    rows = (b"\x00" + b"\xff" * WIDTH) * HEIGHT
    header = WIDTH.to_bytes(4, "big") + HEIGHT.to_bytes(4, "big") + bytes((8, 0, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_the_textract_endpoint_refuses_every_role_but_the_worker_task(env: Env) -> None:
    textract: Any = boto3.client("textract", region_name=env.region, config=CONFIG)
    with pytest.raises(ClientError) as refused:
        textract.detect_document_text(Document={"Bytes": blank_png()})
    error = refused.value.response.get("Error", {})
    assert error.get("Code") == "AccessDeniedException", error.get("Code")
    # This role's own policy allows the call, so the refusal has to be the endpoint's, and
    # AWS says which policy refused.
    assert "VPC endpoint policy" in str(error.get("Message")), error.get("Message")


def test_the_picture_this_test_sends_is_a_blank_png() -> None:
    picture = blank_png()
    assert picture.startswith(b"\x89PNG\r\n\x1a\n") and len(picture) < 1000
    data = picture[picture.index(b"IDAT") + 4 : picture.index(b"IEND") - 8]
    assert set(zlib.decompress(data)) == {0x00, 0xFF}
