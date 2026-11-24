"""Write synthetic test files for build step 6's browser checks (BUILD_SPEC.md section 13).

Usage (PowerShell, from the repository root):

    uv run scripts/make_test_files.py
    uv run scripts/make_test_files.py --tenant-id <tenant id> --out .test-files

Every file is synthetic: made-up names, no real person, nothing from a real loan. The PDFs
are written by hand (no PDF library), so the script needs nothing beyond Python.

    01-normal.pdf                    a plain three-page loan package
    02-normal-again.pdf              the same bytes, another name: the duplicate check
    03-password-protected.pdf        needs a password to open (on_hold, pdf_encrypted)
    04-with-companion.pdf/.json      a PDF and a valid companion file
    05-invalid-companion.pdf/.json   a PDF and a companion file that blocks the batch

With `--tenant-id` (the tenant you upload as), the valid companion carries that `TenantID`;
the invalid one always carries another tenant's ID, a text loan term and a nested value.

The EICAR test file is not written here: see the build step 6 notes in CLAUDE.md for how to
make it on a machine with Windows Defender. The output directory `.test-files/` is ignored by
git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / ".test-files"
# The user password of the protected PDF, for anyone who wants to open it by hand.
PASSWORD = "assay-test"  # noqa: S105  # nosec B105  (a test file's published password)

# The 32-byte padding string of the PDF standard security handler (ISO 32000-1, 7.6.3.3).
PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
# Permissions: print and copy allowed (the value is not what is tested).
PERMISSIONS = -44
KEY_BYTES = 5  # 40-bit RC4, revision 2


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4, as the PDF standard security handler (revision 2) uses it. Not for security."""
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) % 256
        box[i], box[j] = box[j], box[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(byte ^ box[(box[i] + box[j]) % 256])
    return bytes(out)


def _padded(password: str) -> bytes:
    return (password.encode("latin-1") + PAD)[:32]


def _md5(data: bytes) -> bytes:
    # The PDF format defines its key derivation with MD5; this is not used for security.
    return hashlib.md5(data, usedforsecurity=False).digest()


class Encryption:
    """Standard security handler, revision 2: the O and U entries and per-object keys."""

    def __init__(self, user_password: str, file_id: bytes) -> None:
        owner_key = _md5(_padded(user_password))[:KEY_BYTES]
        self.owner = rc4(owner_key, _padded(user_password))
        p = PERMISSIONS.to_bytes(4, "little", signed=True)
        self.key = _md5(_padded(user_password) + self.owner + p + file_id)[:KEY_BYTES]
        self.user = rc4(self.key, PAD)

    def object_key(self, number: int, generation: int = 0) -> bytes:
        material = self.key + number.to_bytes(3, "little") + generation.to_bytes(2, "little")
        return _md5(material)[: KEY_BYTES + 5]

    def encrypt(self, number: int, data: bytes) -> bytes:
        return rc4(self.object_key(number), data)


def _text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(pages: list[list[str]], *, password: str | None = None, seed: str = "") -> bytes:
    """A small PDF: one page per list of lines, in Helvetica. `seed` makes the bytes unique."""
    file_id = hashlib.sha256(f"assay-test:{seed}".encode()).digest()[:16]
    encryption = Encryption(password, file_id) if password is not None else None
    objects: dict[int, bytes] = {}
    # 1 catalog, 2 pages, 3 font; then a page and its contents per page.
    kids = []
    for index, lines in enumerate(pages):
        page_number = 4 + 2 * index
        content_number = page_number + 1
        kids.append(f"{page_number} 0 R")
        ops = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"]
        for line in lines:
            ops.append(f"({_text(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        if encryption is not None:
            stream = encryption.encrypt(content_number, stream)
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
        ).encode()
        objects[content_number] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    trailer_extra = ""
    if encryption is not None:
        number = max(objects) + 1
        objects[number] = (
            f"<< /Filter /Standard /V 1 /R 2 /O <{encryption.owner.hex()}> "
            f"/U <{encryption.user.hex()}> /P {PERMISSIONS} >>"
        ).encode()
        trailer_extra = f" /Encrypt {number} 0 R"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]"
        f"{trailer_extra} >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def package(title: str, applicant: str, loan: str) -> list[list[str]]:
    """Three pages of an obviously synthetic loan package."""
    note = "SYNTHETIC TEST DOCUMENT - not a real application. Assay Insights build checks."
    return [
        [
            note,
            "",
            f"UNIFORM RESIDENTIAL LOAN APPLICATION - {title}",
            f"Borrower: {applicant}",
            "Date of birth: 1980-01-01   Phone: +1 404 555 0100",
            f"Loan number: {loan}   Amount: $250,000.00   Term: 360 months",
            "Property: 100 Example Street, Atlanta, GA 30301",
        ],
        [
            note,
            "",
            "PAYSTUB - Example Employer, Inc.",
            f"Employee: {applicant}   Pay period: 2026-10-01 to 2026-10-15",
            "Gross pay: $4,000.00   Deductions: $1,000.00   Net pay: $3,000.00",
        ],
        [
            note,
            "",
            "CERTIFICATE OF COMPLETION (e-signature audit trail)",
            f"Envelope ID: {uuid.uuid5(uuid.NAMESPACE_URL, loan)}",
            f"Signer: {applicant} <signer@example.test>   Signed: 2026-11-02 14:05 UTC",
            "IP address: 198.51.100.7   Authentication: email",
        ],
    ]


def files(tenant_id: uuid.UUID | None) -> dict[str, bytes]:
    normal = pdf(package("Package 001", "Jane Testperson", "TEST-001"), seed="normal")
    valid_companion = {
        **({"TenantID": str(tenant_id)} if tenant_id else {}),
        "ApplicationId": "APP-TEST-004",
        "LoanNumber": "TEST-004",
        "CreateDate": "2026-10-28",
        "CompleteDate": "2026-11-02",
        "Signer1Name": "Sam Testperson",
        "Signer1Email": "sam@example.test",
        "LoanTerm": 360,
        "LoanInterestRate": 6.125,
        "Branch": "Atlanta",
    }
    invalid_companion = {
        # Another tenant's ID, a text loan term and a nested value: three reasons to refuse.
        "TenantID": str(uuid.UUID("00000000-0000-4000-8000-00000000dead")),
        "LoanTerm": "thirty years",
        "Notes": {"nested": True},
    }
    return {
        "01-normal.pdf": normal,
        "02-normal-again.pdf": normal,
        "03-password-protected.pdf": pdf(
            package("Package 003", "Pat Testperson", "TEST-003"), password=PASSWORD, seed="locked"
        ),
        "04-with-companion.pdf": pdf(
            package("Package 004", "Sam Testperson", "TEST-004"), seed="companion"
        ),
        "04-with-companion.json": json.dumps(valid_companion, indent=2).encode(),
        "05-invalid-companion.pdf": pdf(
            package("Package 005", "Lee Testperson", "TEST-005"), seed="invalid"
        ),
        "05-invalid-companion.json": json.dumps(invalid_companion, indent=2).encode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=None)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, body in files(args.tenant_id).items():
        (args.out / name).write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()[:16]
        print(f"{args.out / name}  {len(body)} bytes  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
