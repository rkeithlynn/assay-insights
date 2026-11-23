"""The tenant pool's pre-token-generation trigger (assay_api.cognito_pre_token). No AWS calls."""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from assay_api import cognito_pre_token as trigger

TENANT_ID = str(uuid.uuid4())

# The shape of a V2_0 event, trimmed to what the trigger reads.
EVENT: dict[str, Any] = {
    "version": "2",
    "triggerSource": "TokenGeneration_HostedAuth",
    "region": "us-east-1",
    "userPoolId": "us-east-1_Example",
    "userName": "3f1c-user",
    "callerContext": {"awsSdkVersion": "aws-sdk-unknown-unknown", "clientId": "client"},
    "request": {
        "userAttributes": {
            "sub": "3f1c-user",
            "email": "user@example.test",
            "custom:tenant_id": TENANT_ID,
        },
        "groupConfiguration": {"groupsToOverride": ["tenant-admin"]},
        "scopes": ["openid"],
    },
    "response": {"claimsAndScopeOverrideDetails": None},
}


class FakeCognito:
    def __init__(self, mfa: list[str] | None = None, error: Exception | None = None) -> None:
        self.mfa = mfa
        self.error = error
        self.calls: list[dict[str, str]] = []

    def admin_get_user(self, **kwargs: str) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        user: dict[str, Any] = {"Username": kwargs["Username"]}
        if self.mfa is not None:
            user["UserMFASettingList"] = self.mfa
        return user


def run(fake: FakeCognito, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> dict[str, Any]:
    monkeypatch.setattr(trigger, "_client", fake)
    event = copy.deepcopy(EVENT)
    event.update(changes)
    return trigger.handler(event, None)


def claims(result: dict[str, Any]) -> dict[str, Any]:
    details = result["response"]["claimsAndScopeOverrideDetails"]
    return dict(details["accessTokenGeneration"]["claimsToAddOrOverride"])


@pytest.mark.parametrize(
    ("mfa", "expected"),
    [(["SOFTWARE_TOKEN_MFA"], True), ([], False), (None, False), (["EMAIL_OTP"], False)],
)
def test_adds_tenant_id_and_mfa(
    monkeypatch: pytest.MonkeyPatch, mfa: list[str] | None, expected: bool
) -> None:
    fake = FakeCognito(mfa=mfa)
    result = run(fake, monkeypatch)
    assert claims(result) == {"tenant_id": TENANT_ID, "mfa": expected}
    assert fake.calls == [{"UserPoolId": "us-east-1_Example", "Username": "3f1c-user"}]


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "TooManyRequestsException"}}, "AdminGetUser"),
        EndpointConnectionError(endpoint_url="https://cognito-idp.us-east-1.amazonaws.com"),
    ],
)
def test_cognito_failure_fails_closed_without_blocking_sign_in(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    result = run(FakeCognito(error=error), monkeypatch)
    assert claims(result) == {"tenant_id": TENANT_ID, "mfa": False}


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", "../other"])
def test_no_token_without_a_valid_tenant_id(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    event = copy.deepcopy(EVENT)
    attributes = event["request"]["userAttributes"]
    if value is None:
        del attributes["custom:tenant_id"]
    else:
        attributes["custom:tenant_id"] = value
    fake = FakeCognito(mfa=["SOFTWARE_TOKEN_MFA"])
    with pytest.raises(trigger.TenantIdMissing):
        run(fake, monkeypatch, request=event["request"])
    assert fake.calls == []


def test_tenant_id_is_normalised() -> None:
    assert trigger.tenant_id_of({"custom:tenant_id": TENANT_ID.upper()}) == TENANT_ID


def test_refuses_v1_events(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="version 2"):
        run(FakeCognito(mfa=[]), monkeypatch, version="1")


def test_ignores_a_group_override_attempt_in_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trigger only adds claims; it never changes groups or scopes."""
    result = run(FakeCognito(mfa=[]), monkeypatch)
    details = result["response"]["claimsAndScopeOverrideDetails"]
    assert set(details) == {"accessTokenGeneration"}
    assert set(details["accessTokenGeneration"]) == {"claimsToAddOrOverride"}
