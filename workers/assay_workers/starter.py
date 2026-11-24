"""Step 3: one Step Functions execution per eligible PDF (BUILD_SPEC.md section 8).

Reads the ingest queue (an SQS event source, with partial batch responses). For each job:

- The reserved isolation-test document ID is refused: nothing is read, written or started.
- In the job's tenant only: the document must be `processing` (eligible) with no execution
  started yet. Anything else is dropped, logged.
- A tenant that is not `active` (suspended) gets no new execution: the job is dropped and the
  document waits, still eligible. The sweeper queues it again once the tenant is active
  (owner decision 19, build step 6). Executions already running finish.
- The execution is named after the document, so a job delivered twice starts it once; a
  `start` event records it.

A job that fails (the database, Step Functions) is returned as a batch item failure, retried
by SQS and, after five receives, kept in the dead-letter queue, whose depth raises an alarm.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from pydantic import ValidationError
from sqlalchemy import Engine, text

from assay_db import batches
from assay_schemas.pipeline import IngestJob
from assay_tss import keys
from assay_workers.runtime import Settings, tenant_tx

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions import SFNClient

log = logging.getLogger()
log.setLevel(logging.INFO)


class Executions(Protocol):
    def start(self, job: IngestJob) -> str: ...


class StepFunctionsExecutions:
    def __init__(self, state_machine_arn: str, client: SFNClient) -> None:
        self._arn = state_machine_arn
        self._client = client

    def start(self, job: IngestJob) -> str:
        try:
            started = self._client.start_execution(
                stateMachineArn=self._arn,
                name=job.execution_name(),
                input=job.model_dump_json(),
            )
            return str(started["executionArn"])
        except self._client.exceptions.ExecutionAlreadyExists:
            return "already_started"


@dataclass(frozen=True)
class Deps:
    engine: Engine
    executions: Executions


def start_one(job: IngestJob, deps: Deps) -> str:
    fields = {"tenant_id": str(job.tenant_id), "document_id": str(job.document_id)}
    if keys.is_reserved(job.document_id):
        log.warning("the reserved isolation-test document ID is never processed", extra=fields)
        return "refused_reserved"
    with tenant_tx(deps.engine, job.tenant_id) as conn:
        row = conn.execute(
            text(
                "SELECT d.status, EXISTS (SELECT 1 FROM assay.document_events e "
                "WHERE e.document_id = d.id AND e.step = 'start') AS started "
                "FROM assay.documents d WHERE d.id = :id FOR UPDATE"
            ),
            {"id": job.document_id},
        ).one_or_none()
        if row is None or row.status != "processing":
            log.warning("job for a document that is not eligible; dropped", extra=fields)
            return "not_eligible"
        if row.started:
            log.info("execution already started; dropped", extra=fields)
            return "already_started"
        if not batches.tenant_is_active(conn, job.tenant_id):
            log.info("tenant not active: held until it is active again", extra=fields)
            return "held"
        execution = deps.executions.start(job)
        batches.document_event(
            conn,
            tenant_id=job.tenant_id,
            document_id=job.document_id,
            step="start",
            status="started",
        )
    log.info("execution started", extra=fields | {"execution": execution})
    return "started"


def handle_record(record: SQSRecord, deps: Deps) -> str:
    try:
        job = IngestJob.model_validate(json.loads(record.body))
    except ValueError, ValidationError:
        # Never retried: it could never succeed. Logged at ERROR, which raises an alarm.
        log.error("malformed ingest job; dropped", extra={"message_id": record.message_id})
        return "malformed"
    return start_one(job, deps)


_deps: Deps | None = None


def _state() -> Deps:
    global _deps
    if _deps is None:
        import boto3

        from assay_workers.runtime import AWS_CONFIG, app_tenant_engine

        settings = Settings.from_env()
        if not settings.state_machine_arn:
            raise RuntimeError("STATE_MACHINE_ARN is not set")
        client: SFNClient = boto3.client(
            "stepfunctions", region_name=settings.region, config=AWS_CONFIG
        )
        _deps = Deps(
            engine=app_tenant_engine(),
            executions=StepFunctionsExecutions(settings.state_machine_arn, client),
        )
    return _deps


def handle(event: dict[str, Any], deps: Deps) -> dict[str, Any]:
    processor = BatchProcessor(event_type=EventType.SQS)
    response = process_partial_response(
        event=event,
        record_handler=lambda record: handle_record(record, deps),
        processor=processor,
        context=None,
    )
    return dict(response)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    return handle(event, _state())
