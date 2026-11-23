"""The migration Lambda image, built from db/Dockerfile and run as Lambda's non-root user.

Regression test for "could not read root certificate file": `ADD <url>` creates files with
mode 0600, so the RDS CA bundle was unreadable by the user Lambda runs code as. These tests
build the image and, as that user:

- read the bundle, check it is world-readable and not writable, that its checksum is the one
  pinned in the Dockerfile, and that it loads as the Amazon RDS certificate authorities;
- connect with libpq (psycopg, sslmode=verify-full) to a TLS PostgreSQL whose certificate is
  self-signed: with the RDS bundle the handshake must reach verification and be rejected,
  and with the server's own CA it must connect. This shows the bundle is read and that full
  verification is still enforced.

Needs Docker. Skipped when Docker is not available, unless `ASSAY_REQUIRE_IMAGE_TESTS=1`
(set in CI), in which case it fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "db" / "Dockerfile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Lambda runs function code as sbx_user1051 (uid 993, gid 990), never as root.
LAMBDA_USER = "993:990"
NOBODY = "65534:65534"
BUNDLE = "/opt/rds-ca/global-bundle.pem"
PYTHON = "/var/lang/bin/python3"
# A path inside the throwaway PostgreSQL container, not on this machine.
SERVER_CERT = "/tmp/server.crt"  # noqa: S108

REQUIRED = os.environ.get("ASSAY_REQUIRE_IMAGE_TESTS") == "1"
DOCKER = shutil.which("docker")


def _docker_available() -> bool:
    if DOCKER is None:
        return False
    return subprocess.run([DOCKER, "info"], capture_output=True, check=False).returncode == 0


pytestmark = pytest.mark.skipif(
    not REQUIRED and not _docker_available(), reason="Docker is not available"
)


def pinned_bundle_checksum() -> str:
    match = re.search(r"--checksum=sha256:([0-9a-f]{64})", DOCKERFILE.read_text())
    assert match, "db/Dockerfile no longer pins the RDS CA bundle checksum"
    return match.group(1)


def postgres_image() -> str:
    """The digest-pinned PostgreSQL image CI already uses."""
    match = re.search(r"image:\s*(postgres:\S+@sha256:[0-9a-f]{64})", CI_WORKFLOW.read_text())
    assert match, "no digest-pinned postgres image in ci.yml"
    return match.group(1)


def docker(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    if DOCKER is None:
        pytest.fail("ASSAY_REQUIRE_IMAGE_TESTS=1 but the docker CLI is not on PATH")
    result = subprocess.run(
        [DOCKER, *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if check and result.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:2])} failed:\n{result.stdout}\n{result.stderr}")
    return result


def run_python(
    image: str,
    code: str,
    *,
    user: str = LAMBDA_USER,
    network: str | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    args = ["run", "--rm", "--platform", "linux/amd64", "--user", user, "--entrypoint", PYTHON]
    if network:
        args += ["--network", network]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    result = docker(*args, image, "-c", code)
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = f"assay-db-migrate:test-{uuid.uuid4().hex[:8]}"
    docker(
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "--load",
        "--file",
        DOCKERFILE.as_posix(),
        "--tag",
        tag,
        REPO_ROOT.as_posix(),
    )
    try:
        yield tag
    finally:
        docker("rmi", "-f", tag, check=False)


READ_BUNDLE = """
import hashlib, json, os, ssl, stat
path = os.environ["DB_CA_BUNDLE"]
st, parent = os.stat(path), os.stat(os.path.dirname(path))
data = open(path, "rb").read()
cas = ssl.create_default_context(cafile=path).get_ca_certs()
names = [dict(r[0] for r in c["subject"]) for c in cas]
import assay_db.handler  # the function code is readable by this user too
print(json.dumps({
    "uid": os.getuid(),
    "path": path,
    "mode": stat.S_IMODE(st.st_mode),
    "owner": st.st_uid,
    "dir_mode": stat.S_IMODE(parent.st_mode),
    "sha256": hashlib.sha256(data).hexdigest(),
    "pem_certs": data.count(b"-----BEGIN CERTIFICATE-----"),
    "loaded": len(cas),
    "orgs": sorted({n.get("organizationName", "") for n in names}),
    "rds_cas": sum("RDS" in n.get("commonName", "") for n in names),
}))
"""


@pytest.mark.parametrize("user", [LAMBDA_USER, NOBODY])
def test_bundle_is_genuine_and_readable_by_non_root(image: str, user: str) -> None:
    info = run_python(image, READ_BUNDLE, user=user)
    assert info["uid"] == int(user.split(":")[0])
    assert info["path"] == BUNDLE
    assert info["mode"] == 0o444, oct(info["mode"])
    assert info["owner"] == 0
    assert info["dir_mode"] == 0o755, oct(info["dir_mode"])
    assert info["sha256"] == pinned_bundle_checksum()
    assert info["pem_certs"] > 0
    assert info["loaded"] == info["pem_certs"]
    assert info["orgs"] == ["Amazon Web Services, Inc."]
    assert info["rds_cas"] == info["loaded"]


def test_image_config(image: str) -> None:
    config = json.loads(docker("image", "inspect", image).stdout)[0]
    assert config["Architecture"] == "amd64"
    assert f"DB_CA_BUNDLE={BUNDLE}" in config["Config"]["Env"]
    assert config["Config"]["Cmd"] == ["assay_db.handler.handler"]
    assert config["Config"].get("User", "") in ("", "root")  # Lambda picks the user, not us


CONNECT = """
import json, os, psycopg
def attempt(rootcert):
    try:
        psycopg.connect(
            host=os.environ["PG_HOST"], port=5432, user="postgres", dbname="postgres",
            sslmode="verify-full", sslrootcert=rootcert, connect_timeout=10,
        ).close()
        return "connected"
    except psycopg.OperationalError as exc:
        return str(exc)
