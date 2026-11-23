"""Run the database migration Lambda for a Pulumi stack and print its result.

Usage (PowerShell, from the repository root, after `pulumi up`):

    uv run scripts/migrate.py                 # create roles, alembic upgrade head
    uv run scripts/migrate.py --command verify    # read-only: RLS and role checks
    uv run scripts/migrate.py --command current
    uv run scripts/migrate.py --command maintain

The AWS profile, Region and function name come from the stack's config and outputs.
"""

from __future__ import annotations

import argparse
import json
import shutil

# Runs the Pulumi CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
from pathlib import Path

import boto3
from botocore.config import Config

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"
COMMANDS = ("upgrade", "verify", "current", "maintain")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--command", choices=COMMANDS, default="upgrade")
    parser.add_argument("--stack", default=DEFAULT_STACK)
    args = parser.parse_args()

    profile = pulumi("config", "get", "aws:profile", stack=args.stack)
    region = pulumi("config", "get", "aws:region", stack=args.stack)
    function = pulumi("stack", "output", "migrate_function_name", stack=args.stack)

    session = boto3.Session(profile_name=profile, region_name=region)
    # A paused database takes a while to resume; the function's own timeout is 300 seconds.
    client = session.client("lambda", config=Config(read_timeout=330, retries={"max_attempts": 0}))
    print(f"Invoking {function} with command {args.command!r} ...", flush=True)
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        Payload=json.dumps({"command": args.command}).encode(),
    )
    payload = json.loads(response["Payload"].read() or b"null")
    print(json.dumps(payload, indent=2))
    if "FunctionError" in response:
        print(f"Migration failed ({response['FunctionError']}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
