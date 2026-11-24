"""The Cognito admin Lambda, with a fake Cognito client.

- `<prefix>-cognito-admin` (provisioning): `invite` only.
- `<prefix>-cognito-admin-api` (build step 5): the API's user operations, each of which
  confirms that the target is the caller's user in the caller's tenant before acting.
"""

from __future__ import annotations

import json
import re
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
    # The provisioning role's function serves `invite` only.
    monkeypatch.setenv("OPERATIONS", "invite")
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


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        (None, "invalid_request"),
        ("delete", "invalid_request"),
        ("probe", "invalid_request"),
        # Real operations, but not this function's.
        ("disable", "operation_not_allowed"),
        ("reset_mfa", "operation_not_allowed"),
        ("set_role", "operation_not_allowed"),
    ],
)
def test_the_provisioning_function_serves_only_invite(
    cognito: FakeCognito, operation: str | None, error: str
) -> None:
    assert cognito_admin.handler(invite_event(operation=operation), None) == {"error": error}
    assert cognito.created == []


def test_operations_must_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATIONS", raising=False)
    with pytest.raises(RuntimeError):
        cognito_admin.allowed_operations({})
    with pytest.raises(RuntimeError):
        cognito_admin.allowed_operations({"OPERATIONS": "invite,delete_everything"})


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


# --- The API function's role grants exactly what the code calls --------------------------------


def _iam_action(method: str) -> str:
    return "cognito-idp:" + "".join(part.capitalize() for part in method.split("_"))


def test_the_api_functions_role_grants_exactly_the_calls_the_code_makes() -> None:
    from pathlib import Path

    from assay_infra import cognito_admin as infra

    source = Path(cognito_admin.__file__).read_text()
    calls = {_iam_action(m) for m in re.findall(r"\.(admin_\w+)\(", source)}
    assert set(infra.TENANT_POOL_ACTIONS) == calls
    assert "cognito-idp:AdminSetUserMFAPreference" not in infra.TENANT_POOL_ACTIONS
    assert "cognito-idp:AdminSetUserMFAPreference" not in infra.PLATFORM_POOL_ACTIONS
    assert set(infra.PLATFORM_POOL_ACTIONS) <= calls


# --- The API's function: every operation confirms its target ---------------------------------

PLATFORM_POOL = "us-east-1_PLATFORMPOOL"


