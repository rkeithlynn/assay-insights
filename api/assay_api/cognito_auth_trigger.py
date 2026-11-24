"""Cognito post-authentication and post-confirmation trigger, on both pools (section 7a).

Records two events by queuing them on SQS; the consumer (`assay_api.auth_event_consumer`)
writes them to `audit_log`:

- `PostAuthentication_Authentication`: a sign-in (`sign_in`), or, while Cognito still reports
  the user as `FORCE_CHANGE_PASSWORD`, the temporary password being accepted
  (`temporary_password_accepted`). A first sign-in on the tenant pool runs this trigger twice:
  once when the temporary password is accepted (a new-password challenge follows) and once
  when the new password is set, just before tokens are issued. Only the second is the
  sign-in. Any other status, or none, is recorded as `sign_in`.
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
USER_STATUS = "cognito:user_status"
# Cognito's status for a user who has not yet replaced the temporary password.
FIRST_SIGN_IN_STATUS = "FORCE_CHANGE_PASSWORD"
FIRST_SIGN_IN_ACTION = "temporary_password_accepted"
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


def user_status(event: dict[str, Any]) -> str | None:
    status = event.get("request", {}).get("userAttributes", {}).get(USER_STATUS)
    return str(status) if status is not None else None


def message_for(event: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
    """The queue message for an event worth recording, or None for any other event."""
    action = ACTIONS.get(str(event.get("triggerSource")))
    if action is None:
        return None
    if action == "sign_in" and user_status(event) == FIRST_SIGN_IN_STATUS:
        action = FIRST_SIGN_IN_ACTION
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
        # The user status (never an identifier) shows which events Cognito sent with which.
        log.info(
            "auth event queued",
            extra={"action": message["action"], "user_status": user_status(event)},
        )
    except Exception as exc:  # never block a sign-in
        log.error(NOT_QUEUED, extra={"error": type(exc).__name__})
    return event
