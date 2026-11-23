"""Add a business tenant, or retry a failed one, and wait for provisioning to finish.

Usage (PowerShell, from the repository root):

    uv run scripts/add_tenant.py --name "Tenant" --admin-name "Admin" --admin-email a@example.com
    uv run scripts/add_tenant.py --retry <tenant id>

Starts the provisioning state machine: the same path `POST /admin/tenants` uses. The task
inserts the tenant row (status `provisioning`), creates the tenant's storage, invites the
first admin by email and sets the tenant `active`. The tenant ID is printed first; keep it.

`--retry` starts a new execution for a tenant whose provisioning failed, with the request
of its latest execution. The AWS profile, Region and state machine come from the stack's
config and outputs. You are recorded in `audit_log` as the operator who started it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil

# Runs the Pulumi CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions import SFNClient

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"
OPERATOR_ROLE = "operator"  # assay_db.tenants.ROLE_OPERATOR
POLL_SECONDS = 10
RUNNING = "RUNNING"


def pulumi(*args: str, stack: str) -> str:
    exe = shutil.which("pulumi")
    if exe is None:
        sys.exit("pulumi CLI not found on PATH")
    out = subprocess.run(  # noqa: S603  # nosec B603
        [exe, *args, "--stack", stack, "--cwd", INFRA_DIR.as_posix()],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def execution_prefix(tenant_id: uuid.UUID) -> str:
    return f"t-{tenant_id}-"


def execution_name(tenant_id: uuid.UUID, now: dt.datetime) -> str:
    return f"{execution_prefix(tenant_id)}{now:%Y%m%d%H%M%S}"


def new_request(
    *, tenant_id: uuid.UUID, name: str, admin_name: str, admin_email: str, principal: str
) -> dict[str, Any]:
    return {
        "tenant_id": str(tenant_id),
        "name": name,
        "first_admin": {"name": admin_name, "email": admin_email},
        "actor": {"role": OPERATOR_ROLE, "principal": principal},
        "retry": False,
    }


def retry_request(previous: dict[str, Any], principal: str) -> dict[str, Any]:
    return {**previous, "actor": {"role": OPERATOR_ROLE, "principal": principal}, "retry": True}


def latest_execution(sfn: SFNClient, state_machine: str, tenant_id: uuid.UUID) -> dict[str, Any]:
    """The tenant's most recent execution (the listing is newest first)."""
    prefix = execution_prefix(tenant_id)
    for page in sfn.get_paginator("list_executions").paginate(stateMachineArn=state_machine):
        for execution in page["executions"]:
            if execution["name"].startswith(prefix):
                return dict(execution)
    sys.exit(f"No provisioning execution found for tenant {tenant_id}.")


def wait(sfn: SFNClient, execution_arn: str) -> str:
    while True:
        status = sfn.describe_execution(executionArn=execution_arn)["status"]
        if status != RUNNING:
            return status
        print(f"  {dt.datetime.now():%H:%M:%S}  {status}", flush=True)
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--name", help="the tenant's legal name")
    parser.add_argument("--admin-name", help="the first admin's name")
    parser.add_argument("--admin-email", help="the first admin's email address")
    parser.add_argument("--retry", metavar="TENANT_ID", type=uuid.UUID)
    parser.add_argument("--no-wait", action="store_true", help="start it and return")
    parser.add_argument("--stack", default=DEFAULT_STACK)
    args = parser.parse_args()
    new_tenant = (args.name, args.admin_name, args.admin_email)
    if args.retry is None and not all(new_tenant):
        parser.error("give --name, --admin-name and --admin-email, or --retry TENANT_ID")
    if args.retry is not None and any(new_tenant):
        parser.error("--retry takes the request from the tenant's last execution")

    profile = pulumi("config", "get", "aws:profile", stack=args.stack)
    region = pulumi("config", "get", "aws:region", stack=args.stack)
    state_machine = pulumi("stack", "output", "provisioning_state_machine_arn", stack=args.stack)
    session = boto3.Session(profile_name=profile, region_name=region)
    principal = session.client("sts").get_caller_identity()["Arn"]
    sfn = session.client("stepfunctions")

    if args.retry is not None:
        tenant_id = args.retry
        previous = latest_execution(sfn, state_machine, tenant_id)
        if previous["status"] == RUNNING:
            sys.exit(f"Tenant {tenant_id} is still being provisioned ({previous['name']}).")
        described = sfn.describe_execution(executionArn=previous["executionArn"])
        request = retry_request(json.loads(described["input"])["request"], principal)
    else:
        tenant_id = uuid.uuid4()
        request = new_request(
            tenant_id=tenant_id,
            name=args.name,
            admin_name=args.admin_name,
            admin_email=args.admin_email,
            principal=principal,
        )

    started = sfn.start_execution(
        stateMachineArn=state_machine,
        name=execution_name(tenant_id, dt.datetime.now(dt.UTC)),
        input=json.dumps({"request": request}),
    )
    print(f"Tenant ID: {tenant_id}")
    print(f"Execution: {started['executionArn']}")
    if args.no_wait:
        return 0
    print("Provisioning (a few minutes) ...", flush=True)
    status = wait(sfn, started["executionArn"])
    print(f"Result: {status}")
    if status != "SUCCEEDED":
        print(
            "Provisioning did not finish. The failed step is in audit_log and in the "
            "provisioning task's log. To retry:\n"
            f"  uv run scripts/add_tenant.py --retry {tenant_id}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
