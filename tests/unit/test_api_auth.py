"""Access-token verification (assay_api.auth), with locally signed tokens. No AWS, no database."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from assay_api.auth import InvalidToken, Principal, TokenVerifier
from tests.unit.tokens import Issuer, make_issuer, new_key

TENANT = make_issuer("tenant")
PLATFORM = make_issuer("platform")
VERIFIER = TokenVerifier([TENANT.settings, PLATFORM.settings])
TENANT_ID = uuid.uuid4()


def tenant_token(**overrides: Any) -> str:
    args: dict[str, Any] = {
        "sub": "user-1",
        "groups": ["tenant-user"],
        "tenant_id": TENANT_ID,
        "mfa": False,
    }
    args.update(overrides)
    return TENANT.mint(**args)


def test_accepts_a_tenant_token() -> None:
    principal = VERIFIER.verify(tenant_token(groups=["tenant-admin"], mfa=True))
    assert principal.auth_time > 0
    assert principal == Principal(
        pool="tenant",
        sub="user-1",
        role="tenant-admin",
        tenant_id=TENANT_ID,
        mfa=True,
        auth_time=principal.auth_time,
    )


def test_accepts_a_platform_token_which_always_passed_mfa() -> None:
    principal = VERIFIER.verify(PLATFORM.mint(sub="m-1", groups=["master-user"]))
    assert principal == Principal(
        pool="platform",
        sub="m-1",
        role="master-user",
        tenant_id=None,
        mfa=True,
        auth_time=principal.auth_time,
    )


def test_a_token_without_auth_time_is_refused() -> None:
    """The account check compares it with users.sessions_revoked_at (MFA reset, disable)."""
    with pytest.raises(InvalidToken):
        VERIFIER.verify(tenant_token(groups=["tenant-user"], drop=("auth_time",)))


def test_platform_token_never_carries_a_tenant() -> None:
    token = PLATFORM.mint(sub="m-1", groups=["master-admin"], tenant_id=TENANT_ID)
    assert VERIFIER.verify(token).tenant_id is None


@pytest.mark.parametrize("mfa", [None, False, "true", 1, "yes"])
def test_mfa_is_true_only_for_the_boolean_claim(mfa: object) -> None:
    assert VERIFIER.verify(tenant_token(mfa=mfa)).mfa is False


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(tenant_token(expires_in=-120), id="expired"),
        pytest.param(tenant_token(token_use="id"), id="id-token"),
        pytest.param(tenant_token(client_id="another-client"), id="wrong-client"),
        pytest.param(
            PLATFORM.mint(sub="m", groups=["master-admin"], client_id=TENANT.settings.client_id),
            id="other-pools-client",
        ),
        pytest.param(
            tenant_token(issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Evil"),
            id="unknown-issuer",
        ),
        pytest.param(tenant_token(key=new_key()), id="forged-signature"),
        pytest.param(tenant_token(kid="unknown-kid"), id="unknown-key"),
        pytest.param(tenant_token(key=PLATFORM.key, kid=PLATFORM.kid), id="other-pools-key"),
        pytest.param(tenant_token(algorithm="HS256"), id="hmac"),
        pytest.param(tenant_token(algorithm="RS512"), id="other-rsa-algorithm"),
        pytest.param(tenant_token(groups=None), id="no-group"),
        pytest.param(tenant_token(groups=[]), id="empty-groups"),
        pytest.param(tenant_token(groups=["tenant-admin", "tenant-user"]), id="two-groups"),
        pytest.param(tenant_token(groups=["master-admin"]), id="platform-group-on-tenant-pool"),
        pytest.param(
            PLATFORM.mint(sub="m", groups=["tenant-admin"]), id="tenant-group-on-platform-pool"
        ),
        pytest.param(tenant_token(tenant_id=None), id="no-tenant-id"),
        pytest.param(tenant_token(tenant_id="not-a-uuid"), id="bad-tenant-id"),
        pytest.param(tenant_token(drop=("exp",)), id="no-exp"),
        pytest.param(tenant_token(drop=("iat",)), id="no-iat"),
        pytest.param(tenant_token(drop=("token_use",)), id="no-token-use"),
        pytest.param(tenant_token(drop=("client_id",)), id="no-client-id"),
        pytest.param(tenant_token(drop=("sub",)), id="no-sub"),
        pytest.param("not.a.jwt", id="garbage"),
        pytest.param("", id="empty"),
    ],
)
def test_rejects(token: str) -> None:
    with pytest.raises(InvalidToken):
        VERIFIER.verify(token)


def test_rejects_unsigned_tokens() -> None:
    import jwt

    token = jwt.encode(
        {"sub": "x", "iss": TENANT.settings.issuer, "token_use": "access"},
        key="",
        algorithm="none",
    )
    with pytest.raises(InvalidToken):
        VERIFIER.verify(token)


def test_needs_both_pools() -> None:
    with pytest.raises(ValueError, match="exactly one tenant pool"):
        TokenVerifier([TENANT.settings])


def test_jwks_without_a_signing_key_is_refused() -> None:
    issuer: Issuer = make_issuer("tenant")
    empty = type(issuer.settings)(
        kind="tenant", region="us-east-1", pool_id="p", client_id="c", jwks={"keys": []}
    )
    with pytest.raises(ValueError, match="no RS256 signing key"):
        TokenVerifier([empty, PLATFORM.settings])
