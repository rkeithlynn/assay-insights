"""Unit tests for the DNS and certificate component, using Pulumi mocks (no AWS calls)."""

from __future__ import annotations

import asyncio
from typing import Any

import pulumi
import pytest

# Python 3.14+ no longer creates a default event loop; Pulumi's mock runtime needs one.
asyncio.set_event_loop(asyncio.new_event_loop())

ZONE = "example.test"
HOSTS = ["example.test", "admin.example.test"]


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str, dict[str, Any]]:
        outputs = dict(args.inputs)
        if args.typ == "aws:route53/zone:Zone":
            outputs["zoneId"] = "Z123"
            outputs["nameServers"] = [f"ns-{i}.awsdns.test" for i in range(4)]
        if args.typ == "aws:acm/certificate:Certificate":
            names = [args.inputs["domainName"], *args.inputs.get("subjectAlternativeNames", [])]
            outputs["arn"] = "arn:aws:acm:us-east-1:000000000000:certificate/mock"
            outputs["domainValidationOptions"] = [
                {
                    "domainName": n,
                    "resourceRecordName": f"_token.{n}.",
                    "resourceRecordType": "CNAME",
                    "resourceRecordValue": f"_value.{n}.acm-validations.aws.",
                }
                for n in names
            ]
        return f"{args.name}-id", outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[Any]]:
        return {}, []


pulumi.runtime.set_mocks(Mocks(), preview=False)

from assay_infra.config import build_config  # noqa: E402
from assay_infra.dns import DnsAndCertificate  # noqa: E402


@pulumi.runtime.test
def test_certificate_covers_every_host() -> Any:
    component = DnsAndCertificate("t", zone_name=ZONE, hosts=HOSTS)

    def check(args: list[Any]) -> None:
        domain, sans, method = args
        assert [domain, *sans] == HOSTS
        assert method == "DNS"

    return pulumi.Output.all(
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

    return pulumi.Output.all(
        *[r.zone_id for r in component.validation_records],
        *[r.name for r in component.validation_records],
    ).apply(check)


def test_config_rejects_host_outside_zone() -> None:
    with pytest.raises(ValueError, match="not inside hosted zone"):
        build_config("example.test", "example.test", "admin.other.test")


def test_config_rejects_suffix_lookalike() -> None:
    with pytest.raises(ValueError, match="not inside hosted zone"):
        build_config("example.test", "example.test", "admin.badexample.test")


def test_config_normalises_case_and_trailing_dot() -> None:
    cfg = build_config("Example.Test.", "example.test", "ADMIN.example.test.")
    assert cfg.hosts == HOSTS
