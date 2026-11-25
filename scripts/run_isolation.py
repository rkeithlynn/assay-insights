"""Run tests/isolation inside the VPC, against the real account, and print the results.

Usage (PowerShell, from the repository root):

    uv run scripts/run_isolation.py
    uv run scripts/run_isolation.py --tenant-a <tenant id> --tenant-b <tenant id>

The tests need two active business tenants. Without arguments, their IDs come from stack
config `isolationTenantIds` ("<id>,<id>"). The TSS and the database are private, so nothing
here runs on this machine. Three things are started, and the script waits for each:

1. the isolation test Lambda (stack config `isolationTests: true`), which runs
   tests/isolation with pytest;
2. the provisioning task with its `probe` command, which tries, as the provisioning role,
   to read and write the tenants' objects and vectors. It must be refused every time.

3. the worker task with its `probe` command (build step 6, second half), which tries, as
   the worker's own role, to get storage credentials and a database session for anything
   but the one document its grant names, and checks the PDF sandbox's confinement on
   Fargate itself. The isolation Lambda prepares its fixture documents and grants first,
   ends one grant while the probe watches, and clears them afterwards.

A fourth part runs here, with your credentials, because Cognito cannot be reached from the
VPC: `tests/isolation/cognito_clients.py` checks that neither portal app client may write
anything but `name`, and that a signed-in user's own token cannot change their
`custom:tenant_id` or `email` (on a throwaway replica pool it creates and deletes).

Exits 1 unless every test and every check passed.
"""

from __future__ import annotations

import argparse
import json
import shutil

# Runs the Pulumi CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.isolation import cognito_clients

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"
TENANTS_CONFIG_KEY = "isolationTenantIds"
MARKS = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}
# assay_infra.provisioning.CONTAINER_NAME: the container, and its log stream prefix.
PROBE_CONTAINER = "provision"
PROBE_COMMAND = "probe"
PROBE_STARTED_BY = "isolation-tests"
# assay_infra.pipeline.WORKER_CONTAINER, and assay_workers.worker.probe.READY_FOR_END.
WORKER_CONTAINER = "worker"
WORKER_READY_FOR_END = "ready_for_end"
WORKER_POLL_SECONDS = 5
WORKER_PROBE_TIMEOUT_SECONDS = 900


def pulumi(*args: str, stack: str, check: bool = True) -> str:
    exe = shutil.which("pulumi")
    if exe is None:
        sys.exit("pulumi CLI not found on PATH")
    out = subprocess.run(  # noqa: S603  # nosec B603
        [exe, *args, "--stack", stack, "--cwd", INFRA_DIR.as_posix()],
        check=check,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def parse_tenants(value: str) -> tuple[uuid.UUID, uuid.UUID]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError(f"{TENANTS_CONFIG_KEY} must hold two tenant IDs separated by a comma")
    first, second = uuid.UUID(parts[0]), uuid.UUID(parts[1])
    if first == second:
        raise ValueError("the two tenant IDs must differ")
    return first, second


def report(result: dict[str, Any]) -> bool:
    """Print one line per test and the totals. True when every test passed."""
    for test in result.get("tests", []):
        print(f"{MARKS.get(test['outcome'], test['outcome'].upper())}  {test['test']}")
        if test.get("detail"):
            print("      " + test["detail"].replace("\n", "\n      "))
    totals = ", ".join(f"{result.get(k, 0)} {k}" for k in ("passed", "failed", "skipped"))
    print(f"\n{totals}")
    return bool(result.get("ok"))


# --- The provisioning role's probe ---------------------------------------------------------


def probe_overrides(tenants: tuple[uuid.UUID, uuid.UUID]) -> dict[str, Any]:
    return {
        "containerOverrides": [
            {
                "name": PROBE_CONTAINER,
                "environment": [
                    {"name": "PROVISION_COMMAND", "value": PROBE_COMMAND},
                    {"name": "PROBE_TENANTS", "value": ",".join(str(t) for t in tenants)},
                ],
            }
        ]
    }


def probe_checks(messages: list[str]) -> list[dict[str, Any]]:
    """The probe's results, from the task's JSON log lines."""
    checks = []
    for message in messages:
        try:
            entry = json.loads(message)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("message") == "probe" and "check" in entry:
            checks.append(entry)
    return checks


def report_probe(checks: list[dict[str, Any]], exit_code: int | None) -> bool:
    """Print one line per check. True when the task exited 0 and every check held."""
    for check in checks:
        mark = "PASS" if check.get("ok") else "FAIL"
        tenant = str(check.get("tenant_id", ""))[:8]
        print(
            f"{mark}  provisioning role, tenant {tenant}: {check['check']} "
            f"(expected {check.get('expected')}, was {check.get('outcome')})"
        )
    failed = [c for c in checks if not c.get("ok")]
    print(
        f"\n{len(checks) - len(failed)} passed, {len(failed)} failed (task exit code {exit_code})"
    )
    return bool(checks) and not failed and exit_code == 0


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


def run_probe(session: boto3.Session, stack: str, tenants: tuple[uuid.UUID, uuid.UUID]) -> bool:
    cluster = pulumi("stack", "output", "provisioning_cluster_name", stack=stack)
    task_definition = pulumi("stack", "output", "provisioning_task_definition_arn", stack=stack)
    log_group = pulumi("stack", "output", "provisioning_log_group", stack=stack)
    subnet = pulumi("stack", "output", "workload_subnet_id", stack=stack)
    security_group = pulumi("stack", "output", "workload_security_group_id", stack=stack)

    ecs = session.client("ecs")
    print(f"\nRunning the provisioning role's probe in cluster {cluster} ...", flush=True)
    started = ecs.run_task(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        platformVersion="LATEST",
        count=1,
        startedBy=PROBE_STARTED_BY,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [subnet],
                "securityGroups": [security_group],
                "assignPublicIp": "DISABLED",
            }
        },
        overrides=probe_overrides(tenants),
    )
    if started.get("failures") or not started.get("tasks"):
        print(f"The probe task did not start: {started.get('failures')}", file=sys.stderr)
        return False
    task_arn = started["tasks"][0]["taskArn"]
    ecs.get_waiter("tasks_stopped").wait(
        cluster=cluster, tasks=[task_arn], WaiterConfig={"Delay": 6, "MaxAttempts": 100}
    )
    task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
    exit_code = task["containers"][0].get("exitCode")
    stream = f"{PROBE_CONTAINER}/{PROBE_CONTAINER}/{task_arn.rsplit('/', 1)[-1]}"
    checks = probe_checks(log_messages(session.client("logs"), log_group, stream))
    if not checks:
        print(
            f"The probe task wrote no results (stopped: {task.get('stoppedReason')}). "
            f"See log stream {stream} in {log_group}.",
            file=sys.stderr,
        )
    return report_probe(checks, exit_code)


