"""Unit tests for the DNS and certificate component, using Pulumi mocks (no AWS calls)."""

from __future__ import annotations

from typing import Any

import pulumi
import pytest

from assay_infra.config import build_dns
from assay_infra.dns import DnsAndCertificate
from tests.unit.conftest import after_settled

ZONE = "example.test"
HOSTS = ["example.test", "admin.example.test"]


@pulumi.runtime.test
def test_certificate_covers_every_host() -> Any:
    component = DnsAndCertificate("t", zone_name=ZONE, hosts=HOSTS)

    def check(args: list[Any]) -> None:
        domain, sans, method = args
        assert [domain, *sans] == HOSTS
        assert method == "DNS"

    return after_settled(
        component.certificate.domain_name,
        component.certificate.subject_alternative_names,
        component.certificate.validation_method,
    ).apply(check)


@pulumi.runtime.test
def test_one_validation_record_per_host_in_the_zone() -> Any:
    component = DnsAndCertificate("t2", zone_name=ZONE, hosts=HOSTS)
    assert len(component.validation_records) == len(HOSTS)

    def check(args: list[Any]) -> None:
        zone_ids, names = args[: len(HOSTS)], args[len(HOSTS) :]
        assert set(zone_ids) == {"Z123"}
        assert sorted(names) == sorted(f"_token.{h}." for h in HOSTS)

    return after_settled(
        *[r.zone_id for r in component.validation_records],
        *[r.name for r in component.validation_records],
    ).apply(check)


def test_config_rejects_host_outside_zone() -> None:
    with pytest.raises(ValueError, match="not inside hosted zone"):
        build_dns("example.test", "example.test", "admin.other.test")


def test_config_rejects_suffix_lookalike() -> None:
    with pytest.raises(ValueError, match="not inside hosted zone"):
        build_dns("example.test", "example.test", "admin.badexample.test")


def test_config_normalises_case_and_trailing_dot() -> None:
    cfg = build_dns("Example.Test.", "example.test", "ADMIN.example.test.")
    assert cfg.hosts == HOSTS
