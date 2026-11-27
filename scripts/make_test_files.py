"""Write synthetic test files for build step 6's browser checks (BUILD_SPEC.md section 13).

Usage (PowerShell, from the repository root):

    uv run scripts/make_test_files.py
    uv run scripts/make_test_files.py --tenant-id <tenant id> --out .test-files
    uv run scripts/make_test_files.py --fresh

Every file is synthetic: made-up names, no real person, nothing from a real loan. The PDFs
are written by hand (no PDF library), so the script needs nothing beyond Python.

    01-normal.pdf                    a plain three-page loan package
    02-normal-again.pdf              the same bytes, another name: the duplicate check
    03-password-protected.pdf        needs a password to open (40-bit RC4)
    04-with-companion.pdf/.json      a PDF and a valid companion file
    05-invalid-companion.pdf/.json   a PDF and a companion file that blocks the batch
    06-not-a-pdf.pdf                 plain text named .pdf (failed, not_a_pdf)
    07-password-protected-128.pdf    needs a password to open (128-bit RC4)
    08-scanned.pdf                   three pages that are pictures only: no text layer
    09-mixed.pdf                     a typed page, a typed page with a pasted picture, and a
                                     scan with a typed header stamped on it
    10-filled-form.pdf               a typed form whose answers are in form fields

    uv run scripts/make_test_files.py --fresh --pages-1000

also writes `11-scanned-1000-pages.pdf`: 1,000 pages of pictures, to time one run of the text
step against the worker task's 30-minute limit. Every one of its pages goes to Textract, so
one upload of it costs 1,000 Textract pages.

Files 08 to 11 are for the text step (build step 6, second half, stage 2). A page that is a
picture has no text layer, so Textract reads it. Their "scans" are drawn here, from a small
dot-matrix letter table in this file, in capitals: no image library, and no real document.

With `--tenant-id` (the tenant you upload as), the valid companion carries that `TenantID`;
the invalid one always carries another tenant's ID, a text loan term and a nested value.

Without `--fresh` the files are the same bytes on every run, so a tenant that already holds
one sees the next upload stop as a duplicate. `--fresh` adds a line with a new run ID to
every page of every PDF (and prints the ID), so each run's PDFs are new documents. Within one
run, 02 is still byte for byte 01, so the duplicate check still shows.

The two password-protected files differ only in their encryption. GuardDuty holds a
password-protected file at the malware scan when it recognises one (`scan_unsupported`),
before the pipeline's own check. AWS does not document how it recognises one, so two kinds
are here to try. One that GuardDuty passes is held by the worker's inspection step instead
(`on_hold`, `pdf_encrypted`).

The EICAR test file is not written here: see the build step 6 notes in CLAUDE.md for how to
make it on a machine with Windows Defender. The output directory `.test-files/` is ignored by
git.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import secrets
import sys
import uuid
import zlib
from pathlib import Path
from typing import Any

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


def _rc4_rounds(key: bytes, data: bytes) -> bytes:
    """Revision 3: RC4 twenty times, the key XORed with the round number (ISO 32000-1,
    algorithms 3 and 5)."""
    for i in range(20):
        data = rc4(bytes(b ^ i for b in key), data)
    return data


class Encryption:
    """The standard security handler's O and U entries and per-object keys (ISO 32000-1,
    7.6.3): revision 2 with a 40-bit key, or revision 3 with a 128-bit key. Both RC4."""

    def __init__(self, user_password: str, file_id: bytes, *, bits: int = 40) -> None:
        if bits not in (40, 128):
            raise ValueError("40 or 128 bits")
        self.bits = bits
        self.revision = 2 if bits == 40 else 3
        size = bits // 8
        p = PERMISSIONS.to_bytes(4, "little", signed=True)
        owner_hash = _md5(_padded(user_password))
        key_hash = None
        if self.revision == 2:
            self.owner = rc4(owner_hash[:size], _padded(user_password))
            key_hash = _md5(_padded(user_password) + self.owner + p + file_id)
            self.key = key_hash[:size]
            self.user = rc4(self.key, PAD)
        else:
            for _ in range(50):
                owner_hash = _md5(owner_hash)
            self.owner = _rc4_rounds(owner_hash[:size], _padded(user_password))
            key_hash = _md5(_padded(user_password) + self.owner + p + file_id)
            for _ in range(50):
                key_hash = _md5(key_hash[:size])
            self.key = key_hash[:size]
            # Sixteen bytes that matter, padded to 32 with anything (zeros here).
            self.user = _rc4_rounds(self.key, _md5(PAD + file_id)) + bytes(16)

    def dictionary(self) -> str:
        """The encryption dictionary, as the trailer's /Encrypt entry points at it."""
        version = "/V 1 /R 2" if self.revision == 2 else f"/V 2 /R 3 /Length {self.bits}"
        return (
            f"<< /Filter /Standard {version} /O <{self.owner.hex()}> "
            f"/U <{self.user.hex()}> /P {PERMISSIONS} >>"
        )

    def object_key(self, number: int, generation: int = 0) -> bytes:
        material = self.key + number.to_bytes(3, "little") + generation.to_bytes(2, "little")
        return _md5(material)[: min(len(self.key) + 5, 16)]

    def encrypt(self, number: int, data: bytes) -> bytes:
        return rc4(self.object_key(number), data)


