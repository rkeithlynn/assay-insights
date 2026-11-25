"""Which of the worker task's progress marks the history views show."""

from __future__ import annotations

from dataclasses import dataclass

from assay_schemas.pipeline import PROGRESS_MARKS, RUNNING, STEP_ERROR, WORKER_STEP, visible_events
from assay_workers import progress


@dataclass(frozen=True)
class Event:
    step: str
    status: str


def shown(*events: tuple[str, str]) -> list[tuple[str, str]]:
    return [(e.step, e.status) for e in visible_events([Event(*e) for e in events])]


def test_a_finished_step_shows_its_result_only() -> None:
    assert shown(
        ("scan", "clean"),
        ("start", "started"),
        ("fingerprint", "continue"),
        ("worker", "running"),
        ("inspect", "running"),
        ("inspect", "continue"),
    ) == [
        ("scan", "clean"),
        ("start", "started"),
        ("fingerprint", "continue"),
        ("inspect", "continue"),
    ]


def test_a_step_under_way_shows_as_running() -> None:
    assert shown(("fingerprint", "continue"), ("worker", "running")) == [
        ("fingerprint", "continue"),
        ("worker", "running"),
    ]
    assert shown(("fingerprint", "continue"), ("worker", "running"), ("inspect", "running")) == [
        ("fingerprint", "continue"),
        ("inspect", "running"),
    ]


def test_a_failed_step_keeps_its_error_and_loses_its_running_mark() -> None:
    assert shown(
        ("worker", "running"),
        ("inspect", "running"),
        ("inspect", "error"),
        ("failed", "failed"),
    ) == [("inspect", "error"), ("failed", "failed")]
    # Killed mid-step: no error row, so the open mark is all there is.
    assert shown(("worker", "running"), ("inspect", "running"), ("failed", "failed")) == [
        ("inspect", "running"),
        ("failed", "failed"),
    ]


def test_a_second_task_for_the_same_document_shows_the_latest_of_each_step() -> None:
    assert shown(
        ("worker", "running"),
        ("worker", "running"),
        ("inspect", "running"),
        ("inspect", "continue"),
    ) == [("inspect", "continue")]


def test_results_that_are_not_marks_are_never_dropped() -> None:
    events = [("scan", "clean"), ("viewed", "pdf_url"), ("viewed", "pdf_url"), ("failed", "failed")]
    assert shown(*events) == events


def test_the_worker_and_the_api_use_the_same_marks() -> None:
    assert (progress.WORKER, progress.RUNNING, progress.ERROR) == (WORKER_STEP, RUNNING, STEP_ERROR)
    assert progress.MARKS == PROGRESS_MARKS == ("running", "error")
