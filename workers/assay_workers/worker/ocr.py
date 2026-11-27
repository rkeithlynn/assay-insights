"""Textract, for the pages whose text layer fails the quality score (step 5).

One operation, `DetectDocumentText`: synchronous, one page a call, the page's picture sent as
bytes. Textract is never pointed at a bucket, so it needs nothing of a tenant's storage or
key. The call is made by the worker task's main process with the task's own role, through the
VPC's Textract endpoint (owner decision 18). The sandbox child that drew the picture has no
network and no credentials.

- **Throttling and server errors are tried again**, with backoff and jitter, for up to five
  minutes a page (owner decision C). After that the step fails with the error's own name
  (`OcrFailed.error_type`), which is all that is ever recorded of it.
- **A page Textract will not take** (`UnsupportedDocumentException`, `BadDocumentException`,
  `DocumentTooLargeException`, `InvalidParameterException`) is that page's problem, not the
  document's: `PageRefused`, and the page is recorded as unread.
- **Anything else** (`AccessDeniedException` among them) fails the step at once.

Nothing here logs a picture, a response or any text from one. What Textract returns is
content chosen by whoever made the PDF: data, never instructions.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

OPERATION = "DetectDocumentText"
SERVICE = "textract"
# One try a call: the backoff below is the only retry, so its limit is the limit.
CLIENT_CONFIG = Config(
    connect_timeout=5, read_timeout=30, retries={"max_attempts": 1, "mode": "standard"}
)
RETRY_SECONDS = 300.0
FIRST_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 20.0
# Too many calls, or Textract's own trouble: worth another try.
RETRIED = frozenset(
    {
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "LimitExceededException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)
# Textract will not read this picture, however often it is asked.
PAGE_REFUSALS = frozenset(
    {
        "UnsupportedDocumentException",
        "BadDocumentException",
        "DocumentTooLargeException",
        "InvalidParameterException",
    }
)
# An error's name as it may be stored: the same form the pipeline's failure handler accepts.
ERROR_NAME = re.compile(r"[A-Za-z0-9_.]{1,64}")
UNNAMED = "UnknownError"
# Positions are ten-thousandths of the page, as the sandbox reports the text layer's.
UNITS = 10_000
MAX_TEXT_BYTES = 256 * 1024
MAX_WORDS = 10_000
MAX_WORD_CHARS = 200
MAX_VERSION_CHARS = 32
MAX_REQUEST_ID_CHARS = 128


class OcrFailed(Exception):
    """The step cannot go on. `error_type` is the service's error code or the exception's
    type: a name, never a message."""

    def __init__(self, error_type: str) -> None:
        self.error_type = error_type if ERROR_NAME.fullmatch(error_type) else UNNAMED
        super().__init__(self.error_type)


class PageRefused(Exception):
    """Textract will not read this one picture. `code` is its error's name."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OcrPage:
    """What Textract read on one page. Text and words are untrusted content."""

    text: str
    # left, top, width, height in ten-thousandths of the page, the word, its confidence
    # (0 to 1).
    words: tuple[tuple[int, int, int, int, str, float], ...]
    # The mean of the words' confidences, 0 to 1. None when it read no word.
    confidence: float | None
    # Textract's own name for the model that read it, and the request's ID.
    model_version: str | None
    request_id: str | None


class Ocr(Protocol):
    def read(self, image: bytes) -> OcrPage: ...


def _unit(value: object) -> int:
    try:
        number = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return 0
    if number != number:  # not a number
        return 0
    return min(max(round(number * UNITS), 0), UNITS)


def _confidence(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    if not 0.0 <= number <= 100.0:
        return None
    return round(number / 100.0, 4)


def parse(response: dict[str, Any]) -> OcrPage:
    """Lines in the order Textract gives them, and each word with its box and confidence."""
    lines: list[str] = []
    words: list[tuple[int, int, int, int, str, float]] = []
    confidences: list[float] = []
    for block in response.get("Blocks") or []:
        kind, text = block.get("BlockType"), block.get("Text")
        if not isinstance(text, str) or "\x00" in text:
            continue
        if kind == "LINE":
            lines.append(text)
        elif kind == "WORD":
            confidence = _confidence(block.get("Confidence"))
            if confidence is not None:
                confidences.append(confidence)
            if len(words) < MAX_WORDS:
                box = (block.get("Geometry") or {}).get("BoundingBox") or {}
                words.append(
                    (
                        _unit(box.get("Left")),
                        _unit(box.get("Top")),
                        _unit(box.get("Width")),
                        _unit(box.get("Height")),
                        text[:MAX_WORD_CHARS],
                        confidence if confidence is not None else 0.0,
                    )
                )
    joined = "\n".join(lines)
    while len(joined.encode("utf-8")) > MAX_TEXT_BYTES:
        joined = joined[: len(joined) * 9 // 10]
    version = response.get("DetectDocumentTextModelVersion")
    request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
    return OcrPage(
        text=joined,
        words=tuple(words),
        confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
        model_version=version[:MAX_VERSION_CHARS] if isinstance(version, str) else None,
        request_id=request_id[:MAX_REQUEST_ID_CHARS] if isinstance(request_id, str) else None,
    )


def _code(error: ClientError) -> str:
    code = (error.response.get("Error") or {}).get("Code")
    return code if isinstance(code, str) and ERROR_NAME.fullmatch(code) else UNNAMED


class TextractOcr:
    """`DetectDocumentText`, tried again with backoff while Textract is busy."""

    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._retry_seconds = retry_seconds

    @classmethod
    def for_region(cls, region: str) -> TextractOcr:
        import boto3

        # boto3's stubs here have no Textract client: it is used as `Any`.
        client: Any = boto3.client(SERVICE, region_name=region, config=CLIENT_CONFIG)  # type: ignore[call-overload]
        return cls(client)

    def read(self, image: bytes) -> OcrPage:
        deadline = self._clock() + self._retry_seconds
        delay = FIRST_DELAY_SECONDS
        while True:
            try:
                return parse(self._client.detect_document_text(Document={"Bytes": image}))
            except ClientError as error:
                code = _code(error)
                if code in PAGE_REFUSALS:
                    raise PageRefused(code) from None
                if code not in RETRIED:
                    raise OcrFailed(code) from None
                name = code
            except BotoCoreError as error:
                # No answer at all: a timeout, a connection that could not be made.
                name = type(error).__name__
            # Half the delay at least, so the calls of several tasks spread out.
            wait = delay * (0.5 + self._jitter() / 2)
            if self._clock() + wait > deadline:
                raise OcrFailed(name) from None
            self._sleep(wait)
            delay = min(delay * 2, MAX_DELAY_SECONDS)
