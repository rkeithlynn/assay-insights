"""The edge: portal bucket, CloudFront, AWS WAF, DNS records and logs (section 4, item 7).

One CloudFront distribution serves both portal hosts:

- the default behaviour serves the portal bucket through origin access control, with a
  viewer-request function that sends client-side routes to `/index.html`;
- `/api/*` goes to the internal API load balancer through a CloudFront VPC origin, over
  HTTPS, with caching disabled and every viewer header forwarded (Host included, so the
  load balancer's certificate, which covers both hosts, matches the name CloudFront checks;
  and CloudFront-Viewer-Address, which the API records in audit rows).

The load balancer accepts HTTPS only from CloudFront's service-managed security group for
VPC origins (`CloudFront-VPCOrigins-Service-SG`), which CloudFront creates with the first VPC
origin in the VPC. That group is looked up after the VPC origin exists, so a preview shows
the rule with an unknown source.

AWS WAF (scope CLOUDFRONT) runs the managed common and known-bad-inputs rule groups and a
per-IP rate limit. A response headers policy sets HSTS (without preload), a content security
policy, and the usual hardening headers on every response.

Logs: CloudFront access logs (v2, through CloudWatch Logs delivery) and the load balancer's
access logs go to one logging bucket (SSE-S3, which load balancer logs require); WAF logs go
to CloudWatch Logs with the Authorization header redacted.

The portal itself is built in a Node container and uploaded by `scripts/build_portal.py`,
run by a Pulumi command whenever the portal's sources change. `config.json` (pool and client
IDs, login domains, hosts) is written by this program, so the portal holds no identifiers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_command as command

from assay_infra.auth import Auth
from assay_infra.config import DnsConfig
from assay_infra.network import Network

PORTAL_BUILD_SCRIPT = "scripts/build_portal.py"
PORTAL_DIR = "portal"
# Never part of the portal's source hash.
PORTAL_IGNORED = frozenset({"node_modules", "dist", ".vite", "coverage"})
CONFIG_KEY = "config.json"

CLOUDFRONT_VPC_ORIGIN_GROUP = "CloudFront-VPCOrigins-Service-SG"
LOGS_EXPIRE_DAYS = 90
LOG_RETENTION_DAYS = 30
HSTS_MAX_AGE_SECONDS = 63_072_000  # two years
TLS_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"
VIEWER_TLS = "TLSv1.2_2021"

CACHE_OPTIMIZED = "Managed-CachingOptimized"
CACHE_DISABLED = "Managed-CachingDisabled"
ALL_VIEWER_AND_CLOUDFRONT_HEADERS = "Managed-AllViewerAndCloudFrontHeaders-2022-06"

# Paths with a file extension are files in the bucket; anything else is a portal route.
SPA_FUNCTION = """function handler(event) {
  var request = event.request;
  var last = request.uri.substring(request.uri.lastIndexOf('/') + 1);
  if (last.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
"""

MANAGED_RULE_GROUPS = ("AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet")


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def portal_source_hash(repo_root: Path) -> str:
    """Changes exactly when a file the portal build reads changes."""
    digest = hashlib.sha256()
    root = repo_root / PORTAL_DIR
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or PORTAL_IGNORED.intersection(rel.parts):
            continue
        digest.update(rel.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for asset in sorted((repo_root / "assets").glob("*.svg")):
        digest.update(asset.name.encode())
        digest.update(asset.read_bytes())
    return f"portal-{digest.hexdigest()[:16]}"


def portal_config(
    *,
    region: str,
    tenant_host: str,
    admin_host: str,
    tenant_pool: str,
    tenant_client: str,
    tenant_domain: str,
    platform_pool: str,
    platform_client: str,
    platform_domain: str,
) -> str:
    """The portal's runtime configuration. The host it is served on picks the pool."""

    def pool(pool_id: str, client: str, domain: str) -> dict[str, str]:
        return {
            "userPoolId": pool_id,
            "clientId": client,
            "loginUrl": f"https://{domain}.auth.{region}.amazoncognito.com",
        }

    return json.dumps(
        {
            "region": region,
            "hosts": {"tenant": tenant_host, "admin": admin_host},
            "pools": {
                "tenant": pool(tenant_pool, tenant_client, tenant_domain),
                "platform": pool(platform_pool, platform_client, platform_domain),
            },
        },
        indent=2,
        sort_keys=True,
    )


def content_security_policy(*, region: str, login_domains: Iterable[str]) -> str:
    """Scripts from the portal only. The browser talks to the API (same origin), to the
    managed login token endpoints, and to Cognito's user API (change password, MFA setup).
    """
    connect = [
        "'self'",
        f"https://cognito-idp.{region}.amazonaws.com",
        *sorted(f"https://{d}.auth.{region}.amazoncognito.com" for d in login_domains),
    ]
    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        # Cloudscape sets inline style attributes on some components.
        "style-src": ["'self'", "'unsafe-inline'"],
        "img-src": ["'self'", "data:"],
        "font-src": ["'self'", "data:"],
        "connect-src": connect,
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
    return "; ".join(f"{k} {' '.join(v)}" for k, v in directives.items())


def portal_bucket_policy(*, bucket: str, distribution: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "CloudFrontReadsThePortal",
                    "Effect": "Allow",
                    "Principal": {"Service": "cloudfront.amazonaws.com"},
                    "Action": "s3:GetObject",
                    "Resource": f"{bucket}/*",
                    "Condition": {"StringEquals": {"AWS:SourceArn": distribution}},
                },
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [bucket, f"{bucket}/*"],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
            ],
        }
    )


