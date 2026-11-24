"""Apply the tenant stack program again to every active tenant (build step 6).

Usage (PowerShell, from the repository root):

    uv run scripts/update_tenants.py --tenant <tenant id> --preview
    uv run scripts/update_tenants.py --tenant <tenant id>
    uv run scripts/update_tenants.py

A change to the tenant stack program (`assay_tss.provision.program`) reaches existing tenants
only when their stacks run again. This starts the provisioning task with its `update`
command and waits for it. For each active business tenant (or each `--tenant`), the task
previews its stack (`assay-tenant/<tenant_id>`) as the provisioning role and applies only if
every planned change is one `assay_tss.provision.update.ALLOWED_CHANGES` lists. Build step 6
allows exactly two: create the bucket's CORS rule, and update the malware protection plan's
protected resource (scan `inbox/` only). The update then targets exactly those resources.
Any other planned change refuses that tenant, prints the change and applies nothing.

`--preview` only previews and prints the planned changes. Without it this changes AWS
resources, as `pulumi up` does: the owner runs it, after `pulumi up` of the platform stack.
Exits 1 unless the task exited 0.
"""

from __future__ import annotations

import argparse
import json
import shutil

# Runs the Pulumi CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
import uuid
from pathlib import Path
from typing import Any

import boto3

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"
# assay_infra.provisioning.CONTAINER_NAME: the container, and its log stream prefix.
CONTAINER = "provision"
COMMAND = "update"
STARTED_BY = "update-tenants"
# A tenant's stack takes a few minutes; allow an hour for all of them.
WAIT = {"Delay": 15, "MaxAttempts": 240}


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


def overrides(tenants: list[uuid.UUID], *, preview: bool = False) -> dict[str, Any]:
    environment = [
        {"name": "PROVISION_COMMAND", "value": COMMAND},
        {"name": "UPDATE_APPLY", "value": "false" if preview else "true"},
    ]
    if tenants:
        environment.append({"name": "UPDATE_TENANTS", "value": ",".join(map(str, tenants))})
    return {"containerOverrides": [{"name": CONTAINER, "environment": environment}]}


REPORTED = (
    "planned change",
    "unexpected changes; tenant not updated",
    "nothing to change",
    "previewed; not applied",
    "tenant updated",
    "tenant preview failed",
    "tenant update failed",
    "not active business tenants; not updated",
    "update finished",
)


def describe(entry: dict[str, Any]) -> str:
    """One printed line per log entry the task wrote about a tenant."""
    tenant = entry.get("tenant_id") or entry.get("tenants") or ""
    if entry["message"] == "planned change":
        diffs = ", ".join(entry.get("diffs") or [])
        changed = f" [{diffs}]" if diffs else ""
        return f"  {tenant}: {entry['op']} {entry['resource']} ({entry['type']}){changed}"
    if entry["message"] == "update finished":
        return f"update finished: {entry.get('tenants')} tenants, {entry.get('failed')} failed"
    detail = entry.get("changes") or entry.get("error") or ""
    return f"{entry['message']}: {tenant} {detail}".rstrip()


def results(messages: list[str]) -> list[dict[str, Any]]:
    """The task's per-tenant lines, from its JSON log."""
    found = []
    for message in messages:
        try:
            entry = json.loads(message)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("message") in REPORTED:
            found.append(entry)
    return found


def log_messages(logs: Any, group: str, stream: str) -> list[str]:
    messages: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.get_log_events(**kwargs)
        messages += [event["message"] for event in page["events"]]
        if not page["events"] or page["nextForwardToken"] == token:
            return messages
        token = page["nextForwardToken"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stack", default=DEFAULT_STACK)
    parser.add_argument(
        "--tenant",
        action="append",
        type=uuid.UUID,
        default=[],
        help="only this tenant (repeatable)",
    )
    parser.add_argument("--preview", action="store_true", help="show the changes; apply none")
    args = parser.parse_args()

    stack = args.stack
    session = boto3.Session(
        profile_name=pulumi("config", "get", "aws:profile", stack=stack),
        region_name=pulumi("config", "get", "aws:region", stack=stack),
    )
    cluster = pulumi("stack", "output", "provisioning_cluster_name", stack=stack)
    task_definition = pulumi("stack", "output", "provisioning_task_definition_arn", stack=stack)
    log_group = pulumi("stack", "output", "provisioning_log_group", stack=stack)
    subnet = pulumi("stack", "output", "workload_subnet_id", stack=stack)
    security_group = pulumi("stack", "output", "workload_security_group_id", stack=stack)

    ecs = session.client("ecs")
    what = "Previewing" if args.preview else "Updating"
    print(f"{what} tenant stacks in cluster {cluster} ...", flush=True)
    started = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        platformVersion="LATEST",
        count=1,
        startedBy=STARTED_BY,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [subnet],
                "securityGroups": [security_group],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides=overrides(args.tenant, preview=args.preview),
    )
    if started.get("failures") or not started.get("tasks"):
        print(f"The task did not start: {started.get('failures')}", file=sys.stderr)
        return 1
    task_arn = started["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(cluster=cluster, tasks=[task_arn], WaiterConfig=WAIT)
    task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
    exit_code = task["containers"][0].get("exitCode")
    stream = f"{CONTAINER}/{CONTAINER}/{task_arn.rsplit('/', 1)[-1]}"
    for entry in results(log_messages(session.client("logs"), log_group, stream)):
        print(describe(entry))
    print(f"Task exit code {exit_code}. Log stream {stream} in {log_group}.")
    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
