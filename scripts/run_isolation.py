"""Run tests/isolation inside the VPC, against the real account, and print the results.

Usage (PowerShell, from the repository root):

    uv run scripts/run_isolation.py
    uv run scripts/run_isolation.py --tenant-a <tenant id> --tenant-b <tenant id>

The tests need two active business tenants. Without arguments, their IDs come from stack
config `isolationTenantIds` ("<id>,<id>"). The TSS and the database are private, so the
tests run in the isolation test Lambda (stack config `isolationTests: true`); this script
starts it and waits. Exits 1 unless every test passed.
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
from botocore.config import Config

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"
TENANTS_CONFIG_KEY = "isolationTenantIds"
MARKS = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tenant-a", type=uuid.UUID)
    parser.add_argument("--tenant-b", type=uuid.UUID)
    parser.add_argument("--stack", default=DEFAULT_STACK)
    args = parser.parse_args()

    if args.tenant_a and args.tenant_b:
        tenant_a, tenant_b = args.tenant_a, args.tenant_b
    elif args.tenant_a or args.tenant_b:
        parser.error("give both --tenant-a and --tenant-b, or neither")
    else:
        configured = pulumi("config", "get", TENANTS_CONFIG_KEY, stack=args.stack, check=False)
        if not configured:
            sys.exit(
                f"No test tenants: pass --tenant-a and --tenant-b, or set stack config "
                f"{TENANTS_CONFIG_KEY} to the two tenant IDs."
            )
        tenant_a, tenant_b = parse_tenants(configured)

    profile = pulumi("config", "get", "aws:profile", stack=args.stack)
    region = pulumi("config", "get", "aws:region", stack=args.stack)
    function = pulumi("stack", "output", "isolation_function_name", stack=args.stack)

    session = boto3.Session(profile_name=profile, region_name=region)
    # A paused database takes a while to resume; the function's own timeout is 300 seconds.
    client = session.client("lambda", config=Config(read_timeout=330, retries={"max_attempts": 0}))
    print(f"Running tests/isolation in {function} for {tenant_a} and {tenant_b} ...", flush=True)
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        Payload=json.dumps({"tenant_a": str(tenant_a), "tenant_b": str(tenant_b)}).encode(),
    )
    payload = json.loads(response["Payload"].read() or b"null")
    if "FunctionError" in response or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2), file=sys.stderr)
        print("The isolation test run itself failed.", file=sys.stderr)
        return 1
    if not report(payload):
        print("Isolation tests did not all pass.", file=sys.stderr)
        return 1
    print("Every isolation test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