def log_bucket_policy(*, bucket: str, account_id: str, region: str, elb_account: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    # Load balancer access logs. This Region delivers them from the Elastic
                    # Load Balancing account (aws.elb.get_service_account), not a service.
                    "Sid": "LoadBalancerLogs",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{elb_account}:root"},
                    "Action": "s3:PutObject",
                    "Resource": f"{bucket}/alb/AWSLogs/{account_id}/*",
                },
                {
                    # CloudFront access logs (v2), delivered by CloudWatch Logs under
                    # AWSLogs/<account>/CloudFront/.
                    "Sid": "CloudFrontLogs",
                    "Effect": "Allow",
                    "Principal": {"Service": "delivery.logs.amazonaws.com"},
                    "Action": "s3:PutObject",
                    "Resource": f"{bucket}/AWSLogs/{account_id}/*",
                    "Condition": {
                        "StringEquals": {
                            "aws:SourceAccount": account_id,
                            "s3:x-amz-acl": "bucket-owner-full-control",
                        },
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:logs:{region}:{account_id}:delivery-source:*"
                        },
                    },
                },
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [bucket, f"{bucket}/*"],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
            ],
        }
    )


def _private_bucket(
    name: str, parent: pulumi.Resource, *, expire_days: int | None = None
) -> tuple[aws.s3.Bucket, list[pulumi.Resource]]:
    """A bucket with public access blocked, ACLs off, SSE-S3 and (optionally) expiry."""
    child = pulumi.ResourceOptions(parent=parent)
    bucket = aws.s3.Bucket(name, force_destroy=False, opts=child)
    settings: list[pulumi.Resource] = [
        aws.s3.BucketOwnershipControls(
            f"{name}-ownership",
            bucket=bucket.id,
            rule=aws.s3.BucketOwnershipControlsRuleArgs(object_ownership="BucketOwnerEnforced"),
            opts=child,
        ),
        aws.s3.BucketPublicAccessBlock(
            f"{name}-public-access",
            bucket=bucket.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=child,
        ),
        aws.s3.BucketVersioning(
            f"{name}-versioning",
            bucket=bucket.id,
            versioning_configuration=aws.s3.BucketVersioningVersioningConfigurationArgs(
                status="Enabled"
            ),
            opts=child,
        ),
    ]
    if expire_days is not None:
        settings.append(
            aws.s3.BucketLifecycleConfiguration(
                f"{name}-lifecycle",
                bucket=bucket.id,
                rules=[
                    aws.s3.BucketLifecycleConfigurationRuleArgs(
                        id="expire-logs",
                        status="Enabled",
                        filter=aws.s3.BucketLifecycleConfigurationRuleFilterArgs(prefix=""),
                        expiration=aws.s3.BucketLifecycleConfigurationRuleExpirationArgs(
                            days=expire_days
                        ),
                        noncurrent_version_expiration=aws.s3.BucketLifecycleConfigurationRuleNoncurrentVersionExpirationArgs(
                            noncurrent_days=7
                        ),
                    )
                ],
                opts=pulumi.ResourceOptions(parent=parent, depends_on=list(settings)),
            )
        )
    # Last of the settings, as for every bucket in this stack (BLOCKED_ENCRYPTION_TYPES).
    settings.append(
        aws.s3.BucketServerSideEncryptionConfiguration(
            f"{name}-encryption",
            bucket=bucket.id,
            rules=[
                aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                    apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                        sse_algorithm="AES256"
                    ),
                    blocked_encryption_types=["SSE-C"],
                )
            ],
            opts=pulumi.ResourceOptions(parent=parent, depends_on=list(settings)),
        )
    )
    return bucket, settings


