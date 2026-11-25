"""scripts/ingest_check.py: the EICAR bytes are right but never in the repository; the script
asks before creating anything, keeps to one tenant, and never shows the token."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess  # nosec B404
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.request import Request

import pytest

from assay_schemas import companion

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ingest_check.py"
# The SHA-256 of the 68-byte EICAR test file, as published.
EICAR_SHA256 = "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"
TENANT = uuid.UUID("7f3a91c2-0000-4000-8000-000000000001")
HOST = "portal.example.test"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ingest_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_check"] = module
    spec.loader.exec_module(module)
    return module


def test_the_masked_bytes_are_the_eicar_test_file(script: ModuleType) -> None:
    """Hashed one unmasked byte at a time: the whole string is never held in this process."""
    masked = script.masked_eicar()
    digest = hashlib.sha256()
    for b in masked:
        digest.update(bytes(((b - 1) % 256,)))
    assert len(masked) == 68
    assert digest.hexdigest() == EICAR_SHA256


def _shift(data: bytes) -> bytes:
    return data.translate(bytes((i + 1) % 256 for i in range(256)))


def test_no_file_in_the_repository_holds_the_eicar_string(script: ModuleType) -> None:
    """A file holds the string exactly when its shifted bytes hold the masked form."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    listed = subprocess.run(  # nosec B603
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    masked = script.masked_eicar()
    paths = [ROOT / p.decode() for p in listed.split(b"\0") if p]
    assert len(paths) > 100
    holding = [p for p in paths if p.is_file() and masked in _shift(p.read_bytes())]
    assert holding == []


def test_the_bad_companion_file_has_six_problems_reported_together(script: ModuleType) -> None:
    with pytest.raises(companion.CompanionError) as raised:
        companion.validate(script.bad_companion(TENANT), tenant_id=TENANT)
    assert [d["code"] for d in raised.value.details()] == [
        "tenant_mismatch",
        "must_be_a_date",
        "must_be_a_whole_number",
        "out_of_range",
        "unknown_signer",
        "invalid_key",
    ]


def test_the_small_pdf_is_a_pdf_and_unique(script: ModuleType) -> None:
    a, b = script.small_pdf("a"), script.small_pdf("b")
    assert a.startswith(b"%PDF-") and a.rstrip().endswith(b"%%EOF") and a != b


def _token(minutes: float, use: str = "access") -> str:
    import time

    claims = {"token_use": use, "exp": time.time() + minutes * 60}

    def part(value: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-part"


class FakeApi:
    """The portal's API and S3, as the script would meet them."""

    def __init__(self, me_tenant: uuid.UUID = TENANT, scan: str = "invalid") -> None:
        self.me_tenant = me_tenant
        self.scan = scan
        self.calls: list[tuple[str, str]] = []
        self.files: list[dict[str, Any]] = []

    def send(self, request: Request) -> tuple[int, bytes]:
        method, url = request.get_method(), request.full_url
        self.calls.append((method, url))
        path = url.split(HOST, 1)[-1]

        def ok(body: Any, status: int = 200) -> tuple[int, bytes]:
            return status, json.dumps(body).encode()

        if url.startswith("https://bucket.example.test/"):
            return 200, b""
        if path == "/api/me":
            return ok(
                {
                    "role": "tenant-admin",
                    "mfa_required": False,
                    "tenant": {"id": str(self.me_tenant), "name": "Test Tenant"},
                    "user": {"email": "admin@example.test"},
                }
            )
        if path == "/api/batches" and method == "POST":
            return ok({"id": "b1"}, 201)
        if path == "/api/batches/b1/files":
            names = [f["name"] for f in json.loads(request.data or b"{}")["files"]]  # type: ignore[arg-type]
            self.files = [
                {
                    "id": f"f{i}",
                    "name": n,
                    "kind": "pdf" if n.endswith(".pdf") else "companion",
                    "document_id": f"d{i}",
                }
                for i, n in enumerate(names)
            ]
            return ok(
                {
                    "files": [
                        {
                            "name": n,
                            "upload": {
                                "url": f"https://bucket.example.test/{i}",
                                "method": "PUT",
                                "headers": {},
                            },
                        }
                        for i, n in enumerate(names)
                    ]
                }
            )
        if path == "/api/batches/b1" and method == "GET":
            files = [
                {
                    **f,
                    "status": ("infected" if self.scan == "infected" else "clean")
                    if f["kind"] == "pdf"
                    else self.scan,
                    "error_code": "threats_found" if self.scan == "infected" else None,
                    "errors": [],
                }
                for f in self.files
            ]
            return ok({"files": files, "problems": []})
        if path == "/api/batches/b1/close":
            return ok({"detail": "batch_not_ready"}, 409)
        if path.startswith("/api/documents/"):
            return ok({"status": "quarantined", "error_code": "threats_found"})
        if path == "/api/batches/b1" and method == "DELETE":
            return 204, b""
        return 404, b""


def _run(script: ModuleType, fake: FakeApi, answers: list[str], token: str, argv: list[str]) -> int:
    replies = iter(answers)
    result: int = script.run(
        argv,
        read_token=lambda _prompt: token,
        ask=lambda _prompt: next(replies),
        send=fake.send,
        sleep=lambda _s: None,
    )
    return result


ARGS = ["bad-companion", "--host", HOST, "--tenant", str(TENANT)]


def test_nothing_is_created_without_a_yes(script: ModuleType, capsys: Any) -> None:
    fake = FakeApi()
    assert _run(script, fake, ["no"], _token(30), ARGS) == 0
    assert [m for m, _ in fake.calls] == ["GET"]
    assert "Test Tenant" in capsys.readouterr().out


def test_another_tenants_token_is_refused_before_anything_is_created(script: ModuleType) -> None:
    fake = FakeApi(me_tenant=uuid.uuid4())
    assert _run(script, fake, [], _token(30), ARGS) == 1
    assert [m for m, _ in fake.calls] == ["GET"]


@pytest.mark.parametrize("token", ["not-a-token", "a.b", None])
def test_a_wrong_or_short_lived_token_is_refused_without_a_call(
    script: ModuleType, token: str | None
) -> None:
    fake = FakeApi()
    for bad in (token or _token(5), _token(30, use="id")):
        assert _run(script, fake, [], bad, ARGS) == 2
    assert fake.calls == []


def test_the_bad_companion_case_expects_close_to_be_refused(
    script: ModuleType, capsys: Any
) -> None:
    token = _token(30)
    fake = FakeApi()
    assert _run(script, fake, ["yes", "yes"], token, ARGS) == 0
    assert ("POST", f"https://{HOST}/api/batches/b1/close") in fake.calls
    assert ("DELETE", f"https://{HOST}/api/batches/b1") in fake.calls
    out = capsys.readouterr()
    assert token not in out.out + out.err
    assert token.split(".")[1] not in out.out + out.err


def test_the_eicar_case_reports_the_quarantine(script: ModuleType, capsys: Any) -> None:
    token = _token(30)
    fake = FakeApi(scan="infected")
    argv = ["eicar", "--host", HOST, "--tenant", str(TENANT)]
    assert _run(script, fake, ["yes", "no"], token, argv) == 0
    out = capsys.readouterr().out
    assert "As expected: infected, quarantined." in out and token not in out
