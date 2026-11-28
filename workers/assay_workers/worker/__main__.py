"""The worker task's entry point: `python -m assay_workers.worker`.

Step Functions starts one task per document's attempt and passes `DOCUMENT_ID`, `ATTEMPT` and
`WORKER_TOKEN`. `WORKER_COMMAND=probe` runs the isolation probe instead
(`scripts/run_isolation.py`).

Exit status: 0 when the attempt's steps ran, whatever the document's outcome (the database
holds it); `EXIT_REFUSED` when no live grant matched; 1 for any other failure, which the
state machine turns into the document's `{step}_failed`.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import uuid
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from assay_workers.worker import logs
from assay_workers.worker.context import TextSettings
from assay_workers.worker.db import GrantRefused, Job

log = logging.getLogger("assay.worker")

EXIT_FAILED = 1
EXIT_REFUSED = 3
PR_SET_DUMPABLE = 4
DEFAULT_WORK_DIR = "/work"


def job_from_env(env: Mapping[str, str]) -> Job:
    return Job(
        document_id=uuid.UUID(env["DOCUMENT_ID"]),
        attempt=int(env["ATTEMPT"]),
        token=env["WORKER_TOKEN"],
    )


def text_settings(env: Mapping[str, str]) -> TextSettings:
    """Step 5's settings, from the task definition (stack config). There is no default: a
    task without them does not run."""
    return TextSettings(
        threshold=float(env["TEXT_QUALITY_THRESHOLD"]),
        max_pages=int(env["TEXT_MAX_PAGES"]),
        price_per_page=Decimal(env["TEXTRACT_PRICE_PER_PAGE"]),
    )


def harden() -> None:
    """Not dumpable: no core dump, and no other user's process may read this one's memory
    or environment, whatever else changes. (The sandbox child is another user already.)"""
    if sys.platform.startswith("linux"):
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)


def run_job(env: Mapping[str, str]) -> int:
    from assay_tss.client import TssClient
    from assay_workers.runtime import S3Objects, TenantStorage
    from assay_workers.sandbox import runner
    from assay_workers.worker import db
    from assay_workers.worker.ocr import TextractOcr
    from assay_workers.worker.run import Deps, Refused, error_name, run

    try:
        job = job_from_env(env)
    except KeyError, ValueError:
        log.error("the task was started without a document, attempt and token")
        return EXIT_FAILED
    fields = {"document_id": str(job.document_id), "attempt": job.attempt}
    try:
        settings = text_settings(env)
    except KeyError, ValueError, ArithmeticError:
        log.error("the task was started without usable settings for the text step", extra=fields)
        return EXIT_FAILED
    tss = TssClient(env["TSS_ENDPOINT_URL"], region=env["AWS_REGION"])

    def storage() -> TenantStorage:
        ctx = tss.resolve_worker(job.document_id, job.attempt, job.token)
        return TenantStorage(context=ctx, objects=S3Objects(ctx))

    engine = db.aurora_engine(job)
    try:
        outcome = run(
            job,
            Deps(
                engine=engine,
                storage=storage,
                inspect=runner.inspect,
                work_dir=Path(env.get("WORK_DIR", DEFAULT_WORK_DIR)),
                # `runner.read_text` and `runner.render`: each call is one sandbox child.
                pages=runner,
                ocr=TextractOcr.for_region(env["AWS_REGION"]),
                text=settings,
            ),
        )
    except GrantRefused, Refused:
        log.error("no live grant for this task; nothing done", extra=fields)
        return EXIT_REFUSED
    except Exception as exc:
        if isinstance(exc.__cause__, GrantRefused) or isinstance(exc.__context__, GrantRefused):
            log.error("no live grant for this task; nothing done", extra=fields)
            return EXIT_REFUSED
        # A name only (the type, or a service's error code): a message can quote what a
        # library was handed.
        log.error("worker failed", extra=fields | {"error_type": error_name(exc)})
        return EXIT_FAILED
    finally:
        engine.dispose()
    log.info("worker finished", extra=fields | {"outcome": outcome})
    return 0


def run_probe(env: Mapping[str, str]) -> int:
    """The isolation probe (`WORKER_COMMAND=probe`). Its one Textract call is billed like any
    other, so it is recorded at the price the text step uses: the task definition's, from
    stack config. A probe without that price does not run."""
    from assay_workers.worker import probe

    try:
        settings = text_settings(env)
    except KeyError, ValueError, ArithmeticError:
        log.error("the probe was started without usable settings for the text step")
        return EXIT_FAILED
    return probe.main(env, price_per_page=settings.price_per_page)


def main(env: Mapping[str, str] | None = None) -> int:
    logs.configure()
    harden()
    values = dict(os.environ if env is None else env)
    # The token stays in this process's memory only; nothing started later inherits it.
    os.environ.pop("WORKER_TOKEN", None)
    if values.get("WORKER_COMMAND") == "probe":
        return run_probe(values)
    return run_job(values)


if __name__ == "__main__":
    sys.exit(main())
