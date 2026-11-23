"""The API refuses to start without usable settings, an empty JWKS above all."""

from __future__ import annotations

import json
from typing import Any

import pytest
import uvicorn

from assay_api import __main__ as entry
from assay_api.settings import Settings, SettingsError
from tests.unit.tokens import make_issuer


def environment(**overrides: str) -> dict[str, str]:
    env = {
        "AWS_REGION": "us-east-1",
        "DB_HOST": "db.invalid",
        "DB_PORT": "5432",
        "DB_NAME": "assay",
        "DB_CA_BUNDLE": "/opt/rds-ca/global-bundle.pem",
    }
    for kind in ("tenant", "platform"):
        settings = make_issuer(kind).settings
        env[f"COGNITO_{kind.upper()}_POOL_ID"] = settings.pool_id
        env[f"COGNITO_{kind.upper()}_CLIENT_ID"] = settings.client_id
        env[f"COGNITO_{kind.upper()}_JWKS"] = json.dumps(settings.jwks)
    env.update(overrides)
    return env


def test_complete_settings_load() -> None:
    settings = Settings.from_env(environment())
    assert [p.kind for p in settings.pools] == ["tenant", "platform"]
    assert all(p.jwks["keys"] for p in settings.pools)


@pytest.mark.parametrize("value", ["", "   ", "not json", "{}", '{"keys": []}', "[]", "null"])
@pytest.mark.parametrize("name", ["COGNITO_TENANT_JWKS", "COGNITO_PLATFORM_JWKS"])
def test_empty_or_unusable_jwks_is_refused(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        Settings.from_env(environment(**{name: value}))


def test_missing_jwks_is_refused() -> None:
    env = environment()
    del env["COGNITO_TENANT_JWKS"]
    with pytest.raises(SettingsError, match="COGNITO_TENANT_JWKS"):
        Settings.from_env(env)


@pytest.mark.parametrize(
    "overrides",
    [
        {"COGNITO_TENANT_JWKS": ""},
        {"COGNITO_PLATFORM_JWKS": '{"keys": []}'},
        # Keys present, but none an RS256 signing key: the verifier refuses them.
        {"COGNITO_TENANT_JWKS": '{"keys": [{"kid": "k", "kty": "EC", "use": "sig"}]}'},
    ],
)
def test_the_server_never_starts_without_signing_keys(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    for key, value in environment(**overrides).items():
        monkeypatch.setenv(key, value)
    started: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(a))
    assert entry.main() == entry.EXIT_BAD_SETTINGS
    assert started == []


def test_the_server_starts_with_good_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    started: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(a))
    assert entry.main() == 0
    assert len(started) == 1
