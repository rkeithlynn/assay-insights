"""How the worker task's progress is recorded in `document_events`, shared by the task
(`assay_workers.worker`) and the state machine's Lambda steps (`assay_workers.steps`).

The task runs several pipeline steps in one go, so Step Functions sees only whether the task
ended well. What it was doing is in the database:

- `(WORKER, RUNNING)`: the task started and bound its database session. Its absence after a
  failed task means the task never ran (no capacity, for example), and the state machine
  waits and tries again instead of failing the document.
- `(step, RUNNING)` before each step, then the step's result. If the task dies, the step
  whose last event is still `RUNNING` (or `ERROR`) is the one that failed, and the document's
  code is `{step}_failed`.
- `(step, ERROR)`, with the exception's type in `detail.error_type`: written by the task,
  when it still can, just before it exits with a failure.

`RUNNING` and `ERROR` rows are progress marks, not results: the history views leave out a
`RUNNING` row once its step has a later row (`assay_schemas.pipeline.visible_events`).
"""

from __future__ import annotations

from assay_schemas.pipeline import PROGRESS_MARKS, RUNNING, STEP_ERROR, WORKER_STEP

WORKER = WORKER_STEP
# The task's steps, in the order it runs them. Part 1 has one; steps 5 to 10 follow.
WORKER_STEPS: tuple[str, ...] = ("inspect",)
ERROR = STEP_ERROR
MARKS = PROGRESS_MARKS

__all__ = ["ERROR", "MARKS", "RUNNING", "WORKER", "WORKER_STEPS"]
