"""Seed the development environment (BUILD_SPEC.md sections 5 and 7b). Safe to run again.

Usage (PowerShell, from the repository root, after `pulumi up` and `scripts/migrate.py`):

    uv run scripts/seed.py

Everything comes from the stack's config (`aiModels`, `defaultIngestModel`,
`defaultQueryModel`, `seed`) and outputs. In order:

1. **Models.** One short test call to each `aiModels` inference profile through Bedrock
   (section 7b: verify before seeding), with your credentials, from this machine. The prompt
   is fixed text; no tenant content goes to Bedrock. Then the catalog is loaded as
   `migrator` through the migration Lambda (`seed_models`). Refused while any price in
   `aiModels` is blank.
2. **Platform tenant and its users.** The Cognito users are created in the platform pool
   with your credentials (no deployed function may create a platform user), then their rows
   and the platform tenant by the seed Lambda. Cognito emails each an invitation; the pool
   requires MFA, so managed login has them set it up at first sign-in.
3. **Business tenants**, through the add-tenant path `POST /admin/tenants` uses (seed
   Lambda), and the script waits for provisioning. Their first admins are invited by the
   provisioning task, as for any new tenant.
4. **Their other users**, through the path `POST /users` uses (seed Lambda).
5. **Default models** for every active business tenant that has none yet (the isolation
   fixture tenants predate the catalog).

You are recorded in audit_log as the operator for every change.
"""

from __future__ import annotations

import argparse
import json
import shutil

# Runs the Pulumi CLI with fixed arguments and no shell.
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient
    from mypy_boto3_lambda import LambdaClient

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"
DEFAULT_STACK = "pilartech/assay-platform/dev"
PLATFORM_ROLES = ("master-admin", "master-user")
TENANT_ROLES = ("tenant-admin", "tenant-user")
TEST_PROMPT = "Reply with the single word OK."
POLL_SECONDS = 15
PROVISION_TIMEOUT_SECONDS = 45 * 60


class SeedFailed(RuntimeError):
    pass


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


def config_object(key: str, stack: str) -> Any:
    return json.loads(pulumi("config", "get", key, "--json", stack=stack))["objectValue"]


# --- Checking the config ------------------------------------------------------------------


def catalog_from_config(raw: Any) -> list[dict[str, Any]]:
    """aiModels as the migration Lambda's seed_models command takes it. Refuses blank prices."""
    if not isinstance(raw, list) or not raw:
        raise SeedFailed("aiModels must be a non-empty list")
    models, blank = [], []
    for entry in raw:
        name = str(entry.get("name") or "")
        for field in ("priceIn", "priceOut"):
            if str(entry.get(field) or "").strip() == "":
                blank.append(f"{name} {field}")
        models.append(
            {
                "name": name,
                "profile_id": str(entry.get("profileId") or ""),
                "jobs": list(entry.get("jobs") or []),
                "price_in": str(entry.get("priceIn") or "").strip(),
                "price_out": str(entry.get("priceOut") or "").strip(),
            }
        )
    if blank:
        raise SeedFailed(
            "Set the model prices in infra/Pulumi.dev.yaml (aiModels, US dollars per million "
            "tokens) before seeding. Blank: " + ", ".join(blank)
        )
    return models


def seed_from_config(raw: Any) -> dict[str, Any]:
    platform = raw.get("platform") if isinstance(raw, dict) else None
    tenants = raw.get("tenants") if isinstance(raw, dict) else None
    if not isinstance(platform, dict) or not isinstance(tenants, list):
        raise SeedFailed("seed must have platform and tenants")
    users = platform.get("users") or []
    if sorted(u.get("role") for u in users) != sorted(PLATFORM_ROLES):
        raise SeedFailed("seed.platform.users must be one master-admin and one master-user")
    for tenant in tenants:
        if not tenant.get("name") or not tenant.get("admin"):
            raise SeedFailed("each seed tenant needs a name and an admin email")
        for user in tenant.get("users") or []:
            if user.get("role") not in TENANT_ROLES:
                raise SeedFailed(
                    f"{tenant['name']}: user roles must be tenant-admin or tenant-user"
                )
    return {"platform": platform, "tenants": tenants}


# --- Steps ---------------------------------------------------------------------------------


def check_models(session: boto3.Session, models: list[dict[str, Any]]) -> None:
    bedrock = session.client("bedrock-runtime", config=Config(read_timeout=60))
    for model in models:
        print(f"  test call: {model['name']} ({model['profile_id']}) ... ", end="", flush=True)
        bedrock.converse(
            modelId=model["profile_id"],
            messages=[{"role": "user", "content": [{"text": TEST_PROMPT}]}],
            inferenceConfig={"maxTokens": 10},
        )
        print("ok")


