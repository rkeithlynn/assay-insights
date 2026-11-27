"""The four images of build step 4, built for linux/amd64 and run as the users they run as.

- **Provisioning task** (tss/provision.Dockerfile): as uid 10001 with a read-only root
  filesystem and *no network*, it previews the real tenant stack program. That shows the
  Pulumi CLI and the AWS provider plugin in the image work without downloading anything,
  that their versions match uv.lock, and that every resource in the program is valid for
  the provider.
- **TSS Lambda** (tss/Dockerfile): imports as Lambda's non-root user, reads the RDS CA
  bundle, and ships no provisioning code and no Pulumi.
- **Isolation test runner** (tests/isolation/Dockerfile): pytest collects the isolation
  tests as Lambda's user on a read-only filesystem.
- **Cognito admin Lambda** (api/cognito-admin.Dockerfile): imports as Lambda's user and
  refuses anything but `invite` without calling AWS.

Needs Docker. Skipped when Docker is not available, unless `ASSAY_REQUIRE_IMAGE_TESTS=1`
(set in CI), in which case it fails.
"""

from __future__ import annotations

import json
import re
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_migrate_image import REQUIRED, _docker_available, docker

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVISION_DOCKERFILE = REPO_ROOT / "tss" / "provision.Dockerfile"
COGNITO_ADMIN_SOURCE = REPO_ROOT / "api" / "assay_api" / "cognito_admin.py"
TASK_USER = "10001:10001"
LAMBDA_USER = "993:990"
LAMBDA_PYTHON = "/var/lang/bin/python3"
BUNDLE = "/opt/rds-ca/global-bundle.pem"

requires_docker = pytest.mark.skipif(
    not REQUIRED and not _docker_available(), reason="Docker is not available"
)


def _build(dockerfile: str) -> Iterator[str]:
    tag = f"assay-test-{uuid.uuid4().hex[:12]}"
    docker(
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "-f",
        str(REPO_ROOT / dockerfile),
        "-t",
        tag,
        str(REPO_ROOT),
        timeout=1800,
    )
    try:
        yield tag
    finally:
        docker("rmi", "-f", tag, check=False)


@pytest.fixture(scope="module")
def provision_image() -> Iterator[str]:
    yield from _build("tss/provision.Dockerfile")


@pytest.fixture(scope="module")
def tss_image() -> Iterator[str]:
    yield from _build("tss/Dockerfile")


@pytest.fixture(scope="module")
def isolation_image() -> Iterator[str]:
    yield from _build("tests/isolation/Dockerfile")


@pytest.fixture(scope="module")
def cognito_admin_image() -> Iterator[str]:
    yield from _build("api/cognito-admin.Dockerfile")


@pytest.fixture(scope="module")
def ops_image() -> Iterator[str]:
    yield from _build("api/lambda.Dockerfile")


@pytest.fixture(scope="module")
def auth_trigger_image() -> Iterator[str]:
    yield from _build("api/auth-trigger.Dockerfile")


def last_json(stdout: str) -> Any:
    lines = [line for line in stdout.strip().splitlines() if line.startswith(("{", "["))]
    assert lines, f"no JSON in output:\n{stdout}"
    return json.loads(lines[-1])


def lambda_python(image: str, code: str, *, env: dict[str, str] | None = None) -> Any:
    """As Lambda runs it: non-root, read-only filesystem, and /tmp as the only writable path."""
    args = ["run", "--rm", "--platform", "linux/amd64", "--read-only", "--network", "none"]
    # A path inside the container, not on this machine.
    args += ["--tmpfs", "/tmp", "--user", LAMBDA_USER]  # noqa: S108
    args += ["--entrypoint", LAMBDA_PYTHON]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    return last_json(docker(*args, image, "-c", code).stdout)


# --- Pinned versions (no Docker needed) -------------------------------------------------------


def locked_version(package: str) -> str:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    [version] = [p["version"] for p in lock["package"] if p["name"] == package]
    return str(version)


def test_image_pins_match_the_lock_file() -> None:
    """The CLI and plugin in the image must be the versions the Python SDKs were locked to."""
    dockerfile = PROVISION_DOCKERFILE.read_text(encoding="utf-8")
    pulumi, pulumi_aws = locked_version("pulumi"), locked_version("pulumi-aws")
    assert f"/releases/sdk/pulumi-v{pulumi}-linux-x64.tar.gz" in dockerfile
    assert (
        f"/download/v{pulumi_aws}/pulumi-resource-aws-v{pulumi_aws}-linux-amd64.tar.gz"
        in dockerfile
    )
    assert dockerfile.count(f"resource-aws-v{pulumi_aws}") >= 3
    # Nothing is downloaded unchecked: every ADD from a URL carries a SHA-256.
    adds = re.findall(r"^ADD (.*)$", dockerfile, re.MULTILINE)
    assert len(adds) == 3
    assert all(re.match(r"--checksum=sha256:[0-9a-f]{64} ", add) for add in adds)


