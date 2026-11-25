#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Exercise the ingestion pipeline in AWS through the portal's own API (build step 6).

Usage, from the repository root or in AWS CloudShell (Python's standard library only):

    uv run scripts/ingest_check.py bad-companion --host <tenant portal host> --tenant <tenant id>
    python3 ingest_check.py eicar --host <tenant portal host> --tenant <tenant id>

Cases:

- `bad-companion`: a small PDF and a companion file with several errors, made in memory. It
  waits for both scans, then asks the API to close the batch, which must refuse while the
  companion file is invalid (`batch_not_ready`), and prints every error the pipeline found.
  Then it offers to discard the batch.
- `eicar`: the EICAR anti-malware test file, assembled in memory at run time and registered as
  `eicar-test.pdf`. GuardDuty should find it, and the pipeline should quarantine it. The script
  prints the file's and the document's status, then offers to close the batch.

It signs in as you: paste the access token of your signed-in portal tab (a tenant admin) at
the hidden prompt. The token is never echoed, logged, written to disk or passed on a command
line. The script calls the same routes the portal calls (`/api/me`, `/api/batches`, the
presigned upload links, `/api/documents`), through the same CloudFront host, so nothing in
production code knows about it. It refuses unless the token's tenant is the one named with
`--tenant`, prints the tenant's name, and asks you to confirm before it creates anything.

Nothing is written to disk. The repository never holds the EICAR string: only a masked form
(each byte plus one, as hex), unmasked in memory when the `eicar` case runs.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

USER_AGENT = "assay-ingest-check/1"
POLL_SECONDS = 10
POLL_LIMIT_SECONDS = 15 * 60
MIN_TOKEN_MINUTES = 10
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
HOST_SHAPE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")

# The EICAR test file, each byte plus one (mod 256), as hex. Never stored unmasked.
MASKED_EICAR_HEX = (
    "5936502251264142515c355d515b59363529515f2a3844442a387e25",
    "464a4442532e5455424f454253452e424f554a574a5356542e554654552e474a4d4622",
    "25492c492b",
)


def masked_eicar() -> bytes:
    return bytes.fromhex("".join(MASKED_EICAR_HEX))


def eicar_bytes() -> bytes:
    """The test file, in memory only, for the upload."""
    return bytes((b - 1) % 256 for b in masked_eicar())


def bad_companion(tenant_id: uuid.UUID) -> bytes:
    """A flat JSON object with six problems the pipeline must report together."""
    other = uuid.uuid4()
    while other == tenant_id:
        other = uuid.uuid4()
    return json.dumps(
        {
            "TenantID": str(other),
            "CreateDate": "24/11/2026",
            "LoanTerm": "sixty",
            "LoanInterestRate": 101,
            "Signer4Name": "Test Signer",
            "9lives": "x",
            "Branch": "ingest-check",
        }
    ).encode()


def small_pdf(label: str) -> bytes:
    """A one-page PDF whose text makes its bytes unique, so it is never a duplicate."""
    text = f"ingest-check {label}".replace("\\", "").replace("(", "").replace(")", "")
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def token_minutes_left(token: str, now: float) -> float:
    """Minutes until the access token expires, read from its payload (not verified here;
    the API verifies it). Raises ValueError if it is not an access token."""
    if not TOKEN_SHAPE.match(token):
        raise ValueError("that is not a token (three dot-separated parts)")
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("token_use") != "access":
        raise ValueError("that is not an access token (use access_token, not id_token)")
    return (float(claims.get("exp", 0)) - now) / 60


Send = Callable[[urllib.request.Request], "tuple[int, bytes]"]


def urlopen_send(request: urllib.request.Request) -> tuple[int, bytes]:
    if not request.full_url.startswith("https://"):
        raise ValueError("only https")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # nosec B310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


class Api:
    """The portal's API, as the portal calls it. The token goes only into the header."""

    def __init__(self, host: str, token: str, send: Send = urlopen_send) -> None:
        self._base = f"https://{host}"
        self._token = token
        self._send = send

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310
            self._base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        status, raw = self._send(request)
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None


def put_file(upload: dict[str, Any], data: bytes, send: Send = urlopen_send) -> tuple[int, str]:
    """PUT through the presigned link with exactly the headers the API signed."""
    request = urllib.request.Request(  # noqa: S310
        upload["url"],
        data=data,
        method=upload.get("method", "PUT"),
        headers={**upload["headers"], "User-Agent": USER_AGENT},
    )
    status, raw = send(request)
    code = re.search(rb"<Code>([^<]{1,64})</Code>", raw or b"")
    return status, code.group(1).decode() if code else ""


def detail(body: Any) -> str:
    return str(body.get("detail")) if isinstance(body, dict) else ""


def show_files(batch: dict[str, Any]) -> None:
    for f in batch.get("files", []):
        line = f"  {f['name']}: {f['status']}"
        if f.get("error_code"):
            line += f" ({f['error_code']})"
        print(line)
        for e in f.get("errors") or []:
            print(f"      - {e.get('key') or 'file'}: {e.get('code')}")
    for p in batch.get("problems", []):
        print(f"  before it can close: {p.get('code')}")


def run(
    argv: list[str],
    *,
    read_token: Callable[[str], str] = getpass.getpass,
    ask: Callable[[str], str] = input,
    send: Send = urlopen_send,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("case", choices=["bad-companion", "eicar"])
    parser.add_argument("--host", required=True, help="the tenant portal's host name")
    parser.add_argument("--tenant", required=True, type=uuid.UUID, help="the test tenant's ID")
    args = parser.parse_args(argv)
    host = args.host.strip().lower()
    if not HOST_SHAPE.match(host):
        print("--host must be a host name, such as the tenant portal's (no https://).")
        return 2

    token = read_token("Access token from your portal tab (input hidden): ").strip()
    try:
        left = token_minutes_left(token, now())
    except ValueError as exc:
        print(f"Not used: {exc}.")
        return 2
    if left < MIN_TOKEN_MINUTES:
        print(f"The token expires in {max(left, 0):.0f} minutes. Copy a fresh one and run again.")
        return 2
    api = Api(host, token, send)

    status, me = api.call("GET", "/api/me")
    if status != 200 or not isinstance(me, dict):
        print(f"GET /api/me refused: {status} {detail(me)}")
        return 1
    tenant = me.get("tenant") or {}
    print(f"Tenant: {tenant.get('name')} ({tenant.get('id')})")
    print(f"Signed in as {me.get('user', {}).get('email')} ({me.get('role')}).")
    if str(tenant.get("id")) != str(args.tenant):
        print("That is not the tenant named with --tenant. Nothing was created.")
        return 1
    if me.get("role") != "tenant-admin" or me.get("mfa_required"):
        print("Uploading needs a tenant admin with MFA set up. Nothing was created.")
        return 1
    answer = ask(f"Create a test batch in {tenant.get('name')} for '{args.case}'? Type yes: ")
    if answer.strip().lower() != "yes":
        print("Nothing was created.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")  # noqa: UP017 (Python 3.9)
    status, batch = api.call("POST", "/api/batches", {"name": f"ingest-check {args.case} {stamp}"})
    if status == 409:
        print("You already have an open batch. Close or discard it in the portal, then run again.")
        return 1
    if status != 201 and status != 200:
        print(f"Opening a batch was refused: {status} {detail(batch)}")
        return 1
    batch_id = batch["id"]
    print(f"Batch {batch_id} opened.")

    files: dict[str, bytes]
    if args.case == "bad-companion":
        label = uuid.uuid4().hex[:12]
        files = {
            f"ingest-check-{label}.pdf": small_pdf(label),
            f"ingest-check-{label}.json": bad_companion(args.tenant),
        }
    else:
        files = {"eicar-test.pdf": eicar_bytes()}

    status, registered = api.call(
        "POST",
        f"/api/batches/{batch_id}/files",
        {
            "files": [
                {"name": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in files.items()
            ]
        },
    )
    if status not in (200, 201) or not isinstance(registered, dict):
        print(f"Registering the files was refused: {status} {detail(registered)}")
        return 1
    for entry in registered["files"]:
        put_status, code = put_file(entry["upload"], files[entry["name"]], send)
        print(f"  {entry['name']}: uploaded ({put_status}{' ' + code if code else ''})")
        if put_status not in (200, 204):
            print("The upload was refused; the batch stays open. Discard it in the portal.")
            return 1

    print("Waiting for the malware scans ...")
    waited = 0.0
    while True:
        status, current = api.call("GET", f"/api/batches/{batch_id}")
        if status != 200 or not isinstance(current, dict):
            print(f"Reading the batch failed: {status} {detail(current)}")
            return 1
        if not any(f["status"] == "registered" for f in current["files"]):
            break
        if waited >= POLL_LIMIT_SECONDS:
            print("No scan result after 15 minutes. The sweeper holds the file after 30.")
            show_files(current)
            return 1
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    show_files(current)

    if args.case == "bad-companion":
        status, closed = api.call("POST", f"/api/batches/{batch_id}/close")
        print(f"Close: {status} {detail(closed)} (expected: 409 batch_not_ready)")
        result = 0 if status == 409 and detail(closed) == "batch_not_ready" else 1
        if ask("Discard the test batch now? Type yes: ").strip().lower() == "yes":
            status, _ = api.call("DELETE", f"/api/batches/{batch_id}")
            print(f"Discard: {status}")
        return result

    pdf = next(f for f in current["files"] if f["kind"] == "pdf")
    document_id = pdf.get("document_id")
    status, document = api.call("GET", f"/api/documents/{document_id}")
    if status == 200 and isinstance(document, dict):
        print(f"Document {document_id}: {document.get('status')} ({document.get('error_code')})")
    expected = pdf["status"] == "infected" and pdf.get("error_code") == "threats_found"
    print("As expected: infected, quarantined." if expected else "NOT as expected: see above.")
    if ask("Close the batch now? Type yes: ").strip().lower() == "yes":
        status, closed = api.call("POST", f"/api/batches/{batch_id}/close")
        print(f"Close: {status} {detail(closed)}")
    return 0 if expected else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