with open("/tmp/test-ca.pem", "w") as f:
    f.write(os.environ["TEST_CA"])
print(json.dumps({
    "rds_bundle": attempt(os.environ["DB_CA_BUNDLE"]),
    "server_ca": attempt("/tmp/test-ca.pem"),
}))
"""


@pytest.fixture
def tls_postgres() -> Iterator[tuple[str, str, str]]:
    """A PostgreSQL that requires TLS, with a fresh self-signed certificate for its name."""
    suffix = uuid.uuid4().hex[:8]
    network, name = f"assay-tls-test-{suffix}", f"pg-tls-{suffix}"
    docker("network", "create", network)
    script = (
        f'openssl req -new -x509 -days 1 -nodes -subj "/CN={name}" '
        f'-addext "subjectAltName=DNS:{name}" '
        "-keyout /tmp/server.key -out /tmp/server.crt 2>/dev/null "
        "&& chown postgres:postgres /tmp/server.key /tmp/server.crt "
        "&& chmod 600 /tmp/server.key "
        "&& exec docker-entrypoint.sh postgres -c ssl=on "
        "-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
    )
    try:
        docker(
            "run", "-d", "--name", name, "--network", network,
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "--entrypoint", "sh", postgres_image(), "-c", script,
        )  # fmt: skip
        for _ in range(60):
            ready = docker(
                "exec", name, "pg_isready", "-h", "localhost", "-U", "postgres", check=False
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("TLS PostgreSQL did not become ready")
        server_ca = docker("exec", name, "cat", SERVER_CERT).stdout
        yield network, name, server_ca
    finally:
        docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


def test_libpq_reads_the_bundle_and_still_verifies(
    image: str, tls_postgres: tuple[str, str, str]
) -> None:
    network, host, server_ca = tls_postgres
    result = run_python(
        image, CONNECT, network=network, env={"PG_HOST": host, "TEST_CA": server_ca}
    )
    # The bundle is read (no "could not read root certificate file"), and a server whose
    # certificate is not signed by an RDS CA is rejected: full verification is on.
    assert "could not read root certificate" not in result["rds_bundle"]
    assert "certificate verify failed" in result["rds_bundle"], result["rds_bundle"]
    # Control: verify-full against the server's own CA succeeds as the same user.
    assert result["server_ca"] == "connected", result["server_ca"]