def test_every_dockerfile_copies_every_workspace_manifest() -> None:
    """`uv export --locked` needs each member's pyproject.toml, or the build fails."""
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = root["tool"]["uv"]["workspace"]["members"]
    for path in (
        "db/Dockerfile",
        "api/Dockerfile",
        "tss/Dockerfile",
        "tss/provision.Dockerfile",
        "tests/isolation/Dockerfile",
        "api/lambda.Dockerfile",
        "workers/lambda.Dockerfile",
        "workers/worker.Dockerfile",
    ):
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for member in members:
            line = f"COPY {member}/pyproject.toml {member}/pyproject.toml"
            assert line in text, f"{path} lacks: {line}"


# --- The provisioning task --------------------------------------------------------------------

# Runs inside the image: previews the real tenant program against a local backend, with a
# provider that never calls AWS. Everything else is as in the task.
PREVIEW = """
import json, os, uuid
from pathlib import Path
from assay_tss.policies import Principals
from assay_tss.provision.settings import ProvisionSettings
from assay_tss.provision.stack import StackRunner, prepare_pulumi_home

prepare_pulumi_home(os.environ)
role = "arn:aws:iam::000000000000:role"
state = Path(os.environ["WORK_DIR"]) / "state"
state.mkdir()
settings = ProvisionSettings(
    region="us-east-1",
    account_id="000000000000",
    name_prefix="assay-dev",
    principals=Principals(
        tenant_role_arn=f"{role}/assay-dev-tenant",
        break_glass_role_arn=f"{role}/assay-dev-break-glass",
        provisioning_role_arn=f"{role}/provisioning",
        scan_role_arn=f"{role}/scan",
    ),
    state_bucket="unused",
    state_key_arn="unused",
    invite_function="unused",
    vector_dimensions=1024,
    default_tags={"app": "assay-insights", "env": "dev"},
    work_dir=Path(os.environ["WORK_DIR"]),
    provider_args={
        "access_key": "AKIAFAKEFAKEFAKEFAKE",
        "secret_key": "fake",
        "skip_credentials_validation": True,
        "skip_requesting_account_id": True,
        "skip_metadata_api_check": True,
        "skip_region_validation": True,
        "allowed_account_ids": None,
    },
    backend_url=f"file://{state}",
    secrets_provider="passphrase",
    upload_origins=("https://portal.example.test",),
)
tenant = uuid.UUID("7f3a91c2-0000-4000-8000-000000000001")
runner = StackRunner(settings)
changes = runner.preview(tenant)
planned = sorted([c.name, c.op] for c in runner.plan(tenant))
print(json.dumps({"uid": os.getuid(), "changes": changes, "planned": planned}))
"""


def task_run(image: str, *command: str, env: dict[str, str] | None = None) -> str:
    """As the Fargate task runs it: non-root, read-only root, /work writable, no network."""
    args = ["run", "--rm", "--platform", "linux/amd64", "--read-only", "--network", "none"]
    args += ["--user", TASK_USER, "--cap-drop", "ALL"]
    args += ["--tmpfs", "/work:rw,exec,uid=10001,gid=10001,mode=0700"]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    return docker(*args, image, *command, timeout=900).stdout


@requires_docker
def test_task_image_has_the_locked_pulumi_cli_and_plugin(provision_image: str) -> None:
    version = task_run(provision_image, "pulumi", "version").strip()
    assert version == f"v{locked_version('pulumi')}"
    plugin = f"/opt/pulumi/plugins/resource-aws-v{locked_version('pulumi-aws')}/pulumi-resource-aws"
    listing = task_run(provision_image, "ls", "-l", plugin)
    assert listing.startswith("-r-xr-xr-x")
    sdk = task_run(
        provision_image,
        "python",
        "-c",
        "import json, importlib.metadata as m; "
        "print(json.dumps([m.version('pulumi'), m.version('pulumi-aws')]))",
    )
    assert last_json(sdk) == [locked_version("pulumi"), locked_version("pulumi-aws")]


