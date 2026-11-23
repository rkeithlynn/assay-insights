"""API settings, read from the task's environment (set by infra/assay_infra/api.py).

The API has no route to the internet and Cognito user pools with a domain cannot be reached
through PrivateLink, so each pool's JWKS is read at `pulumi up` and passed in here. A Cognito
signing-key rotation therefore needs a redeploy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from assay_db.connect import DbTarget

PoolKind = Literal["tenant", "platform"]
POOL_KINDS: tuple[PoolKind, ...] = ("tenant", "platform")


@dataclass(frozen=True)
class PoolSettings:
    kind: PoolKind
    region: str
    pool_id: str
    client_id: str
    jwks: Mapping[str, Any]

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.pool_id}"


@dataclass(frozen=True)
class Settings:
    pools: tuple[PoolSettings, ...]
    db: DbTarget

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        region = env["AWS_REGION"]
        pools = tuple(
            PoolSettings(
                kind=kind,
                region=region,
                pool_id=env[f"COGNITO_{kind.upper()}_POOL_ID"],
                client_id=env[f"COGNITO_{kind.upper()}_CLIENT_ID"],
                jwks=json.loads(env[f"COGNITO_{kind.upper()}_JWKS"]),
            )
            for kind in POOL_KINDS
        )
        db = DbTarget(
            host=env["DB_HOST"],
            port=int(env["DB_PORT"]),
            database=env["DB_NAME"],
            region=region,
            ca_bundle=env["DB_CA_BUNDLE"],
        )
        return cls(pools=pools, db=db)