def invoke(client: LambdaClient, function: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.invoke(
        FunctionName=function,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body: Any = json.loads(response["Payload"].read() or b"null")
    if response.get("FunctionError") or not isinstance(body, dict):
        raise SeedFailed(f"{function} failed: {json.dumps(body)[:500]}")
    if body.get("error"):
        raise SeedFailed(f"{function} refused: {body['error']}")
    return body


def platform_user(cognito: CognitoIdentityProviderClient, pool: str, user: dict[str, Any]) -> str:
    """Create (or find) a platform-pool user in its group. Returns the Cognito sub."""
    email = str(user["email"]).strip()
    try:
        created = cognito.admin_create_user(
            UserPoolId=pool,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": str(user.get("name") or email)},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        attributes = created["User"].get("Attributes", [])
        print(f"  invited {email}")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "UsernameExistsException":
            raise
        attributes = cognito.admin_get_user(UserPoolId=pool, Username=email).get(
            "UserAttributes", []
        )
        print(f"  {email} already exists")
    sub = next((a["Value"] for a in attributes if a["Name"] == "sub"), None)
    if not sub:
        raise SeedFailed(f"Cognito returned no sub for {email}")
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=str(user["role"]))
    return str(sub)


def wait_for_tenants(client: LambdaClient, function: str, names: list[str]) -> dict[str, Any]:
    deadline = time.monotonic() + PROVISION_TIMEOUT_SECONDS
    while True:
        statuses = invoke(client, function, {"command": "status", "names": names})["tenants"]
        pending = [n for n in names if (statuses.get(n) or {}).get("status") == "provisioning"]
        failed = [n for n in names if (statuses.get(n) or {}).get("status") == "failed"]
        if failed:
            raise SeedFailed(
                "Provisioning failed for " + ", ".join(failed) + ". The failed step is in the "
                "provisioning task's log (/aws/ecs/assay-dev-provision). Retry from the Tenants "
                "screen or with scripts/add_tenant.py --retry <tenant id>, then run seed.py again."
            )
        if not pending:
            return dict(statuses)
        if time.monotonic() > deadline:
            raise SeedFailed("Provisioning is taking too long: " + ", ".join(pending))
        print(f"  {time.strftime('%H:%M:%S')}  provisioning: {', '.join(pending)}", flush=True)
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--stack", default=DEFAULT_STACK)
    args = parser.parse_args()
    stack = args.stack

    try:
        models = catalog_from_config(config_object("aiModels", stack))
        seed = seed_from_config(config_object("seed", stack))
    except SeedFailed as exc:
        print(exc, file=sys.stderr)
        return 1

    profile = pulumi("config", "get", "aws:profile", stack=stack)
    region = pulumi("config", "get", "aws:region", stack=stack)
    migrate_fn = pulumi("stack", "output", "migrate_function_name", stack=stack)
    seed_fn = pulumi("stack", "output", "seed_function_name", stack=stack)
    platform_pool = pulumi("stack", "output", "platform_user_pool_id", stack=stack)
    session = boto3.Session(profile_name=profile, region_name=region)
    started_by = session.client("sts").get_caller_identity()["Arn"]
    lam = session.client("lambda", config=Config(read_timeout=330, retries={"max_attempts": 0}))
    cognito = session.client("cognito-idp")

    try:
        print("1. Model catalog")
        check_models(session, models)
        result = invoke(lam, migrate_fn, {"command": "seed_models", "models": models})
        print(f"  catalog: {json.dumps(result.get('models'))}")

        print("2. Platform tenant and users")
        platform = seed["platform"]
        users = []
        for user in platform["users"]:
            sub = platform_user(cognito, platform_pool, user)
            users.append(
                {
                    "email": user["email"],
                    "name": user.get("name") or user["email"],
                    "role": user["role"],
                    "sub": sub,
                }
            )
        result = invoke(
            lam,
            seed_fn,
            {
                "command": "platform",
                "tenant_name": platform["name"],
                "users": users,
                "started_by": started_by,
            },
        )
        print(f"  platform tenant {result['tenant_id']}; new user rows: {result['users_created']}")

        print("3. Business tenants")
        tenants = [
            {
                "name": t["name"],
                "admin": {"name": t.get("adminName") or t["admin"], "email": t["admin"]},
            }
            for t in seed["tenants"]
        ]
        result = invoke(
            lam, seed_fn, {"command": "tenants", "tenants": tenants, "started_by": started_by}
        )
        for tenant in result["tenants"]:
            state = "added" if tenant["created"] else f"exists ({tenant['status']})"
            print(f"  {tenant['name']}: {state}")
        wait_for_tenants(lam, seed_fn, [t["name"] for t in tenants])
        print("  all active")

        print("4. Tenant users")
        for tenant in seed["tenants"]:
            tenant_users = [
                {"email": u["email"], "name": u.get("name") or u["email"], "role": u["role"]}
                for u in tenant.get("users") or []
            ]
            if not tenant_users:
                continue
            result = invoke(
                lam,
                seed_fn,
                {
                    "command": "invite_users",
                    "tenant_name": tenant["name"],
                    "users": tenant_users,
                    "started_by": started_by,
                },
            )
            for user in result["users"]:
                state = "invited" if user["created"] else "exists"
                print(f"  {tenant['name']}: {user['email']} {state}")

        print("5. Default models")
        result = invoke(lam, seed_fn, {"command": "defaults"})
        print(f"  tenants given the defaults: {result['tenants_given_defaults'] or 'none'}")
    except (SeedFailed, ClientError) as exc:
        print(f"\nSeeding stopped: {exc}", file=sys.stderr)
        return 1
    print("\nSeeded. Every step can be run again safely.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
