"""Container entry point: `python -m assay_api` serves the API on port 8000.

The settings are read before the server starts. If they are unusable (an empty or missing
JWKS above all), the process logs why and exits with status 1, so the task never passes
the load balancer's health check and ECS's circuit breaker keeps the previous task.
"""

from __future__ import annotations

import logging
import logging.config
import sys

import uvicorn

from assay_api.app import app_from_env
from assay_api.logs import LOG_CONFIG

PORT = 8000
EXIT_BAD_SETTINGS = 1

log = logging.getLogger("assay_api")


def main() -> int:
    logging.config.dictConfig(LOG_CONFIG)
    try:
        app = app_from_env()
    except (KeyError, ValueError) as exc:
        # ValueError covers SettingsError and a JWKS with no RS256 signing key.
        log.error("API settings unusable; not starting", extra={"error": str(exc)})
        return EXIT_BAD_SETTINGS
    uvicorn.run(
        app,
        # All interfaces inside the task's own network namespace; only the load balancer's
        # security group can reach the port.
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=PORT,
        log_config=LOG_CONFIG,
        server_header=False,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
