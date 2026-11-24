"""Cognito post-authentication and post-confirmation trigger, on both pools (section 7a).

Records two events by queuing them on SQS; the consumer (`assay_api.auth_event_consumer`)
writes them to `audit_log`:

- `PostAuthentication_Authentication`: a sign-in (`sign_in`).
- `PostConfirmation_ConfirmForgotPassword`: a password reset with an emailed code
  (`password_reset`).

Cognito waits at most 5 seconds for a trigger and fails the sign-in if the trigger fails or
times out, while Aurora at 0 ACU takes about 15 seconds to resume. So this function never
touches the database and never raises: it sends one small message (with short timeouts)
and returns the event. If it cannot, it logs an error, which raises an alarm, and the
sign-in goes ahead. Failed sign-ins are not recorded in this deployment.

It runs in the VPC and reaches SQS through the `sqs` interface endpoint, whose policy
admits only this function's role, only to send to this queue. The message carries the
user pool ID, the user's `sub`, the action and the time: no name, email, password or
token. The consumer maps the pool ID to the tenant or platform pool and finds the user's
tenant in the database, so nothing here chooses a tenant.

Only this file and `__init__.py` are in the trigger image, which uses the boto3 bundled with
the digest-pinned Lambda base image.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIONS = {
    "PostAuthentication_Authentication": "sign_in",
    "PostConfirmation_ConfirmForgotPassword": "password_reset",
}
MESSAGE_VERSION = 1
CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 2})
# Logged at ERROR; the stack's metric filter turns every ERROR line into an alarm.
NOT_QUEUED = "auth event not queued"

_client: SQSClient | None = None


def sqs() -> SQSClient:
    global _client  # one client per execution environment
    if _client is None:
        _client = boto3.client("sqs", config=CLIENT_CONFIG)
    return _client


def message_for(event: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
    """The queue message for an event worth recording, or None for any other event."""
    action = ACTIONS.get(str(event.get("triggerSource")))
    if action is None:
        return None
    sub = str(event["request"]["userAttributes"]["sub"])
    return {
        "version": MESSAGE_VERSION,
        "event_id": str(uuid.uuid4()),
        "user_pool_id": str(event["userPoolId"]),
        "sub": sub,
        "action": action,
        "at": now.isoformat(),
    }


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    try:
        message = message_for(event, dt.datetime.now(dt.UTC))
        if message is None:
            log.info("trigger source not recorded", extra={"source": event.get("triggerSource")})
            return event
        sqs().send_message(QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps(message))
        log.info("auth event queued", extra={"action": message["action"]})
    except Exception as exc:  # never block a sign-in
        log.error(NOT_QUEUED, extra={"error": type(exc).__name__})
    return event