# --- The worker task's probe --------------------------------------------------------------


def invoke_runner(session: boto3.Session, function: str, payload: dict[str, Any]) -> Any:
    client = session.client("lambda", config=Config(read_timeout=120, retries={"max_attempts": 0}))
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    result = json.loads(response["Payload"].read() or b"null")
    if "FunctionError" in response or not isinstance(result, dict):
        # The function's error, never the request: the request can hold grant tokens.
        raise RuntimeError(f"the isolation Lambda failed on {payload.get('action')}")
    return result


def worker_overrides(cases: dict[str, Any]) -> dict[str, Any]:
    return {
        "containerOverrides": [
            {
                "name": WORKER_CONTAINER,
                "environment": [
                    {"name": "WORKER_COMMAND", "value": PROBE_COMMAND},
                    {"name": "PROBE_CASES", "value": json.dumps(cases, separators=(",", ":"))},
                ],
            }
        ]
    }


def worker_entries(messages: list[str]) -> list[dict[str, Any]]:
    entries = []
    for message in messages:
        try:
            entry = json.loads(message)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("message") == "probe":
            entries.append(entry)
    return entries


def report_worker_probe(entries: list[dict[str, Any]], exit_code: int | None) -> bool:
    """Print one line per check. True when the task exited 0 and every check held."""
    checks = [e for e in entries if "check" in e]
    for check in checks:
        mark = "PASS" if check.get("ok") else "FAIL"
        print(
            f"{mark}  worker task: {check['check']} "
            f"(expected {check.get('expected')}, was {check.get('outcome')})"
        )
    failed = [c for c in checks if not c.get("ok")]
    print(
        f"\n{len(checks) - len(failed)} passed, {len(failed)} failed (task exit code {exit_code})"
    )
    return bool(checks) and not failed and exit_code == 0


