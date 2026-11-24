"""The Cognito auth-event trigger and the consumer's message handling (no AWS, no database)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest

from assay_api import auth_event_consumer as consumer
from assay_api import cognito_auth_trigger as trigger

NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)
POOLS = {"us-east-1_T": "tenant", "us-east-1_P": "platform"}


def cognito_event(source: str, pool: str = "us-east-1_T") -> dict[str, Any]:
    return {
        "version": "1",
        "triggerSource": source,
        "userPoolId": pool,
        "userName": "user-1",
        "request": {
            "userAttributes": {
                "sub": "sub-1",
                "email": "ada@example.test",
                "custom:tenant_id": str(uuid.uuid4()),
            }
        },
        "response": {},
    }


class FakeSqs:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.sent.append(kwargs)
        return {"MessageId": "m"}


@pytest.fixture
def sqs(monkeypatch: pytest.MonkeyPatch) -> FakeSqs:
    fake = FakeSqs()
    monkeypatch.setattr(trigger, "_client", fake)
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example.test/queue")
    return fake


@pytest.mark.parametrize(
    ("source", "action"),
    [
        ("PostAuthentication_Authentication", "sign_in"),
        ("PostConfirmation_ConfirmForgotPassword", "password_reset"),
    ],
)
def test_the_trigger_queues_only_ids_and_returns_the_event(
    sqs: FakeSqs, source: str, action: str
) -> None:
    event = cognito_event(source)
    assert trigger.handler(event, None) is event
    [sent] = sqs.sent
    body = json.loads(sent["MessageBody"])
    assert set(body) == {"version", "event_id", "user_pool_id", "sub", "action", "at"}
    assert (body["action"], body["sub"], body["user_pool_id"]) == (action, "sub-1", "us-east-1_T")
    # No email, no name, no tenant: the consumer finds the tenant in the database.
    assert "ada@example.test" not in sent["MessageBody"]
    assert "custom:tenant_id" not in sent["MessageBody"]


def test_other_trigger_sources_are_ignored(sqs: FakeSqs) -> None:
    event = cognito_event("PostConfirmation_ConfirmSignUp")
    assert trigger.handler(event, None) is event
    assert sqs.sent == []


def test_the_trigger_never_blocks_a_sign_in(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(trigger, "_client", FakeSqs(error=TimeoutError("sqs unreachable")))
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example.test/queue")
    event = cognito_event("PostAuthentication_Authentication")
    assert trigger.handler(event, None) is event
    assert any(
        r.levelname == "ERROR" and r.getMessage() == trigger.NOT_QUEUED for r in caplog.records
    )


def message(**changes: Any) -> str:
    body = {
        "version": 1,
        "event_id": str(uuid.uuid4()),
        "user_pool_id": "us-east-1_T",
        "sub": "sub-1",
        "action": "sign_in",
        "at": NOW.isoformat(),
    } | changes
    return json.dumps(body)


def test_the_consumer_parses_a_message_from_the_trigger() -> None:
    body = trigger.message_for(cognito_event("PostAuthentication_Authentication"), NOW)
    assert body is not None
    parsed = consumer.parse(json.dumps(body), POOLS, NOW)
    assert (parsed.pool, parsed.sub, parsed.action, parsed.at) == (
        "tenant",
        "sub-1",
        "sign_in",
        NOW,
    )


@pytest.mark.parametrize(
    "body",
    [
        message(user_pool_id="us-east-1_OTHER"),
        message(action="role_changed"),
        message(version=2),
        message(at="2026-11-23T12:00:00"),  # no time zone
        message(at=(NOW + dt.timedelta(hours=1)).isoformat()),  # from the future
        message(event_id="not-a-uuid"),
        message(sub=""),
        "not json",
        "[]",
    ],
)
def test_the_consumer_refuses_anything_else(body: str) -> None:
    with pytest.raises(consumer.BadMessage):
        consumer.parse(body, POOLS, NOW)


def test_unknown_users_and_bad_messages_are_kept_for_the_dead_letter_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_POOL_ID", "us-east-1_T")
    monkeypatch.setenv("PLATFORM_POOL_ID", "us-east-1_P")
    outcomes = iter(["recorded", "unknown_user"])
    monkeypatch.setattr(consumer, "engine", lambda: object())
    monkeypatch.setattr(consumer, "record", lambda db, event: next(outcomes))
    result = consumer.handler(
        {
            "Records": [
                {"messageId": "ok", "body": message()},
                {"messageId": "unknown", "body": message()},
                {"messageId": "bad", "body": "not json"},
            ]
        },
        None,
    )
    assert result == {
        "batchItemFailures": [{"itemIdentifier": "unknown"}, {"itemIdentifier": "bad"}]
    }
