"""Stand-ins for the text step's tests (build step 6, second half, stage 2).

- `InProcessPages`: the sandbox's two page jobs, run in this process. The child cannot run on
  the development machine (it needs Linux and another user), but everything the parent does
  with what a child sends is real here: the job writes its records to a buffer, and the
  buffer is fed through `runner`'s own channel, models and checks.
  `tests/unit/test_worker_image.py` runs the real child in the task's image.
- `FakeOcr`: Textract. It never opens a picture either; it answers from a script.
- `test_files`: `scripts/make_test_files.py`, the same synthetic PDFs the owner uploads.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

from assay_workers.sandbox import jobs, runner
from assay_workers.sandbox.confine import Limits
from assay_workers.sandbox.runner import Channel, Completed, PageImage, PageRecord
from assay_workers.worker.context import TextSettings
from assay_workers.worker.ocr import OcrFailed, OcrPage, PageRefused

ROOT = Path(__file__).resolve().parents[2]
PRICE = Decimal("0.0015")
SETTINGS = TextSettings(threshold=0.90, max_pages=1000, price_per_page=PRICE)


def test_files() -> ModuleType:
    name = "make_test_files"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def in_process(
    command: Callable[[int], Sequence[str]], limits: Limits, channel: Channel
) -> Completed:
    """A `runner.ChannelSpawn` that runs the job here and feeds its output through the
    parent's channel, in pieces, as a pipe would deliver it."""
    argv = list(command(3))
    job, source, arguments = argv[5], Path(argv[6]), argv[8:]
    assert argv[7] == "3"
    sent = io.BytesIO()
    result = jobs.PAGE_JOBS[job](source, sent, *arguments)
    data = sent.getvalue()
    for start in range(0, len(data), 4096):
        channel.feed(data[start : start + 4096])
    channel.finish()
    return Completed(returncode=0, stdout=json.dumps(result).encode())


@dataclass
class InProcessPages:
    """`context.Pages`, with a record of which pages each child was asked for."""

    text_calls: list[tuple[int, int]] = field(default_factory=list)
    render_calls: list[list[int]] = field(default_factory=list)
    spawn: runner.ChannelSpawn = in_process

    def read_text(
        self, source: Path, first: int, last: int, handle: Callable[[PageRecord], None]
    ) -> None:
        self.text_calls.append((first, last))
        runner.read_text(source, first, last, handle, spawn=self.spawn)

    def render(
        self, source: Path, pages: Sequence[int], handle: Callable[[PageImage], None]
    ) -> None:
        self.render_calls.append(list(pages))
        runner.render(source, pages, handle, spawn=self.spawn)


def ocr_page(text: str = "READ BY TEXTRACT", confidence: float = 0.98) -> OcrPage:
    words = tuple(
        (1000 + 900 * index, 1000, 800, 200, word, confidence)
        for index, word in enumerate(text.split())
    )
    return OcrPage(
        text=text,
        words=words,
        confidence=confidence if words else None,
        model_version="1.0",
        request_id="request-0001",
    )


@dataclass
class FakeOcr:
    """Answers each picture from `answers` (by the order pictures arrive), or with a default
    page. An answer may be an exception to raise. Every picture it is given is kept."""

    answers: list[Any] = field(default_factory=list)
    pictures: list[bytes] = field(default_factory=list)
    default: Callable[[], OcrPage] = ocr_page

    def read(self, image: bytes) -> OcrPage:
        self.pictures.append(image)
        answer = self.answers.pop(0) if self.answers else self.default()
        if isinstance(answer, (OcrFailed, PageRefused)):
            raise answer
        assert isinstance(answer, OcrPage)
        return answer


class NoOcr:
    """For a document whose every page has a good text layer: Textract must not be called."""

    def read(self, image: bytes) -> OcrPage:
        raise AssertionError("no page of this document goes to Textract")
