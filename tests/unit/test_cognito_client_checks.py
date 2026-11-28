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


class Rotating(FakePublic):
    """Cognito with rotation on, as the isolation run of 2026-11-27 met it: the old flow is
    `UnsupportedOperationException` whatever the token, and a rotated-out token used after
    its grace period is `RefreshTokenReuseException`. Each keyword breaks one thing."""

    def __init__(
        self,
        *,
        rotates: bool = True,
        old_flow: str = "UnsupportedOperationException",
        old_flow_for_a_bad_token: str = "UnsupportedOperationException",
        reuse: str | None = "RefreshTokenReuseException",
        reuse_revokes_the_family: bool = False,
    ) -> None:
        super().__init__()
        self.rotates = rotates
        self.old_flow, self.old_flow_for_a_bad_token = old_flow, old_flow_for_a_bad_token
        self.reuse, self.revokes = reuse, reuse_revokes_the_family
        self.rotated_out: set[str] = set()
        self.revoked = False
        self.flow_tokens: list[str] = []

    def initiate_auth(
        self, *, ClientId: str, AuthFlow: str, AuthParameters: dict[str, str]
    ) -> dict[str, Any]:
        assert AuthFlow == "REFRESH_TOKEN_AUTH"
        token = AuthParameters["REFRESH_TOKEN"]
        self.flow_tokens.append(token)
        known = token in {OLD, NEW}
        raise refused(self.old_flow if known else self.old_flow_for_a_bad_token, "refused")

    def get_tokens_from_refresh_token(self, *, RefreshToken: str, ClientId: str) -> dict[str, Any]:
        if RefreshToken in self.rotated_out:
            if self.reuse is not None:
                if self.revokes:
                    self.revoked = True
                raise refused(self.reuse, "refused")
        elif self.revoked:
            raise refused("NotAuthorizedException", "Refresh Token has been revoked")
        if self.rotates:
            self.rotated_out.add(RefreshToken)
        return {
            "AuthenticationResult": {
                "RefreshToken": NEW if self.rotates else RefreshToken,
                "AccessToken": "access",
            }
        }

    def get_user(self, *, AccessToken: str) -> dict[str, Any]:
        if self.revoked:
            raise refused("NotAuthorizedException", "Access Token has been revoked")
        return {"Username": "probe"}


OLD = "old-refresh-token-" + "x" * 40
NEW = "new-refresh-token-" + "y" * 40
OLD_FLOW = "replica: REFRESH_TOKEN_AUTH with the user's current refresh token"
OLD_FLOW_BAD_TOKEN = (
    "replica: REFRESH_TOKEN_AUTH with a token that is not valid gets the same answer"
)
REUSED = "replica: the old refresh token, used again after the grace period"
NEW_STILL_WORKS = "replica: after that reuse, the new refresh token still refreshes"
ACCESS_STILL_WORKS = (
    "replica: after that reuse, the access token issued before it is still accepted"
)


def rotation_checks(monkeypatch: pytest.MonkeyPatch, cognito: Rotating) -> list[checks.Check]:
    monkeypatch.setattr("tests.isolation.cognito_clients.time.sleep", lambda seconds: None)
    return checks.refresh_checks(None, cognito, client_id="c", refresh_token=OLD)


def test_rotation_on_the_replica_requires_cognitos_answers_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolation run of 2026-11-27: both answers were refusals, and both checks failed,
    because they had been written for a guess at what Cognito would say."""
    cognito = Rotating()
    good = rotation_checks(monkeypatch, cognito)
    assert failing(good) == [] and len(good) == 6
    expected = {c.check: c.expected for c in good}
    assert expected[OLD_FLOW] == expected[OLD_FLOW_BAD_TOKEN] == "UnsupportedOperationException"
    assert expected[REUSED] == "RefreshTokenReuseException"
    assert expected[NEW_STILL_WORKS] == expected[ACCESS_STILL_WORKS] == "allowed"
    # The old flow was asked with the current token, then with one that is not valid: the
    # same length, one character different.
    current, altered = cognito.flow_tokens
    assert current == NEW and altered != NEW and len(altered) == len(NEW)
    assert sum(a != b for a, b in zip(altered, NEW, strict=True)) == 1


def test_a_refresh_that_does_not_rotate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    found = rotation_checks(monkeypatch, Rotating(rotates=False, reuse=None))
    assert "replica: a refresh returns a new refresh token" in failing(found)
    assert REUSED in failing(found)  # the old token went on working


@pytest.mark.parametrize(
    "other",
    ["NotAuthorizedException", "InvalidParameterException", "UserNotFoundException"],
)
def test_any_other_answer_from_the_old_flow_fails_even_another_refusal(
    monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    assert failing(rotation_checks(monkeypatch, Rotating(old_flow=other))) == [OLD_FLOW]


def test_an_old_flow_that_judges_the_token_first_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a token that is not valid got "invalid token" where a valid one got "not enabled",
    Cognito would have looked at the token before refusing the flow."""
    found = rotation_checks(
        monkeypatch, Rotating(old_flow_for_a_bad_token="NotAuthorizedException")
    )
    assert failing(found) == [OLD_FLOW_BAD_TOKEN]


@pytest.mark.parametrize("other", ["NotAuthorizedException", "InvalidParameterException", None])
def test_any_other_answer_to_a_reused_token_fails_even_another_refusal(
    monkeypatch: pytest.MonkeyPatch, other: str | None
) -> None:
    """`None`: the rotated-out token still works, which is the failure that matters most."""
    assert failing(rotation_checks(monkeypatch, Rotating(reuse=other))) == [REUSED]


def test_a_reuse_that_revoked_the_users_other_tokens_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tabs sharing one refresh token: the second to refresh is sent to sign-in. If its
    attempt also revoked the first tab's tokens, the user would be signed out everywhere."""
    found = rotation_checks(monkeypatch, Rotating(reuse_revokes_the_family=True))
    assert failing(found) == [NEW_STILL_WORKS, ACCESS_STILL_WORKS]


def test_an_unrecognised_error_is_not_called_a_refusal_or_an_authentication() -> None:
    """A check that meets an answer it does not know says which, and fails."""
    answer = checks._flow_answer(lambda: (_ for _ in ()).throw(refused("OddException", "x")))
    assert answer == "not refused as a flow (OddException)"
    assert answer != checks.FLOW_REFUSED
