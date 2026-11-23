"""Stack configuration.

Every account ID, hostname, email address and model ID comes from stack config
(`Pulumi.<stack>.yaml`), never from code.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulumi


@dataclass(frozen=True)
class PlatformConfig:
    hosted_zone: str
    tenant_host: str
    admin_host: str

    @property
    def hosts(self) -> list[str]:
        return [self.tenant_host, self.admin_host]


def _within_zone(host: str, zone: str) -> bool:
    return host == zone or host.endswith(f".{zone}")


def build_config(hosted_zone: str, tenant_host: str, admin_host: str) -> PlatformConfig:
    cfg = PlatformConfig(
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


def load_config() -> PlatformConfig:
    conf = pulumi.Config()
    return build_config(
        hosted_zone=conf.require("hostedZone"),
        tenant_host=conf.require("tenantHost"),
        admin_host=conf.require("adminHost"),
    )
