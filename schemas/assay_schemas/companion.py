"""The companion file, version 1 (BUILD_SPEC.md section 8, "Companion file, version 1").

Validated with plain code, never a model. The input is untrusted: the caller has already
checked the stored size (at most `MAX_BYTES`) before reading it, and this module checks
again, then:

- decodes strict UTF-8 (a byte-order mark is allowed);
- refuses nesting deeper than a flat object before parsing, so no input can make the parser
  recurse;
- parses with no NaN or Infinity and no duplicate keys (compared case-insensitively, as the
  reserved keys are matched);
- checks every reserved key's type and every custom key's name and value.

`CompanionError.key` names the key at fault and `code` says what is wrong. Neither ever holds
a value from the file, so an error can be stored and shown without repeating content. Never
log a companion file's content.

Every key is checked, and every key's problem is reported at once (`CompanionError.errors`,
at most `MAX_ERRORS`; owner decision, 2026-11-24). A problem with the file as a whole (too
large, not UTF-8, nested, not JSON, not one object, a key twice) stops the check at once,
because nothing more can be read safely.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_BYTES = 64 * 1024
MAX_CUSTOM_KEYS = 50
MAX_TEXT = 500
MAX_SIGNERS = 3
# A flat object: the object itself is the only container.
MAX_DEPTH = 1
# The most key errors reported for one file.
MAX_ERRORS = 50

CUSTOM_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SIGNER_KEY = re.compile(r"^signer([1-9])(name|email|phone|location)$")

# Reserved keys, lower-cased (they are matched case-insensitively).
TENANT_ID = "tenantid"
TEXT_KEYS = {
    "applicationid": "application_ref",
    "loannumber": "loan_number",
    "cosignername": "cosigner_name",
}
DATE_KEYS = {
    "createdate": "package_created_on",
    "completedate": "package_completed_on",
    "loanmaturitydate": "loan_maturity_date",
}
LOAN_TERM = "loanterm"
LOAN_RATE = "loaninterestrate"
SIGNER_FIELDS = ("name", "email", "phone", "location")

LabelValue = str | int | float | bool


class CompanionError(Exception):
    """The file is invalid. `key` is the key at fault (None for the whole file).

    `errors` holds every problem found, this one first; `code` and `key` are the first's.
    """

    def __init__(
        self, code: str, key: str | None = None, *, errors: Sequence[CompanionError] = ()
    ) -> None:
        super().__init__(code if key is None else f"{code}: {key}")
        self.code = code
        self.key = key
        self.errors: tuple[CompanionError, ...] = tuple(errors) or (self,)

    def as_detail(self) -> dict[str, str | None]:
        return {"code": self.code, "key": self.key}

    def details(self) -> list[dict[str, str | None]]:
        """Every problem, for `batch_files.error_detail`: codes and keys, never values."""
        return [error.as_detail() for error in self.errors]

    @classmethod
    def of(cls, errors: Sequence[CompanionError]) -> CompanionError:
        first = errors[0]
        return cls(first.code, first.key, errors=errors)


@dataclass(frozen=True)
class CompanionSigner:
    number: int
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class Companion:
    """A valid companion file: reserved values by column, and the custom labels."""

    tenant_id: uuid.UUID | None = None
    application_ref: str | None = None
    loan_number: str | None = None
    cosigner_name: str | None = None
    package_created_on: dt.date | None = None
    package_completed_on: dt.date | None = None
    loan_term_months: int | None = None
    loan_maturity_date: dt.date | None = None
    loan_interest_rate: float | None = None
    signers: tuple[CompanionSigner, ...] = ()
    labels: dict[str, LabelValue] = field(default_factory=dict)


def _depth(text: str) -> int:
    """The deepest nesting of objects and arrays, ignoring brackets inside strings."""
    depth = deepest = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "}]":
            depth -= 1
    return deepest


def _no_constants(name: str) -> Any:
    raise CompanionError("not_json")


def _pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    seen: set[str] = set()
    for key, value in pairs:
        folded = key.lower()
        if folded in seen:
            raise CompanionError("duplicate_key", key)
        seen.add(folded)
        result[key] = value
    return result


def parse(raw: bytes) -> dict[str, Any]:
    """Strict JSON: a flat object, at most MAX_BYTES, no duplicate keys, no NaN."""
    if len(raw) > MAX_BYTES:
        raise CompanionError("too_large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CompanionError("not_utf8") from None
    if _depth(text) > MAX_DEPTH:
        raise CompanionError("nested_value")
    try:
        document = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constants)
    except CompanionError:
        raise
    except ValueError:
        raise CompanionError("not_json") from None
    if not isinstance(document, dict):
        raise CompanionError("not_an_object")
    return document


def _text(key: str, value: Any) -> str | None:
    if not isinstance(value, str):
        raise CompanionError("must_be_text", key)
    if len(value) > MAX_TEXT:
        raise CompanionError("too_long", key)
    value = value.strip()
    return value or None


def _date(key: str, value: Any) -> dt.date | None:
    if value == "":
        return None
    if not isinstance(value, str) or not _DATE.match(value):
        raise CompanionError("must_be_a_date", key)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise CompanionError("must_be_a_date", key) from None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(raw: bytes, *, tenant_id: uuid.UUID) -> Companion:
    """Return the companion file's values, or raise CompanionError.

    `tenant_id` is the tenant the upload belongs to, from the job or the token, never from
    the file. A `TenantID` in the file is only checked against it, never used.
    """
    document = parse(raw)
    values: dict[str, Any] = {}
    signers: dict[int, dict[str, str | None]] = {}
    labels: dict[str, LabelValue] = {}
    errors: list[CompanionError] = []

    for key, value in document.items():
        try:
            _check(key, value, tenant_id, values, signers, labels)
        except CompanionError as exc:
            errors.append(exc)
            if len(errors) >= MAX_ERRORS:
                break

    if len(labels) > MAX_CUSTOM_KEYS and len(errors) < MAX_ERRORS:
        errors.append(CompanionError("too_many_custom_keys"))
    if errors:
        raise CompanionError.of(errors)
    return Companion(
        **values,
        signers=tuple(
            CompanionSigner(number=n, **fields)
            for n, fields in sorted(signers.items())
            if any(fields.values())
        ),
        labels=labels,
    )


def _check(
    key: str,
    value: Any,
    tenant_id: uuid.UUID,
    values: dict[str, Any],
    signers: dict[int, dict[str, str | None]],
    labels: dict[str, LabelValue],
) -> None:
    """One key: store its value, or raise CompanionError for that key."""
    folded = key.lower()
    if folded == TENANT_ID:
        try:
            claimed = uuid.UUID(value) if isinstance(value, str) else None
        except ValueError:
            claimed = None
        if claimed is None:
            raise CompanionError("must_be_a_uuid", key)
        if claimed != tenant_id:
            raise CompanionError("tenant_mismatch", key)
        values["tenant_id"] = claimed
    elif folded in TEXT_KEYS:
        values[TEXT_KEYS[folded]] = _text(key, value)
    elif folded in DATE_KEYS:
        values[DATE_KEYS[folded]] = _date(key, value)
    elif folded == LOAN_TERM:
        if not isinstance(value, int) or isinstance(value, bool):
            raise CompanionError("must_be_a_whole_number", key)
        if not 1 <= value <= 1200:
            raise CompanionError("out_of_range", key)
        values["loan_term_months"] = value
    elif folded == LOAN_RATE:
        if not _number(value) or not math.isfinite(value):
            raise CompanionError("must_be_a_number", key)
        if not 0 <= value <= 100:
            raise CompanionError("out_of_range", key)
        values["loan_interest_rate"] = float(value)
    elif (match := _SIGNER_KEY.match(folded)) is not None:
        number = int(match.group(1))
        if number > MAX_SIGNERS:
            raise CompanionError("unknown_signer", key)
        signers.setdefault(number, {})[match.group(2)] = _text(key, value)
    else:
        labels[key] = _label(key, value)


def _label(key: str, value: Any) -> LabelValue:
    if not CUSTOM_KEY.match(key):
        raise CompanionError("invalid_key", key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            raise CompanionError("too_long", key)
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise CompanionError("must_be_a_number", key)
        return value
    if value is None:
        raise CompanionError("must_not_be_null", key)
    raise CompanionError("nested_value", key)