class EdgeLogs(pulumi.ComponentResource):
    """The logging bucket. Exists before the load balancer, whose access logs it receives."""

    def __init__(
        self,
        name: str,
        *,
        region: str,
        account_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:edge:EdgeLogs", name, None, opts)
        self.bucket, settings = _private_bucket(name, self, expire_days=LOGS_EXPIRE_DAYS)
        elb_account = aws.elb.get_service_account_output().id
        self.policy = aws.s3.BucketPolicy(
            f"{name}-policy",
            bucket=self.bucket.id,
            policy=pulumi.Output.all(
                bucket=self.bucket.arn,
                account_id=pulumi.Output.from_input(account_id),
                elb_account=elb_account,
            ).apply(lambda a: log_bucket_policy(region=region, **a)),
            opts=pulumi.ResourceOptions(parent=self, depends_on=settings),
        )
        self.register_outputs({"bucket": self.bucket.bucket})


class Edge(pulumi.ComponentResource):
    def __init__(
        self,
        name: str,
        *,
        repo_root: Path,
        region: str,
        dns: DnsConfig,
        zone_id: pulumi.Input[str],
        certificate_arn: pulumi.Input[str],
        network: Network,
        auth: Auth,
        load_balancer: aws.lb.LoadBalancer,
        load_balancer_security_group: aws.ec2.SecurityGroup,
        logs: EdgeLogs,
        waf_rate_limit: int,
        aws_profile: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:edge:Edge", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # --- Portal bucket ----------------------------------------------------------------
        self.portal_bucket, portal_settings = _private_bucket(f"{name}-portal", self)
        self.origin_access_control = aws.cloudfront.OriginAccessControl(
            f"{name}-oac",
            name=f"{name}-portal",
            description="CloudFront reads the portal bucket",
            origin_access_control_origin_type="s3",
            signing_behavior="always",
            signing_protocol="sigv4",
            opts=child,
        )

        # --- API origin -------------------------------------------------------------------
        self.vpc_origin = aws.cloudfront.VpcOrigin(
            f"{name}-api-origin",
            vpc_origin_endpoint_config=aws.cloudfront.VpcOriginVpcOriginEndpointConfigArgs(
                name=f"{name}-api",
                arn=load_balancer.arn,
                http_port=80,
                https_port=443,
                origin_protocol_policy="https-only",
                origin_ssl_protocols=aws.cloudfront.VpcOriginVpcOriginEndpointConfigOriginSslProtocolsArgs(
                    items=["TLSv1.2"], quantity=1
                ),
            ),
            opts=child,
        )
        # The service-managed group exists once the VPC origin does. Its ID is unknown in a
        # preview (the lookup waits on the VPC origin's ID).
        cloudfront_group = pulumi.Output.all(self.vpc_origin.id, network.vpc.id).apply(
            lambda a: (
                aws.ec2.get_security_group(
                    vpc_id=a[1],
                    filters=[
                        aws.ec2.GetSecurityGroupFilterArgs(
                            name="group-name", values=[CLOUDFRONT_VPC_ORIGIN_GROUP]
                        )
                    ],
                ).id
            )
        )
        self.load_balancer_ingress = aws.vpc.SecurityGroupIngressRule(
            f"{name}-alb-in-cloudfront",
            security_group_id=load_balancer_security_group.id,
            # EC2 allows only a-zA-Z0-9. _-:/()#,@[]+=&;{}!$* here (no apostrophe).
            description="HTTPS from the CloudFront VPC origin only",
            ip_protocol="tcp",
            from_port=443,
            to_port=443,
            referenced_security_group_id=cloudfront_group,
            opts=child,
        )

        # --- Behaviours -------------------------------------------------------------------
        self.spa_function = aws.cloudfront.Function(
            f"{name}-spa",
            name=f"{name}-spa",
            runtime="cloudfront-js-2.0",
            comment="Portal routes to index.html",
            code=SPA_FUNCTION,
            publish=True,
            opts=child,
        )
        login_domains = pulumi.Output.all(auth.tenant.domain.domain, auth.platform.domain.domain)
        self.headers_policy = aws.cloudfront.ResponseHeadersPolicy(
            f"{name}-headers",
            name=f"{name}-headers",
            comment="Security headers for the portal and the API",
            security_headers_config=aws.cloudfront.ResponseHeadersPolicySecurityHeadersConfigArgs(
                strict_transport_security=aws.cloudfront.ResponseHeadersPolicySecurityHeadersConfigStrictTransportSecurityArgs(
                    access_control_max_age_sec=HSTS_MAX_AGE_SECONDS,
                    include_subdomains=True,
                    preload=False,
                    override=True,
                ),
                content_type_options=aws.cloudfront.ResponseHeadersPolicySecurityHeadersConfigContentTypeOptionsArgs(
                    override=True
                ),
                frame_options=aws.cloudfront.ResponseHeadersPolicySecurityHeadersConfigFrameOptionsArgs(
                    frame_option="DENY", override=True
                ),
                referrer_policy=aws.cloudfront.ResponseHeadersPolicySecurityHeadersConfigReferrerPolicyArgs(
                    referrer_policy="strict-origin-when-cross-origin", override=True
                ),
                content_security_policy=aws.cloudfront.ResponseHeadersPolicySecurityHeadersConfigContentSecurityPolicyArgs(
                    content_security_policy=login_domains.apply(
                        lambda d: content_security_policy(region=region, login_domains=d)
                    ),
                    override=True,
                ),
            ),
            custom_headers_config=aws.cloudfront.ResponseHeadersPolicyCustomHeadersConfigArgs(
                items=[
                    aws.cloudfront.ResponseHeadersPolicyCustomHeadersConfigItemArgs(
                        header="Permissions-Policy",
                        value="camera=(), microphone=(), geolocation=(), payment=()",
                        override=True,
                    )
                ]
            ),
            opts=child,
        )
        cache_optimized = aws.cloudfront.get_cache_policy_output(name=CACHE_OPTIMIZED).id
        cache_disabled = aws.cloudfront.get_cache_policy_output(name=CACHE_DISABLED).id
        forward_all = aws.cloudfront.get_origin_request_policy_output(
            name=ALL_VIEWER_AND_CLOUDFRONT_HEADERS
        ).id

        # --- WAF --------------------------------------------------------------------------
        def visibility(metric: str) -> aws.wafv2.WebAclRuleVisibilityConfigArgs:
            return aws.wafv2.WebAclRuleVisibilityConfigArgs(
                cloudwatch_metrics_enabled=True,
                metric_name=metric,
                sampled_requests_enabled=True,
            )

        rules: list[aws.wafv2.WebAclRuleArgs] = [
            aws.wafv2.WebAclRuleArgs(
                name=group,
                priority=priority,
                override_action=aws.wafv2.WebAclRuleOverrideActionArgs(
                    none=aws.wafv2.WebAclRuleOverrideActionNoneArgs()  # type: ignore[no-untyped-call]
                ),
                statement=aws.wafv2.WebAclRuleStatementArgs(
                    managed_rule_group_statement=aws.wafv2.WebAclRuleStatementManagedRuleGroupStatementArgs(
                        vendor_name="AWS", name=group
                    )
                ),
                visibility_config=visibility(group),
            )
            for priority, group in enumerate(MANAGED_RULE_GROUPS, start=1)
        ]
        rules.append(
            aws.wafv2.WebAclRuleArgs(
                name="RateLimitPerIp",
                priority=len(rules) + 1,
                action=aws.wafv2.WebAclRuleActionArgs(block=aws.wafv2.WebAclRuleActionBlockArgs()),
                statement=aws.wafv2.WebAclRuleStatementArgs(
                    rate_based_statement=aws.wafv2.WebAclRuleStatementRateBasedStatementArgs(
                        limit=waf_rate_limit, aggregate_key_type="IP", evaluation_window_sec=300
                    )
                ),
                visibility_config=visibility("RateLimitPerIp"),
            )
        )
        self.web_acl = aws.wafv2.WebAcl(
            f"{name}-waf",
            name=f"{name}-waf",
            description="Assay Insights portal and API",
            scope="CLOUDFRONT",
            default_action=aws.wafv2.WebAclDefaultActionArgs(
                allow=aws.wafv2.WebAclDefaultActionAllowArgs()
            ),
            rules=rules,
            visibility_config=aws.wafv2.WebAclVisibilityConfigArgs(
                cloudwatch_metrics_enabled=True,
                metric_name=f"{name}-waf",
                sampled_requests_enabled=True,
            ),
            opts=child,
        )
        # WAF requires this name prefix for a CloudWatch Logs destination.
        self.waf_log_group = aws.cloudwatch.LogGroup(
            f"{name}-waf-logs",
            name=f"aws-waf-logs-{name}",
            retention_in_days=LOG_RETENTION_DAYS,
            opts=child,
        )
        aws.wafv2.WebAclLoggingConfiguration(
            f"{name}-waf-logging",
            resource_arn=self.web_acl.arn,
            log_destination_configs=[self.waf_log_group.arn],
            redacted_fields=[
                aws.wafv2.WebAclLoggingConfigurationRedactedFieldArgs(
                    single_header=aws.wafv2.WebAclLoggingConfigurationRedactedFieldSingleHeaderArgs(
                        name="authorization"
                    )
                )
            ],
            opts=child,
        )

        # --- Distribution -----------------------------------------------------------------
        portal_origin = "portal"
        api_origin = "api"
        self.distribution = aws.cloudfront.Distribution(
            f"{name}-cdn",
            comment="Assay Insights portal and API",
            enabled=True,
            aliases=dns.hosts,
            default_root_object="index.html",
            http_version="http2and3",
            is_ipv6_enabled=True,
            price_class="PriceClass_100",
            web_acl_id=self.web_acl.arn,
            viewer_certificate=aws.cloudfront.DistributionViewerCertificateArgs(
                acm_certificate_arn=certificate_arn,
                ssl_support_method="sni-only",
                minimum_protocol_version=VIEWER_TLS,
            ),
            origins=[
                aws.cloudfront.DistributionOriginArgs(
                    origin_id=portal_origin,
                    domain_name=self.portal_bucket.bucket_regional_domain_name,
                    origin_access_control_id=self.origin_access_control.id,
                    s3_origin_config=aws.cloudfront.DistributionOriginS3OriginConfigArgs(
                        origin_access_identity=""
                    ),
                ),
                aws.cloudfront.DistributionOriginArgs(
                    origin_id=api_origin,
                    domain_name=load_balancer.dns_name,
                    vpc_origin_config=aws.cloudfront.DistributionOriginVpcOriginConfigArgs(
                        vpc_origin_id=self.vpc_origin.id,
                        origin_keepalive_timeout=5,
                        origin_read_timeout=60,
                    ),
                ),
            ],
            default_cache_behavior=aws.cloudfront.DistributionDefaultCacheBehaviorArgs(
                target_origin_id=portal_origin,
                viewer_protocol_policy="redirect-to-https",
                allowed_methods=["GET", "HEAD", "OPTIONS"],
                cached_methods=["GET", "HEAD"],
                compress=True,
                cache_policy_id=cache_optimized,
                response_headers_policy_id=self.headers_policy.id,
                function_associations=[
                    aws.cloudfront.DistributionDefaultCacheBehaviorFunctionAssociationArgs(
                        event_type="viewer-request", function_arn=self.spa_function.arn
                    )
                ],
            ),
            ordered_cache_behaviors=[
                aws.cloudfront.DistributionOrderedCacheBehaviorArgs(
                    path_pattern="/api/*",
                    target_origin_id=api_origin,
                    viewer_protocol_policy="https-only",
                    allowed_methods=["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                    cached_methods=["GET", "HEAD"],
                    compress=True,
                    cache_policy_id=cache_disabled,
                    origin_request_policy_id=forward_all,
                    response_headers_policy_id=self.headers_policy.id,
                )
            ],
            restrictions=aws.cloudfront.DistributionRestrictionsArgs(
                geo_restriction=aws.cloudfront.DistributionRestrictionsGeoRestrictionArgs(
                    restriction_type="none"
                )
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[self.load_balancer_ingress]),
        )
        self.portal_policy = aws.s3.BucketPolicy(
            f"{name}-portal-policy",
            bucket=self.portal_bucket.id,
            policy=pulumi.Output.all(
                bucket=self.portal_bucket.arn, distribution=self.distribution.arn
            ).apply(lambda a: portal_bucket_policy(**a)),
            opts=pulumi.ResourceOptions(parent=self, depends_on=portal_settings),
        )

        # --- CloudFront access logs (v2) ----------------------------------------------------
        log_source = aws.cloudwatch.LogDeliverySource(
            f"{name}-cdn-log-source",
            name=f"{name}-cdn",
            log_type="ACCESS_LOGS",
            resource_arn=self.distribution.arn,
            opts=child,
        )
        log_destination = aws.cloudwatch.LogDeliveryDestination(
            f"{name}-cdn-log-destination",
            name=f"{name}-cdn-s3",
            output_format="json",
            delivery_destination_configuration=aws.cloudwatch.LogDeliveryDestinationDeliveryDestinationConfigurationArgs(
                destination_resource_arn=logs.bucket.arn
            ),
            opts=pulumi.ResourceOptions(parent=self, depends_on=[logs.policy]),
        )
        aws.cloudwatch.LogDelivery(
            f"{name}-cdn-logs",
            delivery_source_name=log_source.name,
            delivery_destination_arn=log_destination.arn,
            opts=child,
        )

        # --- DNS --------------------------------------------------------------------------
        self.records = [
            aws.route53.Record(
                f"{name}-{host.replace('.', '-')}-{record_type.lower()}",
                zone_id=zone_id,
                name=host,
                type=record_type,
                aliases=[
                    aws.route53.RecordAliasArgs(
                        name=self.distribution.domain_name,
                        zone_id=self.distribution.hosted_zone_id,
                        evaluate_target_health=False,
                    )
                ],
                opts=child,
            )
            for host in dns.hosts
            for record_type in ("A", "AAAA")
        ]

        # --- Portal files -------------------------------------------------------------------
        self.config_object = aws.s3.BucketObjectv2(
            f"{name}-portal-config",
            bucket=self.portal_bucket.id,
            key=CONFIG_KEY,
            content=pulumi.Output.all(
                tenant_pool=auth.tenant.pool.id,
                tenant_client=auth.tenant.client.id,
                tenant_domain=auth.tenant.domain.domain,
                platform_pool=auth.platform.pool.id,
                platform_client=auth.platform.client.id,
                platform_domain=auth.platform.domain.domain,
            ).apply(
                lambda a: portal_config(
                    region=region, tenant_host=dns.tenant_host, admin_host=dns.admin_host, **a
                )
            ),
            content_type="application/json",
            cache_control="no-cache",
            opts=pulumi.ResourceOptions(parent=self, depends_on=portal_settings),
        )
        environment: dict[str, pulumi.Input[str]] = {
            "PORTAL_BUCKET": self.portal_bucket.bucket,
            "DISTRIBUTION_ID": self.distribution.id,
            "PORTAL_CONTEXT": repo_root.as_posix(),
            "AWS_REGION": region,
        }
        if aws_profile:
            environment["AWS_PROFILE"] = aws_profile
        source = portal_source_hash(repo_root)
        self.portal_build = command.local.Command(
            f"{name}-portal-build",
            dir=repo_root.as_posix(),
            interpreter=["uv", "run", "--frozen", "python"],
            create=PORTAL_BUILD_SCRIPT,
            environment=environment,
            triggers=[self.portal_bucket.bucket, source],
            logging=command.local.Logging.STDERR,
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[*portal_settings, self.config_object]
            ),
        )

        self.register_outputs(
            {
                "distribution_id": self.distribution.id,
                "distribution_domain": self.distribution.domain_name,
                "portal_bucket": self.portal_bucket.bucket,
            }
        )
