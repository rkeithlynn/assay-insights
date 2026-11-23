"""Hosted zone and the DNS-validated ACM certificate for the portal hosts."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pulumi
import pulumi_aws as aws


def _slug(host: str) -> str:
    return host.replace(".", "-")


def _option_for(
    host: str,
) -> Callable[
    [Sequence[aws.acm.outputs.CertificateDomainValidationOption]],
    aws.acm.outputs.CertificateDomainValidationOption,
]:
    def pick(
        options: Sequence[aws.acm.outputs.CertificateDomainValidationOption],
    ) -> aws.acm.outputs.CertificateDomainValidationOption:
        return next(o for o in options if o.domain_name == host)

    return pick


class DnsAndCertificate(pulumi.ComponentResource):
    """Route 53 hosted zone plus one ACM certificate covering every portal host.

    The certificate lives in the provider's Region (us-east-1, as CloudFront requires).
    Validation records are written into the zone; ACM issues the certificate once the
    zone is delegated from the registrar. Waiting for issuance is left to the consumer
    of the certificate (the edge step), so `pulumi up` never blocks on delegation.
    """

    def __init__(
        self,
        name: str,
        *,
        zone_name: str,
        hosts: Sequence[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:edge:DnsAndCertificate", name, None, opts)
        if not hosts:
            raise ValueError("at least one host is required")
        child = pulumi.ResourceOptions(parent=self)

        # Protected: replacing the zone changes its name servers and breaks delegation.
        self.zone = aws.route53.Zone(
            f"{name}-zone",
            name=zone_name,
            comment="Assay Insights development environment",
            force_destroy=False,
            opts=pulumi.ResourceOptions(parent=self, protect=True),
        )

        primary, *alternatives = hosts
        self.certificate = aws.acm.Certificate(
            f"{name}-cert",
            domain_name=primary,
            subject_alternative_names=alternatives,
            validation_method="DNS",
            key_algorithm="RSA_2048",
            options=aws.acm.CertificateOptionsArgs(
                certificate_transparency_logging_preference="ENABLED",
            ),
            opts=child,
        )

        self.validation_records: list[aws.route53.Record] = []
        for host in hosts:
            option = self.certificate.domain_validation_options.apply(_option_for(host))
            self.validation_records.append(
                aws.route53.Record(
                    f"{name}-validation-{_slug(host)}",
                    zone_id=self.zone.zone_id,
                    name=option.resource_record_name,
                    type=option.resource_record_type,
                    records=[option.resource_record_value],
                    ttl=300,
                    allow_overwrite=False,
                    opts=child,
                )
            )

        self.register_outputs(
            {
                "zone_id": self.zone.zone_id,
                "name_servers": self.zone.name_servers,
                "certificate_arn": self.certificate.arn,
            }
        )
