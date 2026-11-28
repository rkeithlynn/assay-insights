"""The whole Pulumi program, run under mocks with the real stack config (Pulumi.dev.yaml).

AWS validates some fields only when a resource is created, so a bad value passes
`pulumi preview` and fails `pulumi up` half-way. The first `pulumi up` of build step 5 failed
that way on a security group rule description with an apostrophe. This test runs
`infra/__main__.py` itself, so every resource in the program is checked, and holds each
description-like field to the rule of the service that stores it.
"""

from __future__ import annotations

import io
import json
import re
import runpy
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pulumi
import pytest
import yaml  # type: ignore[import-untyped]

from tests.unit.conftest import REGISTERED, after_settled, require_settled
from tests.unit.test_infra_auth_api import FAKE_JWKS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAM = REPO_ROOT / "infra" / "__main__.py"
STACK_CONFIG = REPO_ROOT / "infra" / "Pulumi.dev.yaml"

# EC2: security group and rule descriptions (CreateSecurityGroup, AuthorizeSecurityGroup*).
EC2_DESCRIPTION = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]{0,255}$")
# IAM: role descriptions.
IAM_DESCRIPTION = re.compile(
    r"^[\t\n\r\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]{0,1000}$"
)
# AWS WAF: web ACL descriptions.
WAF_DESCRIPTION = re.compile(r"^[\w+=:#@/\-,.][\w+=:#@/\-,.\s]+[\w+=:#@/\-,.]$")


def _limit(length: int) -> re.Pattern[str]:
    return re.compile(rf"^.{{0,{length}}}$", re.DOTALL)


# Resource type -> (input field, rule).
RULES: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "aws:ec2/securityGroup:SecurityGroup": [("description", EC2_DESCRIPTION)],
    "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule": [("description", EC2_DESCRIPTION)],
    "aws:vpc/securityGroupEgressRule:SecurityGroupEgressRule": [("description", EC2_DESCRIPTION)],
    "aws:ec2/securityGroupRule:SecurityGroupRule": [("description", EC2_DESCRIPTION)],
    "aws:iam/role:Role": [("description", IAM_DESCRIPTION)],
    "aws:wafv2/webAcl:WebAcl": [("description", WAF_DESCRIPTION)],
    "aws:lambda/function:Function": [("description", _limit(256))],
    "aws:cloudfront/distribution:Distribution": [("comment", _limit(128))],
    "aws:cloudfront/function:Function": [("comment", _limit(128))],
    "aws:cloudfront/responseHeadersPolicy:ResponseHeadersPolicy": [("comment", _limit(128))],
    "aws:cloudfront/originAccessControl:OriginAccessControl": [("description", _limit(256))],
    "aws:cloudwatch/metricAlarm:MetricAlarm": [("alarmDescription", _limit(1024))],
    "aws:cognito/userGroup:UserGroup": [("description", _limit(2048))],
    "aws:kms/key:Key": [("description", _limit(8192))],
    "aws:scheduler/schedule:Schedule": [("description", _limit(512))],
}


def problems(entries: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    found = []
    for type_token, name, inputs in entries:
        for field, rule in RULES.get(type_token, []):
            value = inputs.get(field)
            if isinstance(value, str) and not rule.match(value):
                found.append(f"{type_token} {name}: {field} {value!r}")
    return found


def test_the_check_catches_the_description_that_failed_pulumi_up() -> None:
    bad = "HTTPS from CloudFront's VPC origin only"
    entry = ("aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule", "r", {"description": bad})
    assert problems([entry]) != []
    waf = ("aws:wafv2/webAcl:WebAcl", "w", {"description": "Assay's portal"})
    assert problems([waf]) != []
    role = ("aws:iam/role:Role", "r", {"description": "The migration Lambda's role"})
    assert problems([role]) == []


def _set_stack_config() -> None:
    """The stack's config, under the mocks' project name instead of `assay-platform`."""
    document = yaml.safe_load(STACK_CONFIG.read_text())
    project = pulumi.get_project()
    for key, value in document["config"].items():
        namespace, _, name = key.partition(":")
        if namespace == "assay-platform":
            key = f"{project}:{name}"
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        pulumi.runtime.set_config(key, str(value))


class _Jwks(io.BytesIO):
    def __enter__(self) -> _Jwks:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The program reads each pool's JWKS at `pulumi up`; here it gets a fixed one."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Jwks(FAKE_JWKS.encode()))
    yield


@pulumi.runtime.test
def test_every_description_in_the_program_is_valid_for_its_service(no_network: None) -> Any:
    _set_stack_config()
    start = len(REGISTERED)
    program = runpy.run_path(str(PROGRAM), run_name="__main__")

    def check(_: list[Any]) -> None:
        # The whole program is registered: nothing is still on its way.
        require_settled()
        entries = REGISTERED[start:]
        # The run built the whole program, edge included: the check is not vacuous.
        types = {t for t, _, _ in entries}
        assert {
            "aws:cloudfront/distribution:Distribution",
            "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule",
            "aws:wafv2/webAcl:WebAcl",
            "aws:iam/role:Role",
        } <= types
        rules = [n for t, n, _ in entries if t.endswith("SecurityGroupIngressRule")]
        assert any(n.endswith("-edge-alb-in-cloudfront") for n in rules)
        assert problems(entries) == []

    return after_settled(
        program["edge"].distribution.id,
        program["edge"].load_balancer_ingress.id,
        program["seed"].function.id,
        program["api"].service.id,
        program["auth_events"].consumer_function.id,
    ).apply(check)
