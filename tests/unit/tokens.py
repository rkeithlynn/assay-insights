"""Locally signed stand-ins for Cognito access tokens, for the API tests (no AWS calls)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from assay_api.settings import PoolKind, PoolSettings

REGION = "us-east-1"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


@dataclass(frozen=True)
class Issuer:
    settings: PoolSettings
    key: rsa.RSAPrivateKey
    kid: str

    def mint(
        self,
        *,
        sub: str,
        groups: list[str] | None,
        tenant_id: uuid.UUID | str | None = None,
        mfa: object = None,
        token_use: str = "access",
        client_id: str | None = None,
        issuer: str | None = None,
        expires_in: int = 3600,
        key: rsa.RSAPrivateKey | None = None,
        kid: str | None = None,
        algorithm: str = "RS256",
        drop: tuple[str, ...] = (),
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "iss": issuer or self.settings.issuer,
            "client_id": client_id or self.settings.client_id,
            "token_use": token_use,
            "scope": "openid email profile aws.cognito.signin.user.admin",
            "auth_time": now,
            "iat": now,
            "exp": now + expires_in,
            "jti": str(uuid.uuid4()),
            "username": sub,
        }
        if groups is not None:
            claims["cognito:groups"] = groups
        if tenant_id is not None:
            claims["tenant_id"] = str(tenant_id)
        if mfa is not None:
            claims["mfa"] = mfa
        for name in drop:
            claims.pop(name, None)
        signing_key: Any = key or self.key
        if algorithm.startswith("HS"):
            signing_key = "a-forged-hmac-key-of-at-least-32-bytes"
        return jwt.encode(
            claims, signing_key, algorithm=algorithm, headers={"kid": kid or self.kid}
        )


def make_issuer(kind: PoolKind) -> Issuer:
    key = new_key()
    kid = f"{kind}-key-1"
    settings = PoolSettings(
        kind=kind,
        region=REGION,
        pool_id=f"{REGION}_{kind.capitalize()}Pool",
        client_id=f"{kind}-client-id",
        jwks={"keys": [public_jwk(key, kid)]},
    )
    return Issuer(settings=settings, key=key, kid=kid)
