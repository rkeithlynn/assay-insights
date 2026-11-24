"""Writes queued sign-ins and password resets to `audit_log` (BUILD_SPEC.md section 7a).

An SQS-triggered Lambda in the VPC. It signs in as `audit_writer` and does one thing per
message: `SELECT auth_events.record(event_id, pool, sub, action, at)` (migration 0004). That
SECURITY DEFINER function finds the user's tenant, ID, email and role in `users`, so the
tenant never comes from the message, and it refuses any action but `sign_in`,
`temporary_password_accepted` (migration 0006) and `password_reset`.

A message fails, and is retried until it reaches the dead-letter queue (which raises an
alarm), when it does not parse, names a user pool other than the two of this stack, or
names a user the database does not know. Nothing is dropped silently. The event ID is the
audit row's ID, so a message delivered twice is recorded once.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from assay_db.connect import DbTarget, iam_engine
from assay_db.roles import AUDIT_WRITER

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIONS = frozenset({"sign_in", "temporary_password_accepted", "password_reset"})
MESSAGE_VERSION = 1
RECORDED = "recorded"
# A queued event older than this is still recorded; one from the future is refused.
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)

_engine: Engine | None = None


class BadMessage(ValueError):
    """The message is not an auth event this stack produced."""


@dataclass(frozen=True)
class AuthEvent:
    event_id: uuid.UUID
    pool: str
    sub: str
    action: str
    at: dt.datetime


def pools_from_env() -> dict[str, str]:
    return {os.environ["TENANT_POOL_ID"]: "tenant", os.environ["PLATFORM_POOL_ID"]: "platform"}


def parse(body: str, pools: dict[str, str], now: dt.datetime) -> AuthEvent:
    try:
        raw: Any = json.loads(body)
        if not isinstance(raw, dict) or raw.get("version") != MESSAGE_VERSION:
            raise BadMessage("unknown message version")
        pool = pools.get(str(raw["user_pool_id"]))
        if pool is None:
            raise BadMessage("unknown user pool")
        action = str(raw["action"])
        if action not in ACTIONS:
            raise BadMessage("action not allowed")
        sub = str(raw["sub"])
        if not sub or len(sub) > 128:
            raise BadMessage("bad sub")
        at = dt.datetime.fromisoformat(str(raw["at"]))
        if at.tzinfo is None or at > now + MAX_CLOCK_SKEW:
            raise BadMessage("bad time")
        return AuthEvent(
            event_id=uuid.UUID(str(raw["event_id"])), pool=pool, sub=sub, action=action, at=at
        )
    except BadMessage:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise BadMessage(type(exc).__name__) from None


def engine() -> Engine:
    global _engine  # one pool per execution environment
    if _engine is None:
        _engine = iam_engine(
            DbTarget.from_env(), AUDIT_WRITER, application_name="assay-auth-events", pool_size=1
        )
    return _engine


def record(db: Engine, event: AuthEvent) -> str:
    with db.begin() as conn:
        result = conn.execute(
            text("SELECT auth_events.record(:id, :pool, :sub, :action, :at)"),
            {
                "id": event.event_id,
                "pool": event.pool,
                "sub": event.sub,
                "action": event.action,
                "at": event.at,
            },
        ).scalar_one()
    return str(result)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    pools = pools_from_env()
    failures: list[dict[str, str]] = []
    now = dt.datetime.now(dt.UTC)
    for item in event.get("Records", []):
        message_id = str(item.get("messageId", ""))
        try:
            auth_event = parse(str(item.get("body", "")), pools, now)
            outcome = record(engine(), auth_event)
            if outcome != RECORDED:
                # unknown_user: kept, retried, and finally dead-lettered (and alarmed).
                log.error("auth event not recorded", extra={"outcome": outcome})
                failures.append({"itemIdentifier": message_id})
            else:
                log.info("auth event recorded", extra={"action": auth_event.action})
        except BadMessage as exc:
            log.error("auth event refused", extra={"reason": str(exc)})
            failures.append({"itemIdentifier": message_id})
        except Exception as exc:
            log.error("auth event failed", extra={"error": type(exc).__name__})
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
