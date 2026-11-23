"""The Cognito admin Lambda's `invite` operation, with a fake Cognito client."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from botocore.exceptions import ClientError

from assay_api import cognito_admin
from assay_tss.provision.invite import InviteError, LambdaInviter

POOL = "us-east-1_TENANTPOOL"
TENANT_A = str(uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"))
TENANT_B = str(uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002"))


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "from Cognito"}}, "AdminCreateUser")


class FakeCognito:
    """Remembers users by email, as the tenant pool does."""

    def __init__(self) -> None:
        self.users: dict[str, list[dict[str, str]]] = {}
        self.groups: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        self.create_error: ClientError | None = None

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        assert kwargs["UserPoolId"] == POOL
        email = kwargs["Username"]
        if email in self.users:
            raise client_error("UsernameExistsException")
        self.created.append(kwargs)
        attributes = [*kwargs["UserAttributes"], {"Name": "sub", "Value": f"sub-{email}"}]
        self.users[email] = attributes
        return {"User": {"Username": email, "Attributes": attributes}}

    def admin_get_user(self, **kwargs: Any) -> dict[str, Any]:
        return {"Username": kwargs["Username"], "UserAttributes": self.users[kwargs["Username"]]}

    def admin_add_user_to_group(self, **kwargs: Any) -> dict[str, Any]:
        self.groups.append((kwargs["Username"], kwargs["GroupName"]))
        return {}


@pytest.fixture
def cognito(monkeypatch: pytest.MonkeyPatch) -> FakeCognito:
    fake = FakeCognito()
    monkeypatch.setattr(cognito_admin, "_client", fake)
    monkeypatch.setenv("TENANT_POOL_ID", POOL)
    return fake


def invite_event(**changes: Any) -> dict[str, Any]:
    return {
        "operation": "invite",
        "tenant_id": TENANT_A,
        "email": "ada@example.test",
        "name": "Ada  Admin",
        "role": "tenant-admin",
    } | changes


def test_invite_creates_the_user_with_an_immutable_tenant_id(cognito: FakeCognito) -> None:
    result = cognito_admin.handler(invite_event(), None)

    assert result == {"sub": "sub-ada@example.test", "created": True}
    [call] = cognito.created
    attributes = {a["Name"]: a["Value"] for a in call["UserAttributes"]}
    assert attributes == {
        "email": "ada@example.test",
        "email_verified": "true",
        "name": "Ada Admin",
        "custom:tenant_id": TENANT_A,
    }
    # Cognito sends the invitation itself; the Lambda never chooses a password.
    assert call["DesiredDeliveryMediums"] == ["EMAIL"]
    assert "TemporaryPassword" not in call and "MessageAction" not in call
    assert cognito.groups == [("ada@example.test", "tenant-admin")]


def test_repeating_an_invitation_returns_the_same_user(cognito: FakeCognito) -> None:
    first = cognito_admin.handler(invite_event(), None)
    second = cognito_admin.handler(invite_event(), None)
    assert second == {"sub": first["sub"], "created": False}
    assert len(cognito.created) == 1


def test_an_address_in_another_tenant_is_refused_without_naming_it(cognito: FakeCognito) -> None:
    cognito_admin.handler(invite_event(tenant_id=TENANT_B), None)
    result = cognito_admin.handler(invite_event(tenant_id=TENANT_A), None)
    assert result == {"error": "email_in_use"}
    assert TENANT_B not in json.dumps(result)
    # Nothing about the existing user changed: still tenant B's, and no new group.
    assert cognito.groups == [("ada@example.test", "tenant-admin")]


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "not-a-uuid"},
        {"tenant_id": ""},
        {"email": "no-at-sign"},
        {"email": "two words@example.test"},
        {"name": "   "},
        {"name": "x" * 201},
        {"role": "master-admin"},
        {"role": "tenant-owner"},
    ],
)
def test_invalid_invitations_never_reach_cognito(
    cognito: FakeCognito, changes: dict[str, Any]
) -> None:
    assert cognito_admin.handler(invite_event(**changes), None) == {"error": "invalid_request"}
    assert cognito.created == [] and cognito.groups == []


def test_missing_field_is_refused(cognito: FakeCognito) -> None:
    event = invite_event()
    del event["tenant_id"]
    assert cognito_admin.handler(event, None) == {"error": "invalid_request"}
    assert cognito.created == []


@pytest.mark.parametrize("operation", [None, "disable", "reset_mfa", "delete", "probe"])
def test_only_invite_is_served(cognito: FakeCognito, operation: str | None) -> None:
    assert cognito_admin.handler(invite_event(operation=operation), None) == {
        "error": "invalid_request"
    }
    assert cognito.created == []


def test_other_cognito_errors_fail_the_invocation(cognito: FakeCognito) -> None:
    cognito.create_error = client_error("TooManyRequestsException")
    with pytest.raises(ClientError):
        cognito_admin.handler(invite_event(), None)


# --- The provisioning task's side of the call ---------------------------------------------------


class FakeLambda:
    def __init__(self, payload: Any, *, function_error: str | None = None) -> None:
        self.payload = payload
        self.function_error = function_error
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)

        class Body:
            def read(inner) -> bytes:
                return json.dumps(self.payload).encode()

        response: dict[str, Any] = {"Payload": Body()}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


def inviter(fake: FakeLambda) -> LambdaInviter:
    return LambdaInviter("invite-fn", region="us-east-1", client=fake)  # type: ignore[arg-type]


def test_inviter_sends_the_tenant_id_it_was_given() -> None:
    fake = FakeLambda({"sub": "sub-1", "created": True})
    result = inviter(fake).invite(
        tenant_id=uuid.UUID(TENANT_A), email="ada@example.test", name="Ada Admin"
    )
    assert (result.cognito_sub, result.created) == ("sub-1", True)
    [call] = fake.calls
    assert call["FunctionName"] == "invite-fn"
    assert json.loads(call["Payload"]) == {
        "operation": "invite",
        "tenant_id": TENANT_A,
        "email": "ada@example.test",
        "name": "Ada Admin",
        "role": "tenant-admin",
    }


@pytest.mark.parametrize(
    ("payload", "function_error", "code"),
    [
        ({"error": "email_in_use"}, None, "email_in_use"),
        ({"errorMessage": "An error occurred: secret detail"}, "Unhandled", "invite_failed"),
        ({"created": True}, None, "invite_failed"),
        (None, None, "invite_failed"),
        ("a string", None, "invite_failed"),
    ],
)
def test_inviter_turns_refusals_into_short_codes(
    payload: Any, function_error: str | None, code: str
) -> None:
    with pytest.raises(InviteError) as raised:
        inviter(FakeLambda(payload, function_error=function_error)).invite(
            tenant_id=uuid.UUID(TENANT_A), email="ada@example.test", name="Ada Admin"
        )
    assert str(raised.value) == code
