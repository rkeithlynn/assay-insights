"""assay-platform Pulumi program.

Build order follows BUILD_SPEC.md section 4. Implemented so far: 1 (DNS and certificate).
"""

import pulumi

from assay_infra.config import load_config
from assay_infra.dns import DnsAndCertificate

config = load_config()

edge_dns = DnsAndCertificate("edge", zone_name=config.hosted_zone, hosts=config.hosts)

pulumi.export("hosted_zone_id", edge_dns.zone.zone_id)
pulumi.export("name_servers", edge_dns.zone.name_servers)
pulumi.export("certificate_arn", edge_dns.certificate.arn)
