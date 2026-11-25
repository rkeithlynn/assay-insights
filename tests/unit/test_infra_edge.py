"""The edge (CloudFront, WAF, portal bucket, DNS, logs), using Pulumi mocks (no AWS calls)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws
import pytest

from assay_infra.auth import Auth
from assay_infra.edge import (
    CLOUDFRONT_VPC_ORIGIN_GROUP,
    Edge,
    EdgeLogs,
    content_security_policy,
    portal_config,
    portal_source_hash,
)
from assay_infra.network import Network
from tests.unit.conftest import (
    ACCOUNT_ID,
    CALLS,
    CLOUDFRONT_VPC_ORIGIN_SG,
    ELB_ACCOUNT_ID,
    REGION,
    registered,
)
from tests.unit.test_infra_auth_api import AUTH, AUTH_EVENT_TRIGGER, DNS, FAKE_JWKS
from tests.unit.test_infra_data import NET

REPO_ROOT = Path(__file__).resolve().parents[2]


def _one(type_token: str, name: str) -> dict[str, Any]:
    matches = [inputs for n, inputs in registered(type_token) if n == name]
    assert len(matches) == 1, f"{type_token} {name}: {len(matches)} registered"
    return matches[0]


def _edge(prefix: str) -> Edge:
    network = Network(prefix, cfg=NET, region=REGION, account_id=ACCOUNT_ID)
    auth = Auth(
        f"{prefix}-auth",
        cfg=AUTH,
        dns=DNS,
        region=REGION,
        account_id=ACCOUNT_ID,
        repo_root=REPO_ROOT,
        auth_event_lambda_arn=AUTH_EVENT_TRIGGER,
        jwks_fetcher=lambda region, pool_id: FAKE_JWKS,
    )
    alb_group = aws.ec2.SecurityGroup(f"{prefix}-alb-sg", vpc_id=network.vpc.id)
    alb = aws.lb.LoadBalancer(f"{prefix}-alb", internal=True, security_groups=[alb_group.id])
    logs = EdgeLogs(f"{prefix}-edge-logs", region=REGION, account_id=ACCOUNT_ID)
    return Edge(
        f"{prefix}-edge",
        repo_root=REPO_ROOT,
        region=REGION,
        dns=DNS,
        zone_id="Z123",
        certificate_arn="arn:aws:acm:us-east-1:000000000000:certificate/validated",
        network=network,
        auth=auth,
        load_balancer=alb,
        load_balancer_security_group=alb_group,
        logs=logs,
        waf_rate_limit=1000,
    )


def _ready(edge: Edge) -> pulumi.Output[list[Any]]:
    return pulumi.Output.all(
        edge.distribution.id,
        edge.load_balancer_ingress.id,
        edge.portal_policy.policy,
        edge.config_object.content,
        edge.portal_build.id,
        edge.headers_policy.id,
        edge.waf_logging.id,
        edge.log_delivery.id,
        *[r.id for r in edge.records],
    )


@pulumi.runtime.test
def test_one_distribution_serves_both_hosts_with_the_portal_and_the_api() -> Any:
    edge = _edge("e1")

    def check(_: list[Any]) -> None:
        cdn = _one("aws:cloudfront/distribution:Distribution", "e1-edge-cdn")
        assert sorted(cdn["aliases"]) == sorted(DNS.hosts)
        assert cdn["viewerCertificate"]["minimumProtocolVersion"] == "TLSv1.2_2021"
        assert cdn["viewerCertificate"]["sslSupportMethod"] == "sni-only"
        assert cdn["webAclId"].endswith("e1-edge-waf")
        portal, api = cdn["origins"]
        assert portal["originAccessControlId"] == "e1-edge-oac-id"
        # Origin access control only. An s3OriginConfig with an empty origin access identity
        # is not stored by AWS, so stating it made every preview show a difference.
        assert "s3OriginConfig" not in portal
        assert api["vpcOriginConfig"]["vpcOriginId"] == "e1-edge-api-origin-id"
        default = cdn["defaultCacheBehavior"]
        assert default["targetOriginId"] == "portal"
        assert default["viewerProtocolPolicy"] == "redirect-to-https"
        assert default["cachePolicyId"] == "cache-policy-Managed-CachingOptimized"
        assert default["functionAssociations"][0]["eventType"] == "viewer-request"
        [api_behaviour] = cdn["orderedCacheBehaviors"]
        assert api_behaviour["pathPattern"] == "/api/*"
        assert api_behaviour["targetOriginId"] == "api"
        assert api_behaviour["cachePolicyId"] == "cache-policy-Managed-CachingDisabled"
        # Every viewer header (Host, for the load balancer's certificate; Authorization) and
        # CloudFront-Viewer-Address, which the API records in audit rows.
        assert (
            api_behaviour["originRequestPolicyId"]
            == "origin-policy-Managed-AllViewerAndCloudFrontHeaders-2022-06"
        )
        assert api_behaviour["viewerProtocolPolicy"] == "https-only"

        origin = _one("aws:cloudfront/vpcOrigin:VpcOrigin", "e1-edge-api-origin")
        endpoint = origin["vpcOriginEndpointConfig"]
        assert endpoint["originProtocolPolicy"] == "https-only"
        assert endpoint["originSslProtocols"] == {"items": ["TLSv1.2"], "quantity": 1}

    return _ready(edge).apply(check)


@pulumi.runtime.test
def test_load_balancer_admits_only_cloudfronts_vpc_origin_group() -> Any:
    """Owner condition 3: the service-managed group, not the origin-facing prefix list."""
    edge = _edge("e2")

    def check(_: list[Any]) -> None:
        rule = _one(
            "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule", "e2-edge-alb-in-cloudfront"
        )
        assert (rule["fromPort"], rule["toPort"], rule["ipProtocol"]) == (443, 443, "tcp")
        assert rule["referencedSecurityGroupId"] == CLOUDFRONT_VPC_ORIGIN_SG
        assert "prefixListId" not in rule and "cidrIpv4" not in rule
        lookups = [a for t, a in CALLS if t == "aws:ec2/getSecurityGroup:getSecurityGroup"]
        assert lookups, "the group is looked up"
        assert lookups[-1]["filters"] == [
            {"name": "group-name", "values": [CLOUDFRONT_VPC_ORIGIN_GROUP]}
        ]
        assert lookups[-1]["vpcId"] == "e2-vpc-id"
        no_prefix_lists = [
            n
            for n, i in registered("aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule")
            if n.startswith("e2-") and "prefixListId" in i
        ]
        assert no_prefix_lists == []

    return _ready(edge).apply(check)


@pulumi.runtime.test
def test_security_headers_and_waf() -> Any:
    edge = _edge("e3")

    def check(_: list[Any]) -> None:
        policy = _one(
            "aws:cloudfront/responseHeadersPolicy:ResponseHeadersPolicy", "e3-edge-headers"
        )
        headers = policy["securityHeadersConfig"]
        hsts = headers["strictTransportSecurity"]
        # Owner condition 5: HSTS without preload.
        assert hsts["preload"] is False
        assert hsts["includeSubdomains"] is True
        assert hsts["accessControlMaxAgeSec"] >= 31_536_000
        assert headers["frameOptions"]["frameOption"] == "DENY"
        csp = headers["contentSecurityPolicy"]["contentSecurityPolicy"]
        assert "script-src 'self';" in csp
        assert "frame-ancestors 'none'" in csp
        assert f"https://cognito-idp.{REGION}.amazonaws.com" in csp

        waf = _one("aws:wafv2/webAcl:WebAcl", "e3-edge-waf")
        assert waf["scope"] == "CLOUDFRONT"
        groups = [
            r["statement"]["managedRuleGroupStatement"]["name"]
            for r in waf["rules"]
            if "managedRuleGroupStatement" in r["statement"]
        ]
        assert groups == ["AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet"]
        [rate] = [r for r in waf["rules"] if "rateBasedStatement" in r["statement"]]
        assert rate["statement"]["rateBasedStatement"]["limit"] == 1000
        assert "block" in rate["action"]
        logging = _one(
            "aws:wafv2/webAclLoggingConfiguration:WebAclLoggingConfiguration", "e3-edge-waf-logging"
        )
        assert logging["redactedFields"] == [{"singleHeader": {"name": "authorization"}}]

    return _ready(edge).apply(check)


@pulumi.runtime.test
def test_portal_bucket_is_private_and_read_only_by_the_distribution() -> Any:
    edge = _edge("e4")

    def check(_: list[Any]) -> None:
        block = _one(
            "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock", "e4-edge-portal-public-access"
        )
        assert all(
            block[k]
            for k in (
                "blockPublicAcls",
                "blockPublicPolicy",
                "ignorePublicAcls",
                "restrictPublicBuckets",
            )
        )
        policy = json.loads(
            _one("aws:s3/bucketPolicy:BucketPolicy", "e4-edge-portal-policy")["policy"]
        )
        read, tls = policy["Statement"]
        assert read["Principal"] == {"Service": "cloudfront.amazonaws.com"}
        assert read["Action"] == "s3:GetObject"
        assert read["Condition"]["StringEquals"]["AWS:SourceArn"].endswith("e4-edge-cdn")
        assert tls["Effect"] == "Deny"

        config = json.loads(
            _one("aws:s3/bucketObjectv2:BucketObjectv2", "e4-edge-portal-config")["content"]
        )
        assert config["hosts"] == {"tenant": DNS.tenant_host, "admin": DNS.admin_host}
        assert set(config["pools"]) == {"tenant", "platform"}
        assert config["pools"]["tenant"]["loginUrl"].startswith("https://t-login.auth.")
        assert "secret" not in json.dumps(config).lower()

        records = sorted(
            (i["name"], i["type"])
            for n, i in registered("aws:route53/record:Record")
            if n.startswith("e4-edge-")
        )
        assert records == sorted((h, t) for h in DNS.hosts for t in ("A", "AAAA"))

        logs = json.loads(_one("aws:s3/bucketPolicy:BucketPolicy", "e4-edge-logs-policy")["policy"])
        alb = next(s for s in logs["Statement"] if s["Sid"] == "LoadBalancerLogs")
        assert alb["Principal"] == {"AWS": f"arn:aws:iam::{ELB_ACCOUNT_ID}:root"}
        assert alb["Resource"].endswith(f"/alb/AWSLogs/{ACCOUNT_ID}/*")

        build = _one("command:local:Command", "e4-edge-portal-build")
        assert build["create"] == "scripts/build_portal.py"

    return _ready(edge).apply(check)


def test_csp_allows_scripts_only_from_the_portal() -> None:
    csp = content_security_policy(region="us-east-1", login_domains=["b", "a"])
    directives = dict(d.split(" ", 1) for d in csp.split("; "))
    assert directives["script-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    # Build step 6: presigned upload (and, from step 7, view) links to tenant buckets, by
    # their regional virtual-hosted names (owner decision 11).
    assert directives["connect-src"] == (
        "'self' https://cognito-idp.us-east-1.amazonaws.com "
        "https://a.auth.us-east-1.amazoncognito.com https://b.auth.us-east-1.amazoncognito.com "
        "https://*.s3.us-east-1.amazonaws.com"
    )


def test_portal_config_lists_both_pools() -> None:
    config = json.loads(
        portal_config(
            region="us-east-1",
            tenant_host="t.example.test",
            admin_host="admin.t.example.test",
            tenant_pool="p1",
            tenant_client="c1",
            tenant_domain="d1",
            platform_pool="p2",
            platform_client="c2",
            platform_domain="d2",
            idle_timeout_minutes=30,
        )
    )
    assert config["idleTimeoutMinutes"] == 30
    assert config["pools"]["platform"] == {
        "userPoolId": "p2",
        "clientId": "c2",
        "loginUrl": "https://d2.auth.us-east-1.amazoncognito.com",
    }


def test_the_portal_idle_timeout_is_checked() -> None:
    from assay_infra.config import EdgeConfig, validate_edge

    assert validate_edge(EdgeConfig(waf_rate_limit=1000, portal_idle_minutes=30))
    for minutes in (4, 481):
        with pytest.raises(ValueError, match="portalIdleTimeoutMinutes"):
            validate_edge(EdgeConfig(waf_rate_limit=1000, portal_idle_minutes=minutes))


def test_the_dev_stack_sets_a_60_minute_idle_timeout() -> None:
    # 60 for now (owner, 2026-11-24); the decision was 30. The portal's fallback stays 30.
    stack = (Path(__file__).resolve().parents[2] / "infra" / "Pulumi.dev.yaml").read_text()
    assert 'assay-platform:portalIdleTimeoutMinutes: "60"' in stack


def test_portal_source_hash_ignores_build_output(tmp_path: Path) -> None:
    (tmp_path / "portal" / "src").mkdir(parents=True)
    (tmp_path / "assets").mkdir()
    (tmp_path / "portal" / "src" / "a.ts").write_text("a")
    before = portal_source_hash(tmp_path)
    (tmp_path / "portal" / "node_modules").mkdir()
    (tmp_path / "portal" / "node_modules" / "x.js").write_text("x")
    (tmp_path / "portal" / "dist").mkdir()
    (tmp_path / "portal" / "dist" / "index.html").write_text("x")
    assert portal_source_hash(tmp_path) == before
    (tmp_path / "portal" / "src" / "a.ts").write_text("b")
    assert portal_source_hash(tmp_path) != before