@requires_docker
def test_task_previews_the_tenant_stack_offline_on_a_read_only_filesystem(
    provision_image: str,
) -> None:
    output = task_run(
        provision_image, "python", "-c", PREVIEW, env={"PULUMI_CONFIG_PASSPHRASE": "test-only"}
    )
    result = last_json(output)
    assert result["uid"] == 10001
    # The stack, the provider and the tenant's 15 resources (the CORS rule since build step
    # 6); nothing else, nothing changed.
    assert result["changes"] == {"create": 17}
    # The update command's preview (assay_tss.provision.update) sees each resource and its
    # operation through the engine's events.
    planned = {name: op for name, op in result["planned"]}
    assert planned["bucket-cors"] == "create"
    assert planned["malware-protection"] == "create"
    assert len(planned) >= 15


@requires_docker
def test_task_image_can_read_the_rds_ca_bundle_and_nothing_is_writable(
    provision_image: str,
) -> None:
    code = (
        "import json, os, stat, assay_tss.provision.task, assay_db.tenants; "
        f"st = os.stat('{BUNDLE}'); "
        "print(json.dumps({'mode': stat.S_IMODE(st.st_mode), "
        "'bundle': os.environ['DB_CA_BUNDLE'], "
        "'app_writable': os.access('/app', os.W_OK), "
        "'plugins_writable': os.access('/opt/pulumi/plugins', os.W_OK), "
        "'work_writable': os.access('/work', os.W_OK)}))"
    )
    result = last_json(task_run(provision_image, "python", "-c", code))
    assert result == {
        "mode": 0o444,
        "bundle": BUNDLE,
        "app_writable": False,
        "plugins_writable": False,
        "work_writable": True,
    }


@requires_docker
def test_task_refuses_to_start_without_a_request(provision_image: str) -> None:
    args = ["run", "--rm", "--platform", "linux/amd64", "--read-only", "--network", "none"]
    args += ["--user", TASK_USER, "--tmpfs", "/work:rw,uid=10001,gid=10001,mode=0700"]
    result = docker(*args, provision_image, check=False)
    assert result.returncode != 0
    assert "PROVISION_REQUEST" in result.stderr


# What the platform stack puts in the task's environment (assay_infra.provisioning).
TASK_ENVIRONMENT = {
    "AWS_REGION": "us-east-1",
    "ACCOUNT_ID": "000000000000",
    "NAME_PREFIX": "assay-dev",
    "TENANT_ROLE_ARN": "arn:aws:iam::000000000000:role/assay-dev-tenant",
    "BREAK_GLASS_ROLE_ARN": "arn:aws:iam::000000000000:role/assay-dev-break-glass",
    "PROVISIONING_ROLE_ARN": "arn:aws:iam::000000000000:role/provisioning",
    "SCAN_ROLE_ARN": "arn:aws:iam::000000000000:role/scan",
    "STATE_BUCKET": "state",
    "STATE_KEY_ARN": "arn:aws:kms:us-east-1:000000000000:key/state",
    "INVITE_FUNCTION": "invite",
    "VECTOR_DIMENSIONS": "1024",
    "DEFAULT_TAGS": '{"app":"assay-insights"}',
    "DEFAULT_INGEST_MODEL": "us.example.ingest-v1:0",
    "DEFAULT_QUERY_MODEL": "us.example.query-v1",
    "UPLOAD_ORIGINS": '["https://portal.example.test"]',
}


@requires_docker
def test_task_probe_command_never_passes_when_it_cannot_reach_aws(provision_image: str) -> None:
    """The isolation probe runs in this image. With no network every call fails, and a
    failed call is not a refusal: the probe must report errors and exit non-zero."""
    args = ["run", "--rm", "--platform", "linux/amd64", "--read-only", "--network", "none"]
    args += ["--user", TASK_USER, "--tmpfs", "/work:rw,uid=10001,gid=10001,mode=0700"]
    environment = TASK_ENVIRONMENT | {
        "PROVISION_COMMAND": "probe",
        "PROBE_TENANTS": "7f3a91c2-0000-4000-8000-000000000001",
        "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKEFAKE",
        "AWS_SECRET_ACCESS_KEY": "fake",
    }
    for key, value in environment.items():
        args += ["-e", f"{key}={value}"]
    result = docker(*args, provision_image, check=False, timeout=600)
    assert result.returncode == 1, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    checks = [line for line in lines if line.get("message") == "probe"]
    assert len(checks) == 14
    assert all(not check["ok"] for check in checks)
    assert all(check["outcome"].startswith("error:") for check in checks)
    [summary] = [line for line in lines if line.get("message") == "probe finished"]
    assert (summary["checks"], summary["failed"]) == (14, 14)


