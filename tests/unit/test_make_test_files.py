"""The synthetic browser-check files (scripts/make_test_files.py) are what they claim to be.

PDFium (the library the fingerprint step uses) opens the plain PDFs and refuses the protected
one with its password error; the companion files pass and fail the real validator.
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
from assay_workers.steps import PDFIUM_PASSWORD_ERROR, inspect_pdf

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
    # And the fingerprint step holds it.
    path = tmp_path / "locked.pdf"
    path.write_bytes(body)
    assert inspect_pdf(path) == ("pdf_encrypted", None)


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
    assert inspect_pdf(path) == ("not_a_pdf", None)


def test_without_a_tenant_id_the_valid_companion_has_none(script: ModuleType) -> None:
    body = script.files(None)["04-with-companion.json"]
    assert companion.validate(body, tenant_id=uuid.uuid4()).tenant_id is None
