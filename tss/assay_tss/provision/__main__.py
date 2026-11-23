"""Entry point of the provisioning task: `python -m assay_tss.provision`.

The state machine passes everything in the environment:

- `PROVISION_COMMAND`: `provision` (the default) or `fail` (the state machine's catch,
  which records a failure the task could not record itself);
- `PROVISION_REQUEST`: the execution's request, as JSON;
- `PROVISION_ERROR`: for `fail`, the error name Step Functions reported.

A third command, `probe`, is not part of provisioning: `scripts/run_isolation.py` runs the
task with it to show what this role cannot do with tenant data (`PROBE_TENANTS`, a
comma-separated list of tenant IDs; see `probe.py`).

Exits 0 on success and 1 on failure, so the execution's status shows the result.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys

from assay_db.connect import DbTarget, iam_engine
from assay_tss.provision.invite import LambdaInviter
from assay_tss.provision.probe import Probe, parse_tenants
from assay_tss.provision.settings import ProvisionSettings
from assay_tss.provision.stack import StackRunner, prepare_pulumi_home
from assay_tss.provision.task import Provisioner, ProvisioningFailed, ProvisionRequest

APPLICATION_NAME = "assay-provision"
DB_USER = "platform_writer"  # assay_db.roles.PLATFORM_WRITER
COMMANDS = ("provision", "fail", "probe")
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry |= {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def run_probe(log: logging.Logger) -> int:
    """Try, as this role, what it must not be able to do with tenant data."""
    settings = ProvisionSettings.from_env()
    tenants = parse_tenants(os.environ.get("PROBE_TENANTS", ""))
    results = Probe(settings).run(tenants)
    failed = [check for check in results if not check.ok]
    log.info("probe finished", extra={"checks": len(results), "failed": len(failed)})
    return 1 if failed else 0


def main() -> int:
    configure_logging()
    log = logging.getLogger("assay_tss.provision")
    command = os.environ.get("PROVISION_COMMAND", "provision")
    if command not in COMMANDS:
        log.error("unknown command", extra={"command": command})
        return 2
    if command == "probe":
        return run_probe(log)
    prepare_pulumi_home(os.environ)
    request = ProvisionRequest.model_validate_json(os.environ["PROVISION_REQUEST"])
    settings = ProvisionSettings.from_env()
    engine = iam_engine(DbTarget.from_env(), DB_USER, application_name=APPLICATION_NAME)
    provisioner = Provisioner(
        settings,
        engine,
        StackRunner(settings),
        LambdaInviter(settings.invite_function, region=settings.region),
    )
    if command == "fail":
        recorded = provisioner.record_crash(
            request.tenant_id, os.environ.get("PROVISION_ERROR", "")
        )
        log.info("failure noted", extra={"tenant_id": str(request.tenant_id), "new": recorded})
        return 0
    try:
        outcome = provisioner.run(request)
    except ProvisioningFailed as exc:
        log.error("provisioning failed", extra={"step": exc.step})
        return 1
    log.info("provisioning finished", extra={"outcome": outcome.value})
    return 0


if __name__ == "__main__":
    sys.exit(main())