# --- The TSS Lambda ---------------------------------------------------------------------------


@requires_docker
def test_tss_image_imports_as_lambdas_user_without_provisioning_code(tss_image: str) -> None:
    code = (
        "import importlib.util, json, os, stat\n"
        "import assay_tss.handler, assay_db.connect, psycopg\n"
        f"st = os.stat('{BUNDLE}')\n"
        "print(json.dumps({'uid': os.getuid(), 'mode': stat.S_IMODE(st.st_mode),\n"
        "    'bundle': os.environ['DB_CA_BUNDLE'],\n"
        "    'handler': callable(assay_tss.handler.handler),\n"
        "    'provision': importlib.util.find_spec('assay_tss.provision') is not None,\n"
        "    'pulumi': importlib.util.find_spec('pulumi') is not None,\n"
        "    'pytest': importlib.util.find_spec('pytest') is not None}))\n"
    )
    assert lambda_python(tss_image, code) == {
        "uid": 993,
        "mode": 0o444,
        "bundle": BUNDLE,
        "handler": True,
        "provision": False,
        "pulumi": False,
        "pytest": False,
    }


@requires_docker
def test_tss_image_answers_without_leaking_when_it_has_no_settings(tss_image: str) -> None:
    """No environment at all: the handler must answer 500 with a plain code, not a traceback."""
    code = (
        "import json, assay_tss.handler as h\n"
        "r = h.handler({'httpMethod': 'POST', 'resource': '/resolve', 'body': '{}'}, None)\n"
        "print(json.dumps(r))\n"
    )
    response = lambda_python(tss_image, code)
    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "internal_error"}


# --- The isolation test runner ----------------------------------------------------------------


@requires_docker
def test_isolation_image_collects_the_tests_as_lambdas_user(isolation_image: str) -> None:
    code = (
        "import json, pytest\n"
        "from tests.isolation import runner\n"
        "class Seen:\n"
        "    def __init__(self): self.items = []\n"
        "    def pytest_collection_modifyitems(self, items):\n"
        "        self.items = [i.nodeid for i in items]\n"
        "seen = Seen()\n"
        "code = pytest.main([str(runner.TESTS_DIR), f'--rootdir={runner.TESTS_DIR}',\n"
        "    '-p', 'no:cacheprovider', '--collect-only', '-q'], plugins=[seen])\n"
        "print(json.dumps({'exit': int(code), 'count': len(seen.items),\n"
        "    'files': sorted({i.split('::')[0] for i in seen.items})}))\n"
    )
    result = lambda_python(isolation_image, code)
    assert result["exit"] == 0
    assert result["count"] >= 40
    # `test_textract.py` since stage 2: the Textract endpoint refuses the runner's role.
    assert result["files"] == [
        "test_rows.py",
        "test_storage.py",
        "test_textract.py",
        "test_tss_api.py",
    ]


# --- The Cognito admin Lambda -----------------------------------------------------------------


@requires_docker
def test_cognito_admin_image_serves_only_invite(cognito_admin_image: str) -> None:
    code = (
        "import importlib.util, json, os\n"
        "os.environ['OPERATIONS'] = 'invite'  # as on the provisioning role's function\n"
        "from assay_api import cognito_admin\n"
        "print(json.dumps({'uid': os.getuid(),\n"
        "    'other': cognito_admin.handler({'operation': 'disable'}, None),\n"
        "    'files': sorted(os.listdir(os.path.dirname(cognito_admin.__file__))),\n"
        "    'fastapi': importlib.util.find_spec('fastapi') is not None}))\n"
    )
    result = lambda_python(cognito_admin_image, code)
    assert result["uid"] == 993
    assert result["other"] == {"error": "operation_not_allowed"}
    assert [f for f in result["files"] if f.endswith(".py")] == [
        "__init__.py",
        "claims.py",
        "cognito_admin.py",
    ]
    assert result["fastapi"] is False


@requires_docker
def test_cognito_admin_images_boto3_has_every_call_the_lambda_makes(
    cognito_admin_image: str,
) -> None:
    """The image installs nothing: it uses the boto3 bundled in the pinned Lambda base image,
    not the version in uv.lock. AdminDeleteSoftwareToken (August 2026) is newer than many
    bundled versions, so every Cognito call in the source must exist in the image's boto3."""
    calls = sorted(set(re.findall(r"\.(admin_\w+)\(", COGNITO_ADMIN_SOURCE.read_text())))
    assert "admin_delete_software_token" in calls
    code = (
        "import json, boto3\n"
        "client = boto3.client('cognito-idp', region_name='us-east-1')\n"
        f"calls = {calls!r}\n"
        "print(json.dumps({'boto3': boto3.__version__,\n"
        "    'missing': [c for c in calls if not hasattr(client, c)]}))\n"
    )
    result = lambda_python(cognito_admin_image, code)
    assert result["missing"] == [], f"boto3 {result['boto3']} in the image lacks them"


