"""Stack configuration.

Every account ID, hostname, email address and model ID comes from stack config
(`Pulumi.<stack>.yaml`), never from code.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

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
class PlatformConfig:
    region: str
    # Prefix for physical resource names, for example "assay-dev".
    name_prefix: str
    dns: DnsConfig
    network: NetworkConfig
    database: DatabaseConfig


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


def load_config() -> PlatformConfig:
    conf = pulumi.Config()
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
    )
