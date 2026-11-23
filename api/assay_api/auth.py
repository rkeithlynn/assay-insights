"""Cognito access-token verification (BUILD_SPEC.md sections 4 and 10).

A token is accepted only if all of these hold:

- it is an RS256 JWT signed by a key in its pool's JWKS (read at deploy time);
- `iss` is exactly one of the two user pools, `token_use` is `access` and `client_id` is
  that pool's app client (access tokens carry no `aud`);
- it has not expired (`exp`, `iat` and `nbf` are checked with a small leeway);
- `cognito:groups` holds exactly one group, and it is one of that pool's groups;
- for the tenant pool, `tenant_id` is a UUID. It was copied by the pre-token trigger from
  the user's immutable `custom:tenant_id`, and is the only source of the tenant ID.

Whether the user still exists, is enabled and belongs to an active tenant is checked
against the database on every request (`assay_api.accounts`), because a token lives for
60 minutes after a user is disabled or a tenant suspended.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from assay_api.claims import MFA_CLAIM, PLATFORM_GROUPS, TENANT_GROUPS, TENANT_ID_CLAIM
from assay_api.settings import PoolKind, PoolSettings

log = logging.getLogger(__name__)

ALGORITHM = "RS256"
LEEWAY_SECONDS = 30
REQUIRED_CLAIMS = ("exp", "iat", "iss", "sub", "token_use", "client_id")
GROUPS_CLAIM = "cognito:groups"
POOL_GROUPS: Mapping[PoolKind, tuple[str, ...]] = {
    "tenant": TENANT_GROUPS,
    "platform": PLATFORM_GROUPS,
}


class InvalidToken(Exception):
    """The token is not acceptable. The reason is logged, never returned to the caller."""


@dataclass(frozen=True)
class Principal:
    """Who the verified token says the caller is. Not yet checked against the database."""

    pool: PoolKind
    sub: str
    role: str
    # Tenant pool: from the token's tenant_id claim. Platform pool: None (the platform
    # tenant is found through the platform users view).
    tenant_id: uuid.UUID | None
    # Platform pool: always true, because the pool requires MFA. Tenant pool: the `mfa` claim.
    mfa: bool


@dataclass(frozen=True)
class _Pool:
    settings: PoolSettings
    keys: Mapping[str, Any]


def _keys(jwks: Mapping[str, Any]) -> dict[str, Any]:
    keys: dict[str, Any] = {}
    for jwk in jwks.get("keys", []):
        if jwk.get("kty") != "RSA" or jwk.get("alg", ALGORITHM) != ALGORITHM:
            continue
        if jwk.get("use", "sig") != "sig" or not jwk.get("kid"):
            continue
        keys[jwk["kid"]] = jwt.PyJWK(dict(jwk), algorithm=ALGORITHM).key
    if not keys:
        raise ValueError("JWKS holds no RS256 signing key")
    return keys


class TokenVerifier:
    def __init__(self, pools: Iterable[PoolSettings]) -> None:
        self._pools = {p.issuer: _Pool(settings=p, keys=_keys(p.jwks)) for p in pools}
        if {p.settings.kind for p in self._pools.values()} != set(POOL_GROUPS):
            raise ValueError("need exactly one tenant pool and one platform pool")

    def verify(self, token: str) -> Principal:
        try:
            return self._verify(token)
        except InvalidToken as exc:
            log.info("token rejected", extra={"reason": str(exc)})
            raise
        except jwt.PyJWTError as exc:
            log.info("token rejected", extra={"reason": type(exc).__name__})
            raise InvalidToken(type(exc).__name__) from None

    def _verify(self, token: str) -> Principal:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("algorithm not allowed")
        # Read without verification only to pick the pool; every claim is verified below.
        unverified = jwt.decode(token, options={"verify_signature": False})
        pool = self._pools.get(unverified.get("iss", ""))
        if pool is None:
            raise InvalidToken("unknown issuer")
        key = pool.keys.get(header.get("kid", ""))
        if key is None:
            raise InvalidToken("unknown signing key")

        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=[ALGORITHM],
            issuer=pool.settings.issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": list(REQUIRED_CLAIMS), "verify_aud": False},
        )
        if claims["token_use"] != "access":  # noqa: S105  # a claim value, not a secret
            raise InvalidToken("not an access token")
        if claims["client_id"] != pool.settings.client_id:
            raise InvalidToken("wrong app client")

        kind = pool.settings.kind
        groups = claims.get(GROUPS_CLAIM)
        if not isinstance(groups, list) or len(groups) != 1:
            raise InvalidToken("token must carry exactly one group")
        role = groups[0]
        if role not in POOL_GROUPS[kind]:
            raise InvalidToken("group does not belong to this pool")

        if kind == "platform":
            return Principal(pool=kind, sub=claims["sub"], role=role, tenant_id=None, mfa=True)

        try:
            tenant_id = uuid.UUID(str(claims[TENANT_ID_CLAIM]))
        except KeyError, ValueError:
            raise InvalidToken("tenant token without a valid tenant_id") from None
        return Principal(
            pool=kind,
            sub=claims["sub"],
            role=role,
            tenant_id=tenant_id,
            mfa=claims.get(MFA_CLAIM) is True,
        )
