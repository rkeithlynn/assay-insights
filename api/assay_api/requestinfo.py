"""The viewer's IP address and browser, for audit rows (BUILD_SPEC.md section 7a).

CloudFront adds `CloudFront-Viewer-Address` (`<ip>:<port>`) to every request it forwards
(origin request policy AllViewerAndCloudFrontHeaders). The load balancer accepts traffic
only from CloudFront's VPC origin, so the header cannot come from anyone else. A header
that does not parse is ignored, never stored.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

from assay_db.audit import RequestInfo

VIEWER_ADDRESS = "cloudfront-viewer-address"


def viewer_ip(value: str | None) -> str | None:
    if not value:
        return None
    address, _, port = value.strip().rpartition(":")
    if not address or not port.isdigit():
        return None
    try:
        return str(ipaddress.ip_address(address.strip("[]")))
    except ValueError:
        return None


def request_info(request: Request) -> RequestInfo:
    return RequestInfo(
        ip_address=viewer_ip(request.headers.get(VIEWER_ADDRESS)),
        user_agent=request.headers.get("user-agent"),
    )
