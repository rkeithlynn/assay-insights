"""The API image (api/Dockerfile) and the pre-token trigger image (api/pre-token.Dockerfile).

Builds both for linux/amd64 and checks, as the users they run as:

- the API starts with a read-only root filesystem and every capability dropped, as its
  unprivileged user (uid 10001), answers the health check, refuses GET /me without a
  token, and can read the RDS CA bundle (0444) for TLS verify-full;
- the trigger imports as Lambda's non-root user (uid 993) and ships no API dependencies.

Needs Docker. Skipped when Docker is not available, unless `ASSAY_REQUIRE_IMAGE_TESTS=1`
(set in CI), in which case it fails.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.test_migrate_image import REQUIRED, _docker_available, docker
from tests.unit.tokens import make_issuer

REPO_ROOT = Path(__file__).resolve().parents[2]
API_USER = "10001"
LAMBDA_USER = "993:990"
BUNDLE = "/opt/rds-ca/global-bundle.pem"

pytestmark = pytest.mark.skipif(
    not REQUIRED and not _docker_available(), reason="Docker is not available"
)


def _build(dockerfile: str) -> Iterator[str]:
    tag = f"assay-test-{uuid.uuid4().hex[:12]}"
    docker(
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "-f",
        str(REPO_ROOT / dockerfile),
        "-t",
        tag,
        str(REPO_ROOT),
    )
    try:
        yield tag
    finally:
        docker("rmi", "-f", tag, check=False)


@pytest.fixture(scope="module")
def api_image() -> Iterator[str]:
    yield from _build("api/Dockerfile")


@pytest.fixture(scope="module")
def trigger_image() -> Iterator[str]:
    yield from _build("api/pre-token.Dockerfile")


def _environment() -> dict[str, str]:
    env = {
        "AWS_REGION": "us-east-1",
        "DB_HOST": "db.invalid",
        "DB_PORT": "5432",
        "DB_NAME": "assay",
    }
    for kind in ("tenant", "platform"):
        settings = make_issuer(kind).settings
        env[f"COGNITO_{kind.upper()}_POOL_ID"] = settings.pool_id
        env[f"COGNITO_{kind.upper()}_CLIENT_ID"] = settings.client_id
        env[f"COGNITO_{kind.upper()}_JWKS"] = json.dumps(settings.jwks)
    return env


@pytest.fixture(scope="module")
def api_container(api_image: str) -> Iterator[str]:
    name = f"assay-api-test-{uuid.uuid4().hex[:8]}"
    args = ["run", "-d", "--name", name, "--read-only", "--cap-drop", "ALL"]
    for key, value in _environment().items():
        args += ["-e", f"{key}={value}"]
    docker(*args, api_image)
    try:
        yield name
    finally:
        docker("rm", "-f", name, check=False)


def _http(container: str, path: str) -> tuple[int, str]:
    script = (
        "import sys, urllib.request, urllib.error\n"
        "try:\n"
        f"    r = urllib.request.urlopen('http://127.0.0.1:8000{path}', timeout=5)\n"
        "    print(r.status, r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code, e.read().decode())\n"
    )
    out = docker("exec", container, "python", "-c", script, check=False)
    status, _, body = out.stdout.strip().partition(" ")
    return (int(status) if status.isdigit() else 0), body


def _wait_until_healthy(container: str) -> tuple[int, str]:
    result = (0, "")
    for _ in range(30):
        result = _http(container, "/api/health")
        if result[0] == 200:
            return result
        time.sleep(1)
    logs = docker("logs", container, check=False)
    pytest.fail(f"API never became healthy: {result}\n{logs.stdout}\n{logs.stderr}")


def test_image_is_linux_amd64(api_image: str, trigger_image: str) -> None:
    for image in (api_image, trigger_image):
        out = docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image)
        assert out.stdout.strip() == "linux/amd64"


def test_api_runs_unprivileged_on_a_read_only_filesystem(api_container: str) -> None:
    assert _wait_until_healthy(api_container) == (200, '{"status":"ok"}')
    assert docker("exec", api_container, "id", "-u").stdout.strip() == API_USER
    write = docker("exec", api_container, "touch", "/app/x", check=False)
    assert write.returncode != 0


def test_api_refuses_me_without_a_token(api_container: str) -> None:
    _wait_until_healthy(api_container)
    status, body = _http(api_container, "/api/me")
    assert status == 401
    assert json.loads(body) == {"detail": "invalid_token"}


def test_api_logs_are_json(api_container: str) -> None:
    _wait_until_healthy(api_container)
    lines = [line for line in docker("logs", api_container).stdout.splitlines() if line]
    assert lines
    for line in lines:
        entry = json.loads(line)
        assert {"time", "level", "logger", "message"} <= set(entry)


def test_api_can_read_the_rds_ca_bundle(api_container: str) -> None:
    script = (
        "import os, stat, ssl\n"
        f"p = '{BUNDLE}'\n"
        "print(oct(stat.S_IMODE(os.stat(p).st_mode)))\n"
        "ssl.create_default_context(cafile=p)\n"
        "print('loaded')\n"
    )
    out = docker("exec", api_container, "python", "-c", script)
    assert out.stdout.split() == ["0o444", "loaded"]


@pytest.mark.parametrize("jwks", ["", '{"keys": []}'])
def test_api_exits_without_signing_keys(api_image: str, jwks: str) -> None:
    """A task given an empty JWKS stops at once and never answers the health check."""
    args = ["run", "--rm", "--read-only", "--cap-drop", "ALL"]
    for key, value in {**_environment(), "COGNITO_TENANT_JWKS": jwks}.items():
        args += ["-e", f"{key}={value}"]
    result = docker(*args, api_image, check=False, timeout=120)
    assert result.returncode == 1, result.stdout + result.stderr
    [entry] = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert entry["message"] == "API settings unusable; not starting"
    assert "COGNITO_TENANT_JWKS" in entry["error"]


def test_trigger_imports_as_lambdas_user_without_api_dependencies(trigger_image: str) -> None:
    script = (
        "import importlib.util, assay_api.cognito_pre_token as t\n"
        "print(t.EVENT_VERSION)\n"
        "absent = importlib.util.find_spec\n"
        "print(absent('fastapi') is None, absent('jwt') is None)\n"
    )
    out = docker(
        "run",
        "--rm",
        "--user",
        LAMBDA_USER,
        "--entrypoint",
        "/var/lang/bin/python3",
        trigger_image,
        "-c",
        script,
    )
    assert out.stdout.split() == ["2", "True", "True"]