def _text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(
    pages: list[list[str]], *, password: str | None = None, seed: str = "", bits: int = 40
) -> bytes:
    """A small PDF: one page per list of lines, in Helvetica. `seed` makes the bytes unique.
    With a password it is encrypted with RC4 at `bits` (40 or 128)."""
    file_id = hashlib.sha256(f"assay-test:{seed}".encode()).digest()[:16]
    encryption = Encryption(password, file_id, bits=bits) if password is not None else None
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
        objects[number] = encryption.dictionary().encode()
        trailer_extra = f" /Encrypt {number} 0 R"
    return assemble(objects, file_id, trailer_extra)


def assemble(objects: dict[int, bytes], file_id: bytes, trailer_extra: str = "") -> bytes:
    """The file: header, the numbered objects, the cross-reference table and the trailer.
    Object 1 is the catalog."""
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


# --- Pages that are pictures (for the text step) ---------------------------------------------

# A 5 by 7 dot-matrix letter table: capitals, digits and a little punctuation. Every "scan"
# here is drawn from it, so no file holds a picture of anything real.
GLYPHS = {
    "A": ".###. #...# #...# ##### #...# #...# #...#",
    "B": "####. #...# #...# ####. #...# #...# ####.",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "D": "####. #...# #...# #...# #...# #...# ####.",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "F": "##### #.... #.... ####. #.... #.... #....",
    "G": ".###. #...# #.... #.### #...# #...# .###.",
    "H": "#...# #...# #...# ##### #...# #...# #...#",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "J": "..### ...#. ...#. ...#. ...#. #..#. .##..",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# ##..# #.#.# #..## #...# #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "Q": ".###. #...# #...# #...# #.#.# #..#. .##.#",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "S": ".#### #.... #.... .###. ....# ....# ####.",
    "T": "##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
    "V": "#...# #...# #...# #...# #...# .#.#. ..#..",
    "W": "#...# #...# #...# #.#.# #.#.# ##.## #...#",
    "X": "#...# #...# .#.#. ..#.. .#.#. #...# #...#",
    "Y": "#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",
    "Z": "##### ....# ...#. ..#.. .#... #.... #####",
    "0": ".###. #...# #..## #.#.# ##..# #...# .###.",
    "1": "..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",
    "2": ".###. #...# ....# ...#. ..#.. .#... #####",
    "3": "####. ....# ....# .###. ....# ....# ####.",
    "4": "...#. ..##. .#.#. #..#. ##### ...#. ...#.",
    "5": "##### #.... ####. ....# ....# #...# .###.",
    "6": "..##. .#... #.... ####. #...# #...# .###.",
    "7": "##### ....# ...#. ..#.. .#... .#... .#...",
    "8": ".###. #...# #...# .###. #...# #...# .###.",
    "9": ".###. #...# #...# .#### ....# ...#. .##..",
    " ": "..... ..... ..... ..... ..... ..... .....",
    "-": "..... ..... ..... ##### ..... ..... .....",
    ".": "..... ..... ..... ..... ..... .##.. .##..",
    ":": "..... .##.. .##.. ..... .##.. .##.. .....",
    "/": "....# ....# ...#. ..#.. .#... #.... #....",
    ",": "..... ..... ..... ..... .##.. ..#.. .#...",
    "$": "..#.. .#### #.#.. .###. ..#.# ####. ..#..",
}
GLYPH_ROWS = 7
# Each dot is this many pixels a side: letters 35 pixels high, well above what Textract needs
# (15) on a page drawn at 200 dots an inch.
DOT = 5
# One column of dots between letters, three rows between lines.
ADVANCE = (5 + 1) * DOT
LINE_HEIGHT = (GLYPH_ROWS + 3) * DOT
SCAN_WIDTH, SCAN_HEIGHT = 1700, 2200  # US Letter at 200 dots an inch
SCAN_MARGIN = 150