# --- Build step 5: the auth-event trigger, and the consumer and seed image -------------------


@requires_docker
def test_auth_trigger_image_holds_only_the_trigger(auth_trigger_image: str) -> None:
    code = (
        "import json, os\n"
        "from assay_api import cognito_auth_trigger as t\n"
        "event = {'triggerSource': 'PostAuthentication_Authentication', 'userPoolId': 'p',\n"
        "         'request': {'userAttributes': {'sub': 's'}}}\n"
        "print(json.dumps({'uid': os.getuid(),\n"
        "    'returned': t.handler(event, None) == event,\n"
        "    'files': sorted(os.listdir(os.path.dirname(t.__file__)))}))\n"
    )
    # No network and no QUEUE_URL: the send fails, and the sign-in must still go ahead.
    result = lambda_python(auth_trigger_image, code)
    assert result["uid"] == 993
    assert result["returned"] is True
    assert [f for f in result["files"] if f.endswith(".py")] == [
        "__init__.py",
        "cognito_auth_trigger.py",
    ]


@requires_docker
def test_ops_image_runs_the_consumer_and_the_seed_without_fastapi(ops_image: str) -> None:
    code = (
        "import importlib.util, json, os\n"
        "import assay_api.auth_event_consumer, assay_api.seed\n"
        "print(json.dumps({'uid': os.getuid(),\n"
        "    'bundle': os.access(os.environ['DB_CA_BUNDLE'], os.R_OK),\n"
        "    'fastapi': importlib.util.find_spec('fastapi') is not None,\n"
        "    'seed': assay_api.seed.handler({'command': 'nope'}, None)}))\n"
    )
    result = lambda_python(ops_image, code)
    assert result["uid"] == 993
    assert result["bundle"] is True
    assert result["fastapi"] is False
    assert "unknown command" in result["seed"]["error"]


# --- Build step 6: the pipeline's Lambda image ------------------------------------------------


@pytest.fixture(scope="module")
def workers_image() -> Iterator[str]:
    yield from _build("workers/lambda.Dockerfile")


@requires_docker
def test_workers_image_runs_every_handler_as_lambdas_user(workers_image: str) -> None:
    """Every pipeline handler imports as uid 993 on a read-only filesystem; the image has no
    PDF library at all (a Lambda never opens a PDF: build step 6, second half); it has the
    TSS client and nothing of the service or the provisioning code; and the reserved
    document ID is refused before anything is reached (no network here)."""
    code = (
        "import importlib.util, json, os\n"
        "from assay_workers import scan, starter, steps, sweeper\n"
        "from assay_tss import keys\n"
        "reserved = str(keys.RESERVED_DOCUMENT_ID)\n"
        "event = {'tenant_id': reserved, 'document_id': reserved}\n"
        "refused = steps.handle({'step': 'fingerprint', **event}, None)\n"
        "others = [steps.handle({'step': s, **event}, None) for s in ('claim', 'release')]\n"
        "print(json.dumps({'uid': os.getuid(),\n"
        "    'pdfium': importlib.util.find_spec('pypdfium2') is not None,\n"
        "    'others': others,\n"
        "    'bundle': os.access(os.environ['DB_CA_BUNDLE'], os.R_OK),\n"
        "    'service': importlib.util.find_spec('assay_tss.service') is not None,\n"
        "    'provision': importlib.util.find_spec('assay_tss.provision') is not None,\n"
        "    'pulumi': importlib.util.find_spec('pulumi') is not None,\n"
        "    'fastapi': importlib.util.find_spec('fastapi') is not None,\n"
        "    'refused': refused}))\n"
    )
    result = lambda_python(workers_image, code)
    assert result["uid"] == 993
    assert result["pdfium"] is False
    assert result["others"] == [{"outcome": "refused"}, {"outcome": "refused"}]
    assert result["bundle"] is True
    assert (result["service"], result["provision"], result["pulumi"], result["fastapi"]) == (
        False,
        False,
        False,
        False,
    )
    assert result["refused"] == {"outcome": "refused"}
