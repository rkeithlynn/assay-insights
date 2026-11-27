"""tests/isolation/cognito_clients.py: the checks on the deployed portal app clients pass on
a correct system and fail when what they guard is broken (refresh-token rotation, owner
decision of 2026-11-25). The real run is the owner's; this exercises its logic with fakes."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from tests.isolation import cognito_clients as checks

CLIENTS = {"tenant": ("pool-t", "client-t"), "platform": ("pool-p", "client-p")}
ROTATED = {"tenant": True, "platform": False}
NOT_ENABLED = {
    "USER_PASSWORD_AUTH": "USER_PASSWORD_AUTH flow not enabled for this client",
    "USER_SRP_AUTH": "USER_SRP_AUTH is not enabled for the client.",
    "CUSTOM_AUTH": "Custom auth lambda trigger is not configured for the user pool.",
    "USER_AUTH": "USER_AUTH flow not enabled for this client",
}


def refused(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "InitiateAuth")


class FakeCognito:
    """The management calls: each deployed client and pool, as Cognito describes them."""

    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {
            "client-t": {
                "WriteAttributes": ["name"],
                "ExplicitAuthFlows": ["ALLOW_CUSTOM_AUTH"],
                "RefreshTokenRotation": {"Feature": "ENABLED", "RetryGracePeriodSeconds": 10},
            },
            "client-p": {
                "WriteAttributes": ["name"],
                "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
            },
        }
        self.triggers: dict[str, dict[str, str]] = {
            "pool-t": {"PostAuthentication": "arn", "PreTokenGeneration": "arn"},
            "pool-p": {"PostAuthentication": "arn"},
        }

    def describe_user_pool_client(self, *, UserPoolId: str, ClientId: str) -> dict[str, Any]:
        return {"UserPoolClient": self.clients[ClientId]}

    def describe_user_pool(self, *, UserPoolId: str) -> dict[str, Any]:
        return {"UserPool": {"LambdaConfig": self.triggers[UserPoolId]}}


class FakePublic:
    """The unsigned calls anyone can make. `answers` overrides a flow's answer."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[tuple[str, str, str]] = []

    def initiate_auth(
        self,
        *,
        ClientId: str,
        AuthFlow: str,
        AuthParameters: dict[str, str],
    ) -> dict[str, Any]:
        self.asked.append((ClientId, AuthFlow, AuthParameters.get("USERNAME", "")))
        answer = (
            self.answers[AuthFlow]
            if AuthFlow in self.answers
            else refused("InvalidParameterException", NOT_ENABLED[AuthFlow])
        )
        if isinstance(answer, Exception):
            raise answer
        result: dict[str, Any] = answer
        return result


def failing(found: list[checks.Check]) -> list[str]:
    return [check.check for check in found if not check.ok]


def test_a_correct_system_passes_every_check_and_signs_nobody_in() -> None:
    public = FakePublic()
    found = checks.deployed_clients(FakeCognito(), public, CLIENTS, ROTATED)
    assert failing(found) == []
    # Per client: writes, flows, rotation, triggers, and the four programmatic sign-ins.
    assert len(found) == 16
    assert {flow for _, flow, _ in public.asked} == set(checks.PROGRAMMATIC_FLOWS)
    assert {client for client, _, _ in public.asked} == {"client-t", "client-p"}
    # Only ever a user that cannot exist.
    assert all(name.endswith("@example.invalid") for _, _, name in public.asked)


def test_a_client_that_answers_an_srp_sign_in_fails() -> None:
    """What an empty flow list does (the probe, 2026-11-25)."""
    public = FakePublic({"USER_SRP_AUTH": {"ChallengeName": "PASSWORD_VERIFIER"}})
    found = checks.deployed_clients(FakeCognito(), public, CLIENTS, ROTATED)
    assert failing(found) == [
        "platform portal client: programmatic sign-in with USER_SRP_AUTH",
        "tenant portal client: programmatic sign-in with USER_SRP_AUTH",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        # The flow is enabled and only the password was wrong: it reached authentication.
        refused("NotAuthorizedException", "Incorrect username or password."),
        refused("UserNotFoundException", "User does not exist."),
        {"AuthenticationResult": {"AccessToken": "x"}},
    ],
)
def test_only_a_flow_that_cannot_be_used_counts_as_refused(answer: Any) -> None:
    public = FakePublic({"USER_PASSWORD_AUTH": answer})
    found = checks.deployed_clients(FakeCognito(), public, CLIENTS, ROTATED)
    assert len(failing(found)) == 2
    assert all("USER_PASSWORD_AUTH" in name for name in failing(found))


def test_a_custom_auth_trigger_on_either_pool_fails() -> None:
    cognito = FakeCognito()
    cognito.triggers["pool-p"]["DefineAuthChallenge"] = "arn"
    found = checks.deployed_clients(cognito, FakePublic(), CLIENTS, ROTATED)
    assert failing(found) == ["platform pool has no custom-auth trigger"]


def test_a_client_that_is_not_as_the_stack_says_fails() -> None:
    cognito = FakeCognito()
    # An empty list, the refresh flow kept beside rotation off, rotation with another grace.
    cognito.clients["client-t"]["ExplicitAuthFlows"] = []
    cognito.clients["client-t"]["RefreshTokenRotation"] = {
        "Feature": "ENABLED",
        "RetryGracePeriodSeconds": 60,
    }
    found = checks.deployed_clients(cognito, FakePublic(), CLIENTS, ROTATED)
    assert failing(found) == [
        "tenant portal client: explicit flows",
        "tenant portal client: refresh-token rotation",
    ]
    # And the stack expecting rotation on a client that has none.
    found = checks.deployed_clients(FakeCognito(), FakePublic(), CLIENTS, {"platform": True})
    assert "platform portal client: refresh-token rotation" in failing(found)
    assert "tenant portal client: refresh-token rotation" in failing(found)


def test_rotation_on_the_replica_is_checked_through_a_real_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tests.isolation.cognito_clients.time.sleep", lambda seconds: None)

    class Rotating(FakePublic):
        def __init__(self, *, rotates: bool, old_dies: bool) -> None:
            super().__init__(
                {
                    "REFRESH_TOKEN_AUTH": refused(
                        "InvalidParameterException",
                        "Refresh Token flow not enabled for this client",
                    )
                }
            )
            self.rotates, self.old_dies, self.used = rotates, old_dies, 0

        def get_tokens_from_refresh_token(
            self, *, RefreshToken: str, ClientId: str
        ) -> dict[str, Any]:
            self.used += 1
            if RefreshToken == "old" and self.used > 1 and self.old_dies:
                raise refused("NotAuthorizedException", "Invalid Refresh Token")
            return {"AuthenticationResult": {"RefreshToken": "new" if self.rotates else "old"}}

    good = checks.refresh_checks(
        None, Rotating(rotates=True, old_dies=True), client_id="c", refresh_token="old"
    )
    assert failing(good) == [] and len(good) == 4
    same = checks.refresh_checks(
        None, Rotating(rotates=False, old_dies=True), client_id="c", refresh_token="old"
    )
    assert "replica: a refresh returns a new refresh token" in failing(same)
    lives = checks.refresh_checks(
        None, Rotating(rotates=True, old_dies=False), client_id="c", refresh_token="old"
    )
    assert failing(lives) == ["replica: the old refresh token stops working after the grace period"]
