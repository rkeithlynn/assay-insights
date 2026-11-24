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
    zone is delegated from the registrar. `validated()` waits for issuance; it is called
    once something uses the certificate (the edge, build step 5).
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
        self._name = name

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

    def validated(self) -> aws.acm.CertificateValidation:
        """Wait until ACM has issued the certificate (build step 5: CloudFront and the load
        balancer use it). The zone is delegated, so this returns at once on an issued one."""
        self.validation = aws.acm.CertificateValidation(
            f"{self._name}-cert-validation",
            certificate_arn=self.certificate.arn,
            validation_record_fqdns=[r.fqdn for r in self.validation_records],
            opts=pulumi.ResourceOptions(parent=self),
        )
        return self.validation