def _glyph_rows(character: str) -> list[str]:
    """One letter as seven strings of pixels ("1" is ink), each dot widened to `DOT`."""
    rows = GLYPHS.get(character.upper(), GLYPHS[" "]).split()
    return ["".join(("1" if dot == "#" else "0") * DOT for dot in row + ".") for row in rows]


def picture(lines: list[str], width: int = SCAN_WIDTH, height: int = SCAN_HEIGHT) -> bytes:
    """A black-on-white picture of the lines, one bit a pixel, as a PDF image's data. Lines
    too long for the picture are cut; lines that do not fit below are left out."""
    row_bytes = (width + 7) // 8
    blank = b"\xff" * row_bytes  # in DeviceGray, a set bit is white
    rows: list[bytes] = [blank] * SCAN_MARGIN
    room = (width - 2 * SCAN_MARGIN) // ADVANCE
    for line in lines:
        if len(rows) + LINE_HEIGHT + SCAN_MARGIN > height:
            break
        letters = [_glyph_rows(character) for character in line[:room]]
        for index in range(GLYPH_ROWS):
            ink = "0" * SCAN_MARGIN + "".join(letter[index] for letter in letters)
            bits = ink.ljust(row_bytes * 8, "0")
            # Ink is 1 in `bits`; the picture wants 0 for ink.
            value = int(bits, 2) ^ ((1 << (row_bytes * 8)) - 1)
            rows += [value.to_bytes(row_bytes, "big")] * DOT
        rows += [blank] * (LINE_HEIGHT - GLYPH_ROWS * DOT)
    rows += [blank] * (height - len(rows))
    return zlib.compress(b"".join(rows[:height]), 6)


def _stream(dictionary: str, data: bytes) -> bytes:
    return f"<< {dictionary} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def _image(data: bytes, width: int, height: int) -> bytes:
    return _stream(
        f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
        "/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode",
        data,
    )


def _typed(lines: list[str], top: int = 720) -> str:
    """Page content that sets the lines in Helvetica: a text layer."""
    ops = ["BT", "/F1 12 Tf", f"72 {top} Td", "16 TL"]
    ops += [f"({_text(line)}) Tj T*" for line in lines]
    return "\n".join([*ops, "ET"])


class Pages:
    """A PDF built page by page: typed lines, pictures, and form fields. Object 1 is the
    catalog, 2 the page tree, 3 Helvetica."""

    def __init__(self, seed: str) -> None:
        self.file_id = hashlib.sha256(f"assay-test:{seed}".encode()).digest()[:16]
        self.objects: dict[int, bytes] = {
            3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        }
        self.kids: list[int] = []
        self.fields: list[int] = []
        self._next = 4

    def add(self, body: bytes) -> int:
        number = self._next
        self._next += 1
        self.objects[number] = body
        return number

    def page(
        self,
        *,
        typed: list[str] | None = None,
        scan: list[str] | None = None,
        # The part of the page the picture covers: (left, bottom, width, height) in points.
        scan_box: tuple[float, float, float, float] = (0, 0, 612, 792),
        # Form fields: (name, value, left, bottom, width, height).
        fields: list[tuple[str, str, float, float, float, float]] | None = None,
    ) -> None:
        content: list[str] = []
        resources = "/Font << /F1 3 0 R >>"
        if scan is not None:
            left, bottom, width, height = scan_box
            pixels = (round(SCAN_WIDTH * width / 612), round(SCAN_HEIGHT * height / 792))
            image = self.add(_image(picture(scan, *pixels), *pixels))
            resources += f" /XObject << /Im1 {image} 0 R >>"
            content.append(f"q {width} 0 0 {height} {left} {bottom} cm /Im1 Do Q")
        if typed is not None:
            content.append(_typed(typed))
        contents = self.add(_stream("", "\n".join(content).encode("latin-1")))
        # The page's number is taken first: its fields point back at it.
        number = self._next
        self._next += 1
        annotations = [self._field(number, *field) for field in fields or []]
        annots = f" /Annots [{' '.join(f'{a} 0 R' for a in annotations)}]" if annotations else ""
        self.objects[number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << {resources} >> /Contents {contents} 0 R{annots} >>"
        ).encode()
        self.kids.append(number)

    def _field(
        self,
        page: int,
        name: str,
        value: str,
        left: float,
        bottom: float,
        width: float,
        height: float,
    ) -> int:
        """A text field with a value and the appearance a viewer draws for it. The value is
        in the field only: it is not part of the page's text layer."""
        appearance = self.add(
            _stream(
                f"/Type /XObject /Subtype /Form /BBox [0 0 {width} {height}] "
                "/Resources << /Font << /F1 3 0 R >> >>",
                f"/Tx BMC q BT /F1 12 Tf 0 g 2 5 Td ({_text(value)}) Tj ET Q EMC".encode("latin-1"),
            )
        )
        field = self.add(
            (
                f"<< /Type /Annot /Subtype /Widget /FT /Tx /T ({_text(name)}) /V ({_text(value)}) "
                f"/Rect [{left} {bottom} {left + width} {bottom + height}] /P {page} 0 R /F 4 "
                f"/DA (/F1 12 Tf 0 g) /AP << /N {appearance} 0 R >> >>"
            ).encode("latin-1")
        )
        self.fields.append(field)
        return field

    def build(self) -> bytes:
        form = ""
        if self.fields:
            form = (
                f" /AcroForm << /Fields [{' '.join(f'{f} 0 R' for f in self.fields)}] "
                "/DA (/F1 12 Tf 0 g) /DR << /Font << /F1 3 0 R >> >> >>"
            )
        self.objects[1] = f"<< /Type /Catalog /Pages 2 0 R{form} >>".encode()
        kids = " ".join(f"{kid} 0 R" for kid in self.kids)
        self.objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(self.kids)} >>".encode()
        return assemble(self.objects, self.file_id)


