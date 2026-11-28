"""Messages between the parts of the ingestion pipeline (BUILD_SPEC.md section 8).

A pipeline job carries IDs only, never a file name or any document content. Its tenant ID is
one of the two sources section 6 (rule 4) allows; the other is a verified token.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

# How the worker task's progress is recorded in `document_events` (assay_workers.progress
# says when each is written). `RUNNING` and `STEP_ERROR` are progress marks, not results.
WORKER_STEP = "worker"
RUNNING = "running"
STEP_ERROR = "error"
PROGRESS_MARKS = (RUNNING, STEP_ERROR)


class StepEvent(Protocol):
    @property
    def step(self) -> str: ...

    @property
    def status(self) -> str: ...


def visible_events[E: StepEvent](events: Sequence[E]) -> list[E]:
    """The events a history view shows, from a document's events in time order.

    A step's `running` mark is left out once the step has a later event (its result), so a
    finished step shows one line, and one that is under way shows "running". The task's own
    `worker` mark says only that the task started: it is left out once anything follows it.
    A step's `error` mark stays: it is the last word on that step.
    """
    shown: list[E] = []
    for index, event in enumerate(events):
        later = events[index + 1 :]
        if event.status == RUNNING:
            if event.step == WORKER_STEP and later:
                continue
            if any(other.step == event.step for other in later):
                continue
        shown.append(event)
    return shown


def checksum_b64(sha256_hex: str) -> str:
    """S3's form of a SHA-256 (`x-amz-checksum-sha256`): base64 of the digest."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


class IngestJob(BaseModel):
    """One eligible PDF, sent to the ingest queue; the starter turns it into one execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    document_id: uuid.UUID


def execution_name(document_id: uuid.UUID, attempt: int) -> str:
    """One execution per attempt of a document: a second start of the same one is a no-op.

    The first attempt keeps the document's ID as its name. Step Functions does not give a
    Standard execution's name out again for 90 days, so "Process again" needs a new one.
    """
    return str(document_id) if attempt == 1 else f"{document_id}-{attempt}"