class PoolsFake:
    """Both pools. Users by (pool, email): attributes, groups, status, enabled, TOTP."""

    def __init__(self) -> None:
        self.users: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def add(
        self,
        pool: str,
        email: str,
        *,
        tenant: str | None,
        groups: list[str],
        status: str = "CONFIRMED",
    ) -> None:
        attributes = [{"Name": "sub", "Value": f"sub-{email}"}, {"Name": "email", "Value": email}]
        if tenant is not None:
            attributes.append({"Name": "custom:tenant_id", "Value": tenant})
        self.users[(pool, email)] = {
            "attributes": attributes,
            "groups": list(groups),
            "status": status,
            "enabled": True,
            "totp": True,
        }

    def _user(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        key = (kwargs["UserPoolId"], kwargs["Username"])
        if key not in self.users:
            raise client_error("UserNotFoundException")
        return self.users[key]

    def admin_get_user(self, **kwargs: Any) -> dict[str, Any]:
        user = self._user(kwargs)
        return {
            "Username": kwargs["Username"],
            "UserAttributes": user["attributes"],
            "UserStatus": user["status"],
            "Enabled": user["enabled"],
        }

    def admin_list_groups_for_user(self, **kwargs: Any) -> dict[str, Any]:
        return {"Groups": [{"GroupName": g} for g in self._user(kwargs)["groups"]]}

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("admin_"):
            raise AttributeError(name)

        def record(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            user = self._user(kwargs)
            if name == "admin_add_user_to_group":
                user["groups"].append(kwargs["GroupName"])
            elif name == "admin_remove_user_from_group":
                user["groups"].remove(kwargs["GroupName"])
            elif name == "admin_disable_user":
                user["enabled"] = False
            elif name == "admin_delete_software_token":
                if not user["totp"]:
                    raise client_error("ResourceNotFoundException")
                user["totp"] = False
            return {}

        return record

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> PoolsFake:
    fake = PoolsFake()
    fake.add(POOL, "ada@example.test", tenant=TENANT_A, groups=["tenant-user"])
    fake.add(
        POOL,
        "invited@example.test",
        tenant=TENANT_A,
        groups=["tenant-user"],
        status="FORCE_CHANGE_PASSWORD",
    )
    fake.add(POOL, "bob@example.test", tenant=TENANT_B, groups=["tenant-user"])
    fake.add(PLATFORM_POOL, "master@example.test", tenant=None, groups=["master-admin"])
    fake.add(PLATFORM_POOL, "muser@example.test", tenant=None, groups=["master-user"])
    monkeypatch.setattr(cognito_admin, "_client", fake)
    monkeypatch.setenv("TENANT_POOL_ID", POOL)
    monkeypatch.setenv("PLATFORM_POOL_ID", PLATFORM_POOL)
    monkeypatch.setenv(
        "OPERATIONS", "invite,resend_invite,reset_password,disable,enable,set_role,reset_mfa"
    )
    return fake


def target(email: str = "ada@example.test", **changes: Any) -> dict[str, Any]:
    return {
        "pool": "tenant",
        "tenant_id": TENANT_A,
        "email": email,
        "sub": f"sub-{email}",
    } | changes


@pytest.mark.parametrize(
    "operation", ["disable", "enable", "reset_password", "reset_mfa", "set_role"]
)
@pytest.mark.parametrize(
    "event",
    [
        # Another tenant's user, named with this tenant's ID: refused, and the tenant not named.
        target("bob@example.test"),
        # The right address but someone else's sub.
        target(sub="sub-someone-else"),
        # No tenant ID at all.
        {"pool": "tenant", "email": "ada@example.test", "sub": "sub-ada@example.test"},
    ],
)
def test_every_tenant_operation_refuses_a_user_outside_the_tenant(
    pools: PoolsFake, operation: str, event: dict[str, Any]
) -> None:
    result = cognito_admin.handler({"operation": operation, "role": "tenant-admin", **event}, None)
    assert result["error"] in {"user_not_found", "invalid_request"}
    assert TENANT_B not in json.dumps(result)
    assert [n for n in pools.names() if n not in {"admin_get_user"}] == []


def test_disable_signs_the_user_out(pools: PoolsFake) -> None:
    assert cognito_admin.handler({"operation": "disable", **target()}, None) == {"ok": True}
    assert pools.names() == ["admin_disable_user", "admin_user_global_sign_out"]


def test_set_role_adds_the_new_group_then_removes_the_old(pools: PoolsFake) -> None:
    result = cognito_admin.handler(
        {"operation": "set_role", "role": "tenant-admin", **target()}, None
    )
    assert result == {"ok": True}
    assert pools.names() == ["admin_add_user_to_group", "admin_remove_user_from_group"]
    assert pools.users[(POOL, "ada@example.test")]["groups"] == ["tenant-admin"]


def test_set_role_refuses_a_platform_role(pools: PoolsFake) -> None:
    result = cognito_admin.handler(
        {"operation": "set_role", "role": "master-admin", **target()}, None
    )
    assert result == {"error": "invalid_request"}
    assert pools.calls == []


def test_reset_mfa_deletes_the_authenticator_and_signs_out(pools: PoolsFake) -> None:
    assert cognito_admin.handler({"operation": "reset_mfa", **target()}, None) == {
        "ok": True,
        "had_totp": True,
    }
    assert pools.names() == ["admin_delete_software_token", "admin_user_global_sign_out"]
    # Again, with no authenticator left: still signs out.
    assert cognito_admin.handler({"operation": "reset_mfa", **target()}, None)["had_totp"] is False


def test_resend_only_for_invited_and_reset_password_only_for_confirmed(pools: PoolsFake) -> None:
    assert cognito_admin.handler({"operation": "resend_invite", **target()}, None) == {
        "error": "wrong_state"
    }
    invited = target("invited@example.test")
    assert cognito_admin.handler({"operation": "resend_invite", **invited}, None) == {"ok": True}
    resend = dict(pools.calls[-1][1])
    assert resend["MessageAction"] == "RESEND"
    assert cognito_admin.handler({"operation": "reset_password", **invited}, None) == {
        "error": "wrong_state"
    }
    assert cognito_admin.handler({"operation": "reset_password", **target()}, None) == {"ok": True}


def test_platform_pool_allows_only_resetting_the_master_users_mfa(pools: PoolsFake) -> None:
    master_user = {
        "pool": "platform",
        "email": "muser@example.test",
        "sub": "sub-muser@example.test",
    }
    assert cognito_admin.handler({"operation": "reset_mfa", **master_user}, None)["ok"] is True
    master_admin = {
        "pool": "platform",
        "email": "master@example.test",
        "sub": "sub-master@example.test",
    }
    assert cognito_admin.handler({"operation": "reset_mfa", **master_admin}, None) == {
        "error": "operation_not_allowed"
    }
    for operation in ("disable", "enable", "set_role", "reset_password", "resend_invite", "invite"):
        result = cognito_admin.handler(
            {"operation": operation, "role": "master-user", **master_user}, None
        )
        assert result == {"error": "operation_not_allowed"}, operation
    assert pools.users[(PLATFORM_POOL, "master@example.test")]["totp"] is True