def package(title: str, applicant: str, loan: str, run: str | None = None) -> list[list[str]]:
    """Three pages of an obviously synthetic loan package. `run` adds a line that makes this
    run's pages unlike any other run's."""
    note = "SYNTHETIC TEST DOCUMENT - not a real application. Assay Insights build checks."
    pages = _package(note, title, applicant, loan)
    if run is not None:
        for page in pages:
            page.insert(1, f"Test run: {run}")
    return pages


def _package(note: str, title: str, applicant: str, loan: str) -> list[list[str]]:
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


def files(tenant_id: uuid.UUID | None, run: str | None = None) -> dict[str, bytes]:
    """Every file, by name. With `run` (a short ID), every PDF's pages carry it, so the PDFs
    are new documents; without it they are the same bytes on every call."""
    suffix = "" if run is None else f":{run}"

    def document(title: str, applicant: str, loan: str, seed: str, **options: Any) -> bytes:
        return pdf(package(title, applicant, loan, run), seed=seed + suffix, **options)

    normal = document("Package 001", "Jane Testperson", "TEST-001", "normal")
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
        # Four key errors, all reported at once (owner decision A2): another tenant's ID, a
        # date in the wrong form, a text loan term and a rate out of range. (A nested value
        # would stop the check at that one problem, so there is none.)
        "TenantID": str(uuid.UUID("00000000-0000-4000-8000-00000000dead")),
        "CreateDate": "28/10/2026",
        "LoanTerm": "thirty years",
        "LoanInterestRate": 101,
    }
    return {
        "01-normal.pdf": normal,
        "02-normal-again.pdf": normal,
        "03-password-protected.pdf": document(
            "Package 003", "Pat Testperson", "TEST-003", "locked", password=PASSWORD
        ),
        "04-with-companion.pdf": document("Package 004", "Sam Testperson", "TEST-004", "companion"),
        "04-with-companion.json": json.dumps(valid_companion, indent=2).encode(),
        "05-invalid-companion.pdf": document(
            "Package 005", "Lee Testperson", "TEST-005", "invalid"
        ),
        "05-invalid-companion.json": json.dumps(invalid_companion, indent=2).encode(),
        # Plain text named .pdf: scans clean, then fails at fingerprint (`not_a_pdf`).
        "06-not-a-pdf.pdf": b"This is plain text named .pdf, for the not_a_pdf browser check.\n",
        # The same protection with a 128-bit key: another kind for the malware scan to
        # recognise or miss. Whichever step holds it, it is never processed.
        "07-password-protected-128.pdf": document(
            "Package 007", "Kim Testperson", "TEST-007", "locked-128", password=PASSWORD, bits=128
        ),
        # For the text step: pages that are pictures, pages that are partly pictures, and a
        # form whose answers are in its fields.
        "08-scanned.pdf": scanned("Package 008", "Ann Testperson", "TEST-008", run),
        "09-mixed.pdf": mixed("Package 009", "Ray Testperson", "TEST-009", run),
        "10-filled-form.pdf": filled_form("Package 010", "Joy Testperson", "TEST-010", run),
    }


