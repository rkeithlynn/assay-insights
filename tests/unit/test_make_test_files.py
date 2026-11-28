"""The synthetic browser-check files (scripts/make_test_files.py) are what they claim to be.

PDFium (the library the worker's sandboxed inspect step uses) opens the plain PDFs and refuses
the protected one with its password error; the companion files pass and fail the real
validator.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pypdfium2
import pytest

from assay_schemas import companion
from assay_workers import steps
from assay_workers.sandbox import jobs
from assay_workers.sandbox.jobs import PDFIUM_PASSWORD_ERROR

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_test_files.py"
TENANT = uuid.UUID("7f3a91c2-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_test_files", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_test_files"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def made(script: ModuleType) -> dict[str, bytes]:
    result: dict[str, bytes] = script.files(TENANT)
    return result


def test_the_plain_pdfs_open_with_three_pages_and_text(made: dict[str, bytes]) -> None:
    for name in ("01-normal.pdf", "04-with-companion.pdf", "05-invalid-companion.pdf"):
        document = pypdfium2.PdfDocument(made[name])
        try:
            assert len(document) == 3, name
            text = document[0].get_textpage().get_text_range()
            assert "SYNTHETIC TEST DOCUMENT" in text, name
        finally:
            document.close()


def test_the_duplicate_is_byte_for_byte_the_same(made: dict[str, bytes]) -> None:
    assert made["01-normal.pdf"] == made["02-normal-again.pdf"]
    assert made["01-normal.pdf"] != made["04-with-companion.pdf"]


def test_the_protected_pdf_needs_its_password(
    made: dict[str, bytes], script: ModuleType, tmp_path: Path
) -> None:
    body = made["03-password-protected.pdf"]
    with pytest.raises(pypdfium2.PdfiumError) as raised:
        pypdfium2.PdfDocument(body)
    assert raised.value.err_code == PDFIUM_PASSWORD_ERROR
    document = pypdfium2.PdfDocument(body, password=script.PASSWORD)
    try:
        assert "SYNTHETIC TEST DOCUMENT" in document[0].get_textpage().get_text_range()
    finally:
        document.close()
    # It has a PDF header, so fingerprint passes it on; the inspect step holds it.
    path = tmp_path / "locked.pdf"
    path.write_bytes(body)
    assert steps.has_pdf_header(path)
    assert jobs.inspect(path) == {"problem": "pdf_encrypted", "pages": None}


def test_the_valid_companion_is_valid_and_the_invalid_one_is_not(made: dict[str, bytes]) -> None:
    valid = companion.validate(made["04-with-companion.json"], tenant_id=TENANT)
    assert valid.tenant_id == TENANT and valid.loan_term_months == 360
    with pytest.raises(companion.CompanionError) as raised:
        companion.validate(made["05-invalid-companion.json"], tenant_id=TENANT)
    # Every problem at once (owner decision A2).
    assert [d["code"] for d in raised.value.details()] == [
        "tenant_mismatch",
        "must_be_a_date",
        "must_be_a_whole_number",
        "out_of_range",
    ]


def test_the_not_a_pdf_file_fails_fingerprint(made: dict[str, bytes], tmp_path: Path) -> None:
    path = tmp_path / "not.pdf"
    path.write_bytes(made["06-not-a-pdf.pdf"])
    assert not steps.has_pdf_header(path)
    assert jobs.inspect(path) == {"problem": "not_a_pdf", "pages": None}


def test_without_a_tenant_id_the_valid_companion_has_none(script: ModuleType) -> None:
    body = script.files(None)["04-with-companion.json"]
    assert companion.validate(body, tenant_id=uuid.uuid4()).tenant_id is None


def test_the_128_bit_protected_pdf_needs_its_password_too(
    made: dict[str, bytes], script: ModuleType, tmp_path: Path
) -> None:
    """The same protection with a 128-bit key (revision 3), checked against PDFium: it
    refuses without the password, opens with it, and the inspect job holds it."""
    body = made["07-password-protected-128.pdf"]
    assert b"/V 2 /R 3 /Length 128" in body and b"/V 1 /R 2" not in body
    assert b"/V 1 /R 2" in made["03-password-protected.pdf"]
    with pytest.raises(pypdfium2.PdfiumError) as raised:
        pypdfium2.PdfDocument(body)
    assert raised.value.err_code == PDFIUM_PASSWORD_ERROR
    with pytest.raises(pypdfium2.PdfiumError):
        pypdfium2.PdfDocument(body, password="not-the-password")
    document = pypdfium2.PdfDocument(body, password=script.PASSWORD)
    try:
        assert len(document) == 3
        assert "SYNTHETIC TEST DOCUMENT" in document[0].get_textpage().get_text_range()
        assert "TEST-007" in document[0].get_textpage().get_text_range()
    finally:
        document.close()
    path = tmp_path / "locked-128.pdf"
    path.write_bytes(body)
    assert steps.has_pdf_header(path)
    assert jobs.inspect(path) == {"problem": "pdf_encrypted", "pages": None}


def test_without_fresh_the_files_are_the_same_on_every_run(script: ModuleType) -> None:
    assert script.files(TENANT) == script.files(TENANT)


def test_fresh_pdfs_are_new_documents_every_run(script: ModuleType) -> None:
    """`--fresh`: no PDF of one run has the bytes of any PDF of another run or of the plain
    set, so none stops as a duplicate; within a run, 02 is still 01."""
    plain = script.files(TENANT)
    first, second = script.files(TENANT, "run-a"), script.files(TENANT, "run-b")
    pdfs = [name for name in plain if name.endswith(".pdf") and name != "06-not-a-pdf.pdf"]
    # Six since build step 6's first half; 08 to 10 (scanned, mixed, filled form) since stage 2.
    assert len(pdfs) == 9
    for name in pdfs:
        assert len({plain[name], first[name], second[name]}) == 3, name
    assert first["01-normal.pdf"] == first["02-normal-again.pdf"]
    # The companion files, and the text file, do not change.
    for name in ("04-with-companion.json", "05-invalid-companion.json", "06-not-a-pdf.pdf"):
        assert plain[name] == first[name] == second[name]
    # Still PDFs that open, with the run's line on every page.
    document = pypdfium2.PdfDocument(first["01-normal.pdf"])
    try:
        assert len(document) == 3
        for page in range(3):
            assert "Test run: run-a" in document[page].get_textpage().get_text_range()
    finally:
        document.close()
    locked = pypdfium2.PdfDocument(first["03-password-protected.pdf"], password=script.PASSWORD)
    try:
        assert "Test run: run-a" in locked[0].get_textpage().get_text_range()
    finally:
        locked.close()


def test_run_ids_do_not_repeat(script: ModuleType) -> None:
    ids = {script.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(run) == 22 for run in ids)


def test_fresh_on_the_command_line_writes_new_pdfs(
    script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def run(*arguments: str) -> bytes:
        monkeypatch.setattr(sys, "argv", ["make_test_files.py", "--out", str(tmp_path), *arguments])
        assert script.main() == 0
        return (tmp_path / "01-normal.pdf").read_bytes()

    plain = run()
    assert run() == plain
    fresh = run("--fresh")
    assert fresh != plain and run("--fresh") != fresh
    assert "Test run " in capsys.readouterr().out
    assert (tmp_path / "07-password-protected-128.pdf").exists()
