"""Container entry point: `python -m assay_api` serves the API on port 8000."""

from __future__ import annotations

import uvicorn

from assay_api.app import app_from_env
from assay_api.logs import LOG_CONFIG

PORT = 8000


def main() -> None:
    uvicorn.run(
        app_from_env(),
        # All interfaces inside the task's own network namespace; only the load balancer's
        # security group can reach the port.
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=PORT,
        log_config=LOG_CONFIG,
        server_header=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