LARGE_NAME = "11-scanned-1000-pages.pdf"
LARGE_PAGES = 1000


def scanned(
    title: str, applicant: str, loan: str, run: str | None = None, *, pages: int | None = None
) -> bytes:
    """Pages that are pictures only: no text layer at all, so every page goes to Textract.
    With `pages`, the three pages are repeated up to that many, each with its own number."""
    source = package(title, applicant, loan, run)
    total = len(source) if pages is None else pages
    builder = Pages(f"scanned:{loan}:{run}:{total}")
    for number in range(1, total + 1):
        lines = [*source[(number - 1) % len(source)], "", f"PAGE {number} OF {total}"]
        builder.page(scan=lines)
    return builder.build()


def mixed(title: str, applicant: str, loan: str, run: str | None = None) -> bytes:
    """Three kinds of page in one document:

    1. typed only: its text layer is all there is (stays `native`);
    2. typed, with a picture pasted over the lower two fifths of the page: the picture's words
       are not in the text layer (goes to Textract; the text layer's text is kept beside it);
    3. a picture of a whole page with one typed line stamped on it, as an e-signing service
       stamps an envelope ID on a scan (goes to Textract; the stamped line is kept beside it).
    """
    first, second, third = package(title, applicant, loan, run)
    builder = Pages(f"mixed:{loan}:{run}")
    builder.page(typed=first)
    builder.page(
        typed=second,
        scan=["ATTACHED COPY OF IDENTITY DOCUMENT", "NAME: " + applicant, "NUMBER: T 000 111 222"],
        scan_box=(0, 60, 612, 316.8),
    )
    stamp = f"Envelope ID: {uuid.uuid5(uuid.NAMESPACE_URL, loan)}"
    builder.page(typed=[stamp], scan=["", "", *third])
    return builder.build()


def filled_form(title: str, applicant: str, loan: str, run: str | None = None) -> bytes:
    """A typed form whose answers are in form fields. The questions are in the text layer;
    the answers are not, so the page goes to Textract, which reads both from its picture."""
    note, *_ = package(title, applicant, loan, run)[0]
    labels = [
        note,
        *([f"Test run: {run}"] if run is not None else []),
        "",
        f"LOAN APPLICATION FORM - {title}",
        "",
        "Borrower name:",
        "",
        "Loan number:",
        "",
        "Amount requested:",
    ]
    # Each answer sits to the right of its question: lines are 16 points apart from 720.
    row = len(labels) - 5
    fields = [
        ("borrower", applicant, 200.0, 715.0 - 16 * row, 300.0, 18.0),
        ("loan_number", loan, 200.0, 715.0 - 16 * (row + 2), 300.0, 18.0),
        ("amount", "$250,000.00", 200.0, 715.0 - 16 * (row + 4), 300.0, 18.0),
    ]
    builder = Pages(f"form:{loan}:{run}")
    builder.page(typed=labels, fields=fields)
    return builder.build()


def large(run: str | None = None) -> dict[str, bytes]:
    """The 1,000-page scanned file (`--pages-1000`): one run of it measures the text step
    against the worker task's time limit, and costs 1,000 Textract pages."""
    return {
        LARGE_NAME: scanned("Package 011", "Max Testperson", "TEST-011", run, pages=LARGE_PAGES)
    }


def new_run_id() -> str:
    """The time to the second and a few random characters: short enough to read back."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tenant-id", type=uuid.UUID, default=None)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="add a new run ID to every page of every PDF, so none is a duplicate of an "
        "earlier upload",
    )
    parser.add_argument(
        "--pages-1000",
        action="store_true",
        help=f"also write {LARGE_NAME}: {LARGE_PAGES} scanned pages, to time one run of the "
        f"text step. Uploading it sends {LARGE_PAGES} pages to Textract",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    run = new_run_id() if args.fresh else None
    if run is not None:
        print(f"Test run {run}: these PDFs are unlike any written before.")
    written = files(args.tenant_id, run)
    if args.pages_1000:
        written |= large(run)
    for name, body in written.items():
        (args.out / name).write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()[:16]
        print(f"{args.out / name}  {len(body)} bytes  sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
