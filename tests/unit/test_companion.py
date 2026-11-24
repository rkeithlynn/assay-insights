"""The companion file, version 1 (BUILD_SPEC.md section 8), validated with plain code."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from assay_schemas import companion
from assay_schemas.companion import CompanionError, validate

TENANT = uuid.UUID("7f3a91c2-0000-4000-8000-000000000001")
OTHER = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")


def raw(document: object) -> bytes:
    return json.dumps(document).encode()


def test_the_specs_example_is_valid() -> None:
    """The example in section 8, with this tenant's ID."""
    result = validate(
        raw(
            {
                "TenantID": str(TENANT),
                "ApplicationId": "APP-2026-00123",
                "LoanNumber": "",
                "CreateDate": "2026-10-28",
                "CompleteDate": "2026-11-02",
                "Signer1Name": "Jane Smith",
                "Signer1Email": "jane@example.com",
                "Signer1Phone": "+14045550100",
                "Signer1Location": "Atlanta, GA",
                "CosignerName": "John Smith",
                "LoanTerm": 360,
                "LoanMaturityDate": "2056-12-01",
                "LoanInterestRate": 6.125,
                "Branch": "Atlanta",
            }
        ),
        tenant_id=TENANT,
    )
    assert result.tenant_id == TENANT
    assert result.application_ref == "APP-2026-00123"
    assert result.loan_number is None  # an empty value is no value
    assert result.package_created_on == dt.date(2026, 10, 28)
    assert result.package_completed_on == dt.date(2026, 11, 2)
    assert result.cosigner_name == "John Smith"
    assert result.loan_term_months == 360
    assert result.loan_maturity_date == dt.date(2056, 12, 1)
    assert result.loan_interest_rate == 6.125
    [signer] = result.signers
    assert (signer.number, signer.name, signer.location) == (1, "Jane Smith", "Atlanta, GA")
    assert result.labels == {"Branch": "Atlanta"}


def test_every_key_is_optional() -> None:
    assert validate(b"{}", tenant_id=TENANT) == companion.Companion()


def test_reserved_keys_are_matched_case_insensitively() -> None:
    result = validate(
        raw({"applicationid": "A-1", "LOANTERM": 12, "signer2EMAIL": "x@y"}), tenant_id=TENANT
    )
    assert result.application_ref == "A-1" and result.loan_term_months == 12
    assert result.signers[0].number == 2 and result.signers[0].email == "x@y"
    assert result.labels == {}


def test_a_byte_order_mark_is_allowed() -> None:
    assert validate(b"\xef\xbb\xbf{}", tenant_id=TENANT) == companion.Companion()


@pytest.mark.parametrize(
    ("data", "code", "key"),
    [
        # TenantID is only ever checked, never used to choose a tenant.
        ({"TenantID": str(OTHER)}, "tenant_mismatch", "TenantID"),
        ({"tenantid": "not-a-uuid"}, "must_be_a_uuid", "tenantid"),
        ({"TenantID": 12}, "must_be_a_uuid", "TenantID"),
        ({"ApplicationId": 5}, "must_be_text", "ApplicationId"),
        ({"LoanNumber": "x" * 501}, "too_long", "LoanNumber"),
        ({"CreateDate": "10/28/2026"}, "must_be_a_date", "CreateDate"),
        ({"CompleteDate": "2026-02-30"}, "must_be_a_date", "CompleteDate"),
        ({"LoanTerm": 360.5}, "must_be_a_whole_number", "LoanTerm"),
        ({"LoanTerm": True}, "must_be_a_whole_number", "LoanTerm"),
        ({"LoanTerm": 0}, "out_of_range", "LoanTerm"),
        ({"LoanInterestRate": "6.1"}, "must_be_a_number", "LoanInterestRate"),
        ({"LoanInterestRate": 101}, "out_of_range", "LoanInterestRate"),
        ({"Signer4Name": "x"}, "unknown_signer", "Signer4Name"),
        ({"Signer1Name": None}, "must_be_text", "Signer1Name"),
        ({"9lives": "x"}, "invalid_key", "9lives"),
        ({"has space": "x"}, "invalid_key", "has space"),
        ({"A" * 65: "x"}, "invalid_key", "A" * 65),
        ({"Note": "x" * 501}, "too_long", "Note"),
        ({"Note": None}, "must_not_be_null", "Note"),
    ],
)
def test_invalid_values_are_refused_by_key(data: dict[str, object], code: str, key: str) -> None:
    with pytest.raises(CompanionError) as raised:
        validate(raw(data), tenant_id=TENANT)
    assert (raised.value.code, raised.value.key) == (code, key)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"[]", "not_an_object"),
        (b'"text"', "not_an_object"),
        (b"{", "not_json"),
        (b"", "not_json"),
        (b'{"a": NaN}', "not_json"),
        (b'{"a": Infinity}', "not_json"),
        (b"\xff\xfe{}", "not_utf8"),
        (b'{"Note": {"x": 1}}', "nested_value"),
        (b'{"Note": [1, 2]}', "nested_value"),
        (b"[" * 60_000, "nested_value"),  # deep, but under the size cap: never parsed
        (b'{"Note": "a", "note": "b"}', "duplicate_key"),
        (b'{"LoanTerm": 1, "loanterm": 2}', "duplicate_key"),
        (b'{"x": "' + b"a" * (companion.MAX_BYTES) + b'"}', "too_large"),
    ],
)
def test_malformed_files_are_refused(body: bytes, code: str) -> None:
    with pytest.raises(CompanionError) as raised:
        validate(body, tenant_id=TENANT)
    assert raised.value.code == code


def test_brackets_inside_strings_are_not_nesting() -> None:
    result = validate(raw({"Note": "[{[{]}]}"}), tenant_id=TENANT)
    assert result.labels == {"Note": "[{[{]}]}"}


def test_custom_values_may_be_text_numbers_or_booleans() -> None:
    result = validate(
        raw({"Branch": "Atlanta", "Score": 7, "Ratio": 0.5, "Vip": False}), tenant_id=TENANT
    )
    assert result.labels == {"Branch": "Atlanta", "Score": 7, "Ratio": 0.5, "Vip": False}


def test_at_most_fifty_custom_keys() -> None:
    validate(raw({f"K{i}": i for i in range(50)}), tenant_id=TENANT)
    with pytest.raises(CompanionError) as raised:
        validate(raw({f"K{i}": i for i in range(51)}), tenant_id=TENANT)
    assert raised.value.code == "too_many_custom_keys"


def test_an_error_never_carries_a_value_from_the_file() -> None:
    secret = "SSN-123-45-6789"
    for data in ({"LoanTerm": secret}, {"bad key": secret}, {"TenantID": secret}):
        with pytest.raises(CompanionError) as raised:
            validate(raw(data), tenant_id=TENANT)
        assert secret not in str(raised.value)
        assert secret not in json.dumps(raised.value.as_detail())
