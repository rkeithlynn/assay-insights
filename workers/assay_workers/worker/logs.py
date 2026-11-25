"""Structured JSON logs on stdout, which the awslogs driver sends to CloudWatch.

The level is in `level`, as in the Lambda functions' logs, so the same metric filter
(`{ $.level = "ERROR" }`) raises the worker's alarm. An exception is logged by its type
only, never its message or traceback: a message can quote what a library was handed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

# Attributes every LogRecord has; anything else was passed through `extra=`.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


def configure() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # botocore logs request details at DEBUG; keep it and its friends at WARNING.
    for noisy in ("boto3", "botocore", "urllib3", "sqlalchemy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
