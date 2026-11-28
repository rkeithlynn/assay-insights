"""Stack configuration.

Every account ID, hostname, email address and model ID comes from stack config
(`Pulumi.<stack>.yaml`), never from code.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import pulumi


@dataclass(frozen=True)
class DnsConfig:
    hosted_zone: str
    tenant_host: str
    admin_host: str

    @property
    def hosts(self) -> list[str]:
        return [self.tenant_host, self.admin_host]


@dataclass(frozen=True)
class NetworkConfig:
    vpc_cidr: str
    # Availability Zone IDs (for example use1-az2), not names: names are shuffled per account.
    workload_az_id: str
    second_az_id: str


@dataclass(frozen=True)
class DatabaseConfig:
    engine_version: str
    database_name: str
    min_acu: float
    max_acu: float
    auto_pause_seconds: int

    @property
    def major_version(self) -> str:
        return self.engine_version.split(".", 1)[0]


@dataclass(frozen=True)
class AuthConfig:
    # Cognito prefix domains (<prefix>.auth.<region>.amazoncognito.com) for managed login.
    # A custom domain under the hosted zone needs an A record on its parent, which exists
    # only once CloudFront serves the portal hosts.
    tenant_domain_prefix: str
    platform_domain_prefix: str
    # Refresh-token rotation, per app client (stack config tenantRefreshTokenRotation and
    # platformRefreshTokenRotation; owner, 2026-11-25). True: rotation on, and the client's
    # only explicit flow is ALLOW_CUSTOM_AUTH. False: rotation explicitly off, and the
    # refresh flow back (the way back if sign-in breaks). None (unset): the client is left
    # as it was before rotation existed here.
    tenant_refresh_rotation: bool | None = None
    platform_refresh_rotation: bool | None = None


@dataclass(frozen=True)
class AlertsConfig:
    # Where alarm notifications are emailed. The owner confirms the subscription.
    email: str
    # Where security notices (a quarantined upload) are emailed, on their own topic. Blank
    # until the owner sets it; the topic and its subscription arrive with the notice itself
    # (build step 6, second half, part 2).
    security_notice_email: str = ""


@dataclass(frozen=True)
class PipelineConfig:
    # The most worker tasks one tenant's documents may have running at once (owner decision,
    # 2026-11-24). The account's Fargate vCPU quota is the ceiling across tenants.
    worker_max_per_tenant: int = 5
    # Lambda reserved concurrency (owner decision, 2026-11-25; stack config
    # scanReservedConcurrency and tssReservedConcurrency). Reserved concurrency is both a
    # cap and a guarantee. The scan function is capped because uploads drive it and its
    # overflow is safe (Lambda queues scan results and retries them). The TSS is guaranteed
    # its share so that no burst of other functions can leave it without one. None: not
    # set, which is the only choice in an account whose quota is still too low (Lambda
    # keeps 100 unreserved, and a new account allows 10 in all): raise the quota first.
    scan_reserved_concurrency: int | None = None
    tss_reserved_concurrency: int | None = None


@dataclass(frozen=True)
class TextConfig:
    """Step 5, text (build step 6, second half, stage 2). Every value is the owner's, from
    stack config: none has a default here, and a stack without one fails its preview."""

    # A page whose quality score is below this goes to Textract (`textQualityThreshold`).
    quality_threshold: str
    # A document with more pages than this is not read at all (`textMaxPages`).
    max_pages: int
    # What one Textract DetectDocumentText page costs, in US dollars
    # (`textractPricePerPage`). Recorded with every call (ai_usage.unit_price).
    textract_price_per_page: str
    # The spend guard: an alarm when the account's Textract pages in a day pass this
    # (`textractDailyPagesAlarm`).
    textract_daily_pages_alarm: int


@dataclass(frozen=True)
class TenancyConfig:
    # The only principal that may assume the break-glass role (an IAM role ARN).
    break_glass_principal_arn: str
    # Dimensions of the embedding model's vectors; fixed when a tenant's index is created.
    vector_dimensions: int
    # Deploy the in-VPC isolation test runner (tests/isolation). Development stacks only.
    isolation_tests: bool
    # The GuardDuty scan role reads and tags objects under inbox/ only, the prefix each
    # tenant's plan scans (stack config malwareScanInboxOnly; owner, 2026-11-27). False or
    # unset: the whole bucket, as AWS's page writes the role. The way back is "false".
    scan_inbox_only: bool
    # The provider's default tags, repeated on every tenant resource.
    default_tags: dict[str, str]


@dataclass(frozen=True)
class EdgeConfig:
    # AWS WAF rate-based rule: requests per IP address in any 5-minute window.
    waf_rate_limit: int
    # The portal signs a user out after this many minutes without activity in any of its
    # tabs (owner decision, 2026-11-24). Both hosts; written to config.json.
    portal_idle_minutes: int = 30


@dataclass(frozen=True)
class ModelsConfig:
    # Bedrock inference profile IDs of the models every new tenant starts with (section 7b).
    default_ingest_model: str
    default_query_model: str
    # Every profile ID in the catalog (aiModels), which scripts/seed.py loads.
    catalog_profiles: tuple[str, ...]


@dataclass(frozen=True)
class PlatformConfig:
    region: str
    # Prefix for physical resource names, for example "assay-dev".
    name_prefix: str
    dns: DnsConfig
    network: NetworkConfig
    database: DatabaseConfig
    auth: AuthConfig
    tenancy: TenancyConfig
    alerts: AlertsConfig
    edge: EdgeConfig
    models: ModelsConfig
    text: TextConfig
    pipeline: PipelineConfig = PipelineConfig()


def _within_zone(host: str, zone: str) -> bool:
    return host == zone or host.endswith(f".{zone}")


def build_dns(hosted_zone: str, tenant_host: str, admin_host: str) -> DnsConfig:
    cfg = DnsConfig(
        hosted_zone=hosted_zone.lower().rstrip("."),
        tenant_host=tenant_host.lower().rstrip("."),
        admin_host=admin_host.lower().rstrip("."),
    )
    for host in cfg.hosts:
        if not _within_zone(host, cfg.hosted_zone):
            raise ValueError(f"host {host!r} is not inside hosted zone {cfg.hosted_zone!r}")
    if cfg.tenant_host == cfg.admin_host:
        raise ValueError("tenantHost and adminHost must differ")
    return cfg


def validate_network(cfg: NetworkConfig) -> NetworkConfig:
    net = ipaddress.ip_network(cfg.vpc_cidr)
    if not net.is_private:
        raise ValueError(f"vpcCidr {cfg.vpc_cidr!r} must be a private range")
    if net.prefixlen > 18:
        raise ValueError(f"vpcCidr {cfg.vpc_cidr!r} must be /18 or larger")
    if cfg.workload_az_id == cfg.second_az_id:
        raise ValueError("workloadAzId and secondAzId must differ")
    return cfg


def validate_database(cfg: DatabaseConfig) -> DatabaseConfig:
    if not 0 <= cfg.min_acu <= cfg.max_acu:
        raise ValueError("dbMinAcu must be between 0 and dbMaxAcu")
    if cfg.min_acu == 0 and not 300 <= cfg.auto_pause_seconds <= 86400:
        raise ValueError("dbAutoPauseSeconds must be between 300 and 86400")
    if not cfg.database_name.isidentifier() or cfg.database_name.lower() != cfg.database_name:
        raise ValueError("dbName must be a lower-case identifier")
    return cfg


_DOMAIN_PREFIX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_auth(cfg: AuthConfig) -> AuthConfig:
    for prefix in (cfg.tenant_domain_prefix, cfg.platform_domain_prefix):
        if not _DOMAIN_PREFIX.match(prefix):
            raise ValueError(f"Cognito domain prefix {prefix!r} must be lower-case DNS label")
        # Cognito rejects prefixes containing these words.
        if any(word in prefix for word in ("aws", "amazon", "cognito")):
            raise ValueError(f"Cognito domain prefix {prefix!r} may not contain aws/amazon/cognito")
    if cfg.tenant_domain_prefix == cfg.platform_domain_prefix:
        raise ValueError("tenantAuthDomainPrefix and platformAuthDomainPrefix must differ")
    return cfg


_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$")
# Amazon Titan Text Embeddings V2 produces 256, 512 or 1024 dimensions.
VECTOR_DIMENSIONS = (256, 512, 1024)


_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


def validate_alerts(cfg: AlertsConfig) -> AlertsConfig:
    if not _EMAIL.match(cfg.email):
        raise ValueError("alarmEmail must be an email address")
    if cfg.security_notice_email and not _EMAIL.match(cfg.security_notice_email):
        raise ValueError("securityNoticeEmail must be an email address, or blank")
    return cfg


WORKER_MAX_PER_TENANT_RANGE = (1, 50)


# A reservation of 0 would stop the function altogether; the upper end leaves Lambda's own
# 100 unreserved in an account that allows 1,000.
RESERVED_CONCURRENCY_RANGE = (1, 900)


def validate_pipeline(cfg: PipelineConfig) -> PipelineConfig:
    low, high = WORKER_MAX_PER_TENANT_RANGE
    if not low <= cfg.worker_max_per_tenant <= high:
        raise ValueError(f"workerMaxPerTenant must be between {low} and {high}")
    low, high = RESERVED_CONCURRENCY_RANGE
    scan, tss = cfg.scan_reserved_concurrency, cfg.tss_reserved_concurrency
    for key, value in (("scanReservedConcurrency", scan), ("tssReservedConcurrency", tss)):
        if value is not None and not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}, or unset")
    if (scan is None) != (tss is None):
        # The scan function's cap alone would leave the TSS unprotected, and the TSS's
        # reservation alone would leave the scan function able to take everything else.
        raise ValueError("set scanReservedConcurrency and tssReservedConcurrency together")
    if scan is not None and tss is not None and tss < scan:
        # Every running scan function may be waiting for a TSS call at the same moment.
        raise ValueError("tssReservedConcurrency must be at least scanReservedConcurrency")
    return cfg


# The worker task has 30 minutes: more pages than this could not be read in them.
TEXT_MAX_PAGES_RANGE = (1, 3000)
TEXTRACT_DAILY_PAGES_RANGE = (1, 10_000_000)


def validate_text(cfg: TextConfig) -> TextConfig:
    try:
        threshold = Decimal(cfg.quality_threshold)
    except InvalidOperation:
        raise ValueError("textQualityThreshold must be a number") from None
    if not threshold.is_finite() or not 0 < threshold <= 1:
        raise ValueError("textQualityThreshold must be above 0 and at most 1")
    low, high = TEXT_MAX_PAGES_RANGE
    if not low <= cfg.max_pages <= high:
        raise ValueError(f"textMaxPages must be between {low} and {high}")
    if not cfg.textract_price_per_page.strip():
        raise ValueError(
            "textractPricePerPage is not set: enter what one Textract DetectDocumentText page "
            "costs, in US dollars"
        )
    try:
        price = Decimal(cfg.textract_price_per_page)
    except InvalidOperation:
        raise ValueError("textractPricePerPage must be a number") from None
    if not price.is_finite() or price < 0:
        raise ValueError("textractPricePerPage must be zero or more")
    low, high = TEXTRACT_DAILY_PAGES_RANGE
    if not low <= cfg.textract_daily_pages_alarm <= high:
        raise ValueError(f"textractDailyPagesAlarm must be between {low} and {high}")
    return cfg


# AWS WAF's lowest rate limit is 10; anything under 100 would lock out a normal user.
WAF_RATE_LIMIT_RANGE = (100, 20_000_000)
# Longer than the 2-minute warning; shorter than the 8-hour refresh token.
PORTAL_IDLE_MINUTES_RANGE = (5, 480)
_PROFILE_ID = re.compile(r"^us\.[a-z0-9][a-z0-9.:-]{2,200}$")


def validate_edge(cfg: EdgeConfig) -> EdgeConfig:
    low, high = WAF_RATE_LIMIT_RANGE
    if not low <= cfg.waf_rate_limit <= high:
        raise ValueError(f"wafRateLimit must be between {low} and {high}")
    low, high = PORTAL_IDLE_MINUTES_RANGE
    if not low <= cfg.portal_idle_minutes <= high:
        raise ValueError(f"portalIdleTimeoutMinutes must be between {low} and {high}")
    return cfg


def build_models(raw_catalog: object, default_ingest: str, default_query: str) -> ModelsConfig:
    """Only `us.` inference profiles (section 2), and the defaults must be in the catalog."""
    if not isinstance(raw_catalog, list) or not raw_catalog:
        raise ValueError("aiModels must be a non-empty list")
    profiles: list[str] = []
    for entry in raw_catalog:
        profile = str(entry.get("profileId", "")) if isinstance(entry, dict) else ""
        if not _PROFILE_ID.match(profile):
            raise ValueError(f"aiModels profileId {profile!r} must be a us. inference profile")
        profiles.append(profile)
    if len(set(profiles)) != len(profiles):
        raise ValueError("aiModels lists a profileId twice")
    for name, value in (
        ("defaultIngestModel", default_ingest),
        ("defaultQueryModel", default_query),
    ):
        if value not in profiles:
            raise ValueError(f"{name} must be one of the aiModels profile IDs")
    return ModelsConfig(
        default_ingest_model=default_ingest,
        default_query_model=default_query,
        catalog_profiles=tuple(profiles),
    )


def validate_tenancy(cfg: TenancyConfig) -> TenancyConfig:
    if not _ROLE_ARN.match(cfg.break_glass_principal_arn):
        raise ValueError("breakGlassPrincipalArn must be an IAM role ARN")
    if cfg.vector_dimensions not in VECTOR_DIMENSIONS:
        raise ValueError(f"vectorDimensions must be one of {VECTOR_DIMENSIONS}")
    if not cfg.default_tags:
        # The provisioning role's policy tells platform resources from new ones by a tag
        # that every platform resource carries.
        raise ValueError("aws:defaultTags must set at least one tag")
    return cfg


def load_config() -> PlatformConfig:
    conf = pulumi.Config()
    default_tags = (pulumi.Config("aws").get_object("defaultTags") or {}).get("tags") or {}
    return PlatformConfig(
        region=pulumi.Config("aws").require("region"),
        name_prefix=f"assay-{pulumi.get_stack()}",
        dns=build_dns(
            hosted_zone=conf.require("hostedZone"),
            tenant_host=conf.require("tenantHost"),
            admin_host=conf.require("adminHost"),
        ),
        network=validate_network(
            NetworkConfig(
                vpc_cidr=conf.require("vpcCidr"),
                workload_az_id=conf.require("workloadAzId"),
                second_az_id=conf.require("secondAzId"),
            )
        ),
        database=validate_database(
            DatabaseConfig(
                engine_version=conf.require("dbEngineVersion"),
                database_name=conf.require("dbName"),
                min_acu=conf.require_float("dbMinAcu"),
                max_acu=conf.require_float("dbMaxAcu"),
                auto_pause_seconds=conf.require_int("dbAutoPauseSeconds"),
            )
        ),
        auth=validate_auth(
            AuthConfig(
                tenant_domain_prefix=conf.require("tenantAuthDomainPrefix"),
                platform_domain_prefix=conf.require("platformAuthDomainPrefix"),
                tenant_refresh_rotation=conf.get_bool("tenantRefreshTokenRotation"),
                platform_refresh_rotation=conf.get_bool("platformRefreshTokenRotation"),
            )
        ),
        tenancy=validate_tenancy(
            TenancyConfig(
                break_glass_principal_arn=conf.require("breakGlassPrincipalArn"),
                vector_dimensions=conf.require_int("vectorDimensions"),
                isolation_tests=conf.get_bool("isolationTests") or False,
                scan_inbox_only=conf.get_bool("malwareScanInboxOnly") or False,
                default_tags={str(k): str(v) for k, v in default_tags.items()},
            )
        ),
        alerts=validate_alerts(
            AlertsConfig(
                email=conf.require("alarmEmail"),
                security_notice_email=(conf.get("securityNoticeEmail") or "").strip(),
            )
        ),
        pipeline=validate_pipeline(
            PipelineConfig(
                worker_max_per_tenant=conf.require_int("workerMaxPerTenant"),
                scan_reserved_concurrency=conf.get_int("scanReservedConcurrency"),
                tss_reserved_concurrency=conf.get_int("tssReservedConcurrency"),
            )
        ),
        text=validate_text(
            TextConfig(
                quality_threshold=conf.require("textQualityThreshold").strip(),
                max_pages=conf.require_int("textMaxPages"),
                textract_price_per_page=(conf.get("textractPricePerPage") or "").strip(),
                textract_daily_pages_alarm=conf.require_int("textractDailyPagesAlarm"),
            )
        ),
        edge=validate_edge(
            EdgeConfig(
                waf_rate_limit=conf.require_int("wafRateLimit"),
                portal_idle_minutes=conf.require_int("portalIdleTimeoutMinutes"),
            )
        ),
        models=build_models(
            conf.require_object("aiModels"),
            conf.require("defaultIngestModel"),
            conf.require("defaultQueryModel"),
        ),
    )