def run_worker_probe(
    session: boto3.Session, stack: str, tenants: tuple[uuid.UUID, uuid.UUID]
) -> bool:
    function = pulumi("stack", "output", "isolation_function_name", stack=stack)
    cluster = pulumi("stack", "output", "worker_cluster_name", stack=stack)
    task_definition = pulumi("stack", "output", "worker_task_definition_arn", stack=stack)
    log_group = pulumi("stack", "output", "worker_log_group", stack=stack)
    subnet = pulumi("stack", "output", "workload_subnet_id", stack=stack)
    security_group = pulumi("stack", "output", "workload_security_group_id", stack=stack)
    names = {"tenant_a": str(tenants[0]), "tenant_b": str(tenants[1])}

    print(f"\nRunning the worker task's probe in cluster {cluster} ...", flush=True)
    cases = invoke_runner(session, function, {"action": "worker_fixture", **names})["cases"]
    ecs, logs = session.client("ecs"), session.client("logs")
    try:
        started = ecs.run_task(
            cluster=cluster,
            taskDefinition=task_definition,
            launchType="FARGATE",
            platformVersion="LATEST",
            count=1,
            startedBy=PROBE_STARTED_BY,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [subnet],
                    "securityGroups": [security_group],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides=worker_overrides(cases),
        )
        if started.get("failures") or not started.get("tasks"):
            print(f"The worker probe did not start: {started.get('failures')}", file=sys.stderr)
            return False
        task_arn = started["tasks"][0]["taskArn"]
        stream = f"{WORKER_CONTAINER}/{WORKER_CONTAINER}/{task_arn.rsplit('/', 1)[-1]}"
        ended = False
        deadline = time.monotonic() + WORKER_PROBE_TIMEOUT_SECONDS
        task: dict[str, Any] = {}
        while time.monotonic() < deadline:
            task = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]
            if not ended:
                # The probe has bound a session to this grant and is watching it. End it now.
                try:
                    entries = worker_entries(log_messages(logs, log_group, stream))
                except logs.exceptions.ResourceNotFoundException:
                    entries = []  # the task has not written a line yet
                if any(e.get("signal") == WORKER_READY_FOR_END for e in entries):
                    invoke_runner(
                        session,
                        function,
                        {
                            "action": "end_worker_grant",
                            **names,
                            "document_id": cases["to_end"]["document_id"],
                        },
                    )
                    ended = True
            if task.get("lastStatus") == "STOPPED":
                break
            time.sleep(WORKER_POLL_SECONDS)
        exit_code = (task.get("containers") or [{}])[0].get("exitCode")
        try:
            entries = worker_entries(log_messages(logs, log_group, stream))
        except logs.exceptions.ResourceNotFoundException:
            entries = []
        if not [e for e in entries if "check" in e]:
            print(
                f"The worker probe wrote no results (stopped: {task.get('stoppedReason')}). "
                f"See log stream {stream} in {log_group}.",
                file=sys.stderr,
            )
        return report_worker_probe(entries, exit_code)
    finally:
        invoke_runner(session, function, {"action": "worker_cleanup", **names})


def run_cognito_clients(session: boto3.Session, stack: str) -> bool:
    print("\nChecking the portal app clients' writable attributes ...", flush=True)
    probe: list[str] = []
    checks = cognito_clients.run(
        session,
        probe=probe,
        tenant=(
            pulumi("stack", "output", "tenant_user_pool_id", stack=stack),
            pulumi("stack", "output", "tenant_app_client_id", stack=stack),
        ),
        platform=(
            pulumi("stack", "output", "platform_user_pool_id", stack=stack),
            pulumi("stack", "output", "platform_app_client_id", stack=stack),
        ),
        name_prefix=f"assay-{stack.rsplit('/', 1)[-1]}",
    )
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"{mark}  {check.check} (expected {check.expected}, was {check.outcome})")
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    if probe:
        # For the owner's decision, before rotation is switched on for the real clients.
        print("\nRefresh-token rotation probe (replica pool; not pass or fail):")
        for line in probe:
            print(f"  {line}")
    return bool(checks) and not failed


def run_tests(session: boto3.Session, stack: str, tenants: tuple[uuid.UUID, uuid.UUID]) -> bool:
    function = pulumi("stack", "output", "isolation_function_name", stack=stack)
    # A paused database takes a while to resume; the function's own timeout is 300 seconds.
    client = session.client("lambda", config=Config(read_timeout=330, retries={"max_attempts": 0}))
    print(
        f"Running tests/isolation in {function} for {tenants[0]} and {tenants[1]} ...", flush=True
    )
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        Payload=json.dumps({"tenant_a": str(tenants[0]), "tenant_b": str(tenants[1])}).encode(),
    )
    payload = json.loads(response["Payload"].read() or b"null")
    if "FunctionError" in response or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2), file=sys.stderr)
        print("The isolation test run itself failed.", file=sys.stderr)
        return False
    return report(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tenant-a", type=uuid.UUID)
    parser.add_argument("--tenant-b", type=uuid.UUID)
    parser.add_argument("--stack", default=DEFAULT_STACK)
    args = parser.parse_args()

    if args.tenant_a and args.tenant_b:
        tenants = (args.tenant_a, args.tenant_b)
    elif args.tenant_a or args.tenant_b:
        parser.error("give both --tenant-a and --tenant-b, or neither")
    else:
        configured = pulumi("config", "get", TENANTS_CONFIG_KEY, stack=args.stack, check=False)
        if not configured:
            sys.exit(
                f"No test tenants: pass --tenant-a and --tenant-b, or set stack config "
                f"{TENANTS_CONFIG_KEY} to the two tenant IDs."
            )
        tenants = parse_tenants(configured)

    profile = pulumi("config", "get", "aws:profile", stack=args.stack)
    region = pulumi("config", "get", "aws:region", stack=args.stack)
    session = boto3.Session(profile_name=profile, region_name=region)

    tests_ok = run_tests(session, args.stack, tenants)
    probe_ok = run_probe(session, args.stack, tenants)
    worker_ok = run_worker_probe(session, args.stack, tenants)
    clients_ok = run_cognito_clients(session, args.stack)
    if not (tests_ok and probe_ok and worker_ok and clients_ok):
        print("Isolation tests did not all pass.", file=sys.stderr)
        return 1
    print("Every isolation test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
