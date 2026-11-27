"""The worker task's image (workers/worker.Dockerfile), run as the task runs it: as root with
every capability dropped except SETUID, SETGID and KILL, on a read-only root filesystem, with
`/work` as its only writable directory.

Owner condition (c), 2026-11-24. With the real sandbox child, it shows that the child:

- runs as the sandbox user with an empty environment, although the parent's environment
  holds credentials and a token;
- has no AWS or database library loaded;
- cannot open a network socket, read the parent's environment or memory, start another
  program, or write a file;
- is stopped at the wall-clock timeout, the memory limit, the output limit and the process
  limit;
- opens a real PDF, holds a password-protected one, and answers `not_a_pdf` for a malformed
  one, without the parent ever opening it.

And that if the parent cannot switch user (not root, or without CAP_SETUID), the job does
not run at all.

Needs Docker. Skipped when Docker is not available, unless `ASSAY_REQUIRE_IMAGE_TESTS=1`
(set in CI), in which case it fails.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from assay_workers.sandbox import runner
from tests.unit.test_migrate_image import REQUIRED, _docker_available, docker

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = "workers/worker.Dockerfile"
TASK_CAPABILITIES = ("SETUID", "SETGID", "KILL")
# What the task's main process has in its environment that a child must never see.
SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "parent-secret-value",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/parent-credentials-id",
    "WORKER_TOKEN": "parent-worker-token",
    "DB_HOST": "db.invalid",
}

pytestmark = pytest.mark.skipif(
    not REQUIRED and not _docker_available(), reason="Docker is not available"
)


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = f"assay-test-{uuid.uuid4().hex[:12]}"
    docker(
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "-f",
        str(REPO_ROOT / DOCKERFILE),
        "-t",
        tag,
        str(REPO_ROOT),
    )
    try:
        yield tag
    finally:
        docker("rmi", "-f", tag, check=False)


def as_the_task(
    image: str,
    code: str,
    *,
    user: str = "0:0",
    capabilities: tuple[str, ...] = TASK_CAPABILITIES,
    arguments: tuple[str, ...] = (),
) -> Any:
    """Run `code` in the task's main process and return the JSON it prints last."""
    args = [
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--user",
        user,
        "--cap-drop",
        "ALL",
        "--read-only",
        "--tmpfs",
        "/work:rw,mode=0755",
    ]
    for capability in capabilities:
        args += ["--cap-add", capability]
    for key, value in SECRETS.items():
        args += ["-e", f"{key}={value}"]
    result = docker(*args, image, "python", "-c", code, *arguments, timeout=300)
    return json.loads(result.stdout.strip().splitlines()[-1])


REPORT = """
import json
from assay_workers.sandbox import runner
from assay_workers.worker.__main__ import harden
harden()
report = runner.selftest("report")
status = dict(
    line.split(":", 1) for line in open("/proc/self/status").read().splitlines() if ":" in line
)
print(json.dumps({"child": report.model_dump(), "parent": {
    "uid": __import__("os").getuid(),
    "cap_eff": status["CapEff"].strip(), "cap_bnd": status["CapBnd"].strip(),
}}))
"""


@pytest.fixture(scope="module")
def report(image: str) -> dict[str, Any]:
    result: dict[str, Any] = as_the_task(image, REPORT)
    return result


def test_the_parent_is_root_with_three_capabilities_and_nothing_else(
    report: dict[str, Any],
) -> None:
    parent = report["parent"]
    assert parent["uid"] == 0
    # CAP_KILL (5), CAP_SETGID (6), CAP_SETUID (7).
    assert int(parent["cap_eff"], 16) == 0b1110_0000
    assert int(parent["cap_bnd"], 16) == 0b1110_0000


def test_the_child_is_another_user_with_an_empty_environment(report: dict[str, Any]) -> None:
    child = report["child"]
    assert (child["uid"], child["gid"]) == (runner.SANDBOX_UID, runner.SANDBOX_GID)
    assert child["groups"] in ([], [runner.SANDBOX_GID])
    # The parent's environment holds credentials and a token. The child was given nothing:
    # the one name it has is the one CPython sets for itself under the "C" locale.
    assert set(child["environment"]) <= runner.SET_BY_THE_INTERPRETER
    assert not set(child["environment"]) & set(SECRETS)


def test_the_child_has_no_aws_or_database_library_loaded(report: dict[str, Any]) -> None:
    assert report["child"]["forbidden_modules"] == []


def test_the_child_cannot_open_a_network_socket(report: dict[str, Any]) -> None:
    assert report["child"]["socket"] == "PermissionError"


def test_the_child_cannot_read_the_parents_environment_or_memory(report: dict[str, Any]) -> None:
    child = report["child"]
    assert child["parent_environment"] == "PermissionError"
    assert child["parent_memory_file"] == "PermissionError"
    assert child["parent_memory_read"] == "PermissionError"


def test_the_child_cannot_start_a_program_gain_privileges_or_write_a_file(
    report: dict[str, Any],
) -> None:
    child = report["child"]
    assert child["exec"] == "PermissionError"
    assert child["no_new_privs"] == 1
    assert child["seccomp"] == 2  # filter mode
    assert child["write_work"] != "allowed" and child["write_tmp"] != "allowed"


HOSTILE = """
import json, sys
from assay_workers.sandbox import runner
from assay_workers.sandbox.confine import Limits
from assay_workers.worker import probe as worker_probe
short = Limits(wall_seconds=4.0, cpu_seconds=3, address_space_bytes=512 * 1024**2, processes=8)
# The probe's own limits for the two time checks: only one of the two can be reached.
limits = {
    "spin": worker_probe.CPU_LIMITED,
    "stubborn": worker_probe.CPU_LIMITED,
    "sleep": worker_probe.WALL_LIMITED,
}
results = {}
for probe in sys.argv[1:]:
    try:
        chosen = limits.get(probe, short)
        results[probe] = {"returned": runner.selftest(probe, limits=chosen).model_dump()}
    except runner.SandboxError as exc:
        results[probe] = {"error": type(exc).__name__, "reason": exc.reason}
print(json.dumps(results))
"""


@pytest.fixture(scope="module")
def hostile(image: str) -> dict[str, Any]:
    result: dict[str, Any] = as_the_task(
        image, HOSTILE, arguments=("spin", "stubborn", "sleep", "crash", "memory", "flood", "fork")
    )
    return result


def test_a_child_that_spins_is_stopped_by_the_cpu_limit_and_named_so(
    hostile: dict[str, Any],
) -> None:
    """With the wall clock far off, only the CPU limit can be what stopped it: the kernel's
    SIGXCPU at the soft limit. The parent reports the limit by name, not the signal."""
    assert hostile["spin"] == {"error": "SandboxLimit", "reason": "cpu_limit"}


def test_a_child_that_ignores_the_cpu_warning_is_still_stopped_and_named_so(
    hostile: dict[str, Any],
) -> None:
    """It ignores SIGXCPU, so the kernel kills it at the hard limit a second later. That is
    a SIGKILL like any other; the CPU time it had used tells the parent which."""
    assert hostile["stubborn"] == {"error": "SandboxLimit", "reason": "cpu_limit"}


def test_a_child_that_only_waits_is_stopped_at_the_wall_clock_and_named_so(
    hostile: dict[str, Any],
) -> None:
    """It uses no CPU time, so only the parent's wall clock can be what stopped it."""
    assert hostile["sleep"] == {"error": "SandboxLimit", "reason": "wall_clock_timeout"}


def test_a_child_that_crashes_is_an_unexpected_signal_never_a_limit(
    hostile: dict[str, Any],
) -> None:
    assert hostile["crash"] == {"error": "SandboxKilled", "reason": "signal_SIGABRT"}


def test_a_child_that_tries_to_exhaust_memory_is_stopped(hostile: dict[str, Any]) -> None:
    assert hostile["memory"] == {"error": "SandboxLimit", "reason": "memory_limit"}


def test_a_child_that_floods_its_output_is_stopped(hostile: dict[str, Any]) -> None:
    assert hostile["flood"] == {"error": "SandboxLimit", "reason": "output_limit"}


def test_a_child_cannot_fill_the_task_with_processes(hostile: dict[str, Any]) -> None:
    """The process limit stops it well short of what it asked for, and the parent kills the
    whole process group when the child is done."""
    result = hostile["fork"]
    if "returned" in result:
        assert result["returned"]["children"] < 8
    else:
        assert result["error"] in ("SandboxLimit", "SandboxKilled")


INSPECT = """
import base64, json, os, sys
from pathlib import Path
from assay_workers.sandbox import runner
files = {n: base64.b64decode(e) for n, e in zip(sys.argv[1::2], sys.argv[2::2])}
normal = files["normal"]
# Malformed in several ways, made here so they need not travel on the command line: a PDF
# header and then nothing a parser can use; a real PDF cut off half way; one with its
# cross-reference table overwritten; one that promises a deeply nested structure.
files["garbage"] = b"%PDF-1.7 " + bytes(range(256)) * 64
files["truncated"] = normal[: len(normal) // 2]
files["corrupted"] = normal.replace(b"xref", b"xxxx").replace(b"obj", b"0bj")
files["nested"] = b"%PDF-1.7 1 0 obj " + b"[" * 200000 + b"]" * 200000 + b" endobj "
results = {}
for name, body in files.items():
    directory = Path("/work") / name
    directory.mkdir(mode=0o755)
    path = directory / "document.pdf"
    path.write_bytes(body)
    os.chmod(path, 0o444)
    try:
        results[name] = runner.inspect(path).model_dump()
    except runner.SandboxError as exc:
        results[name] = {"error": type(exc).__name__, "reason": exc.reason}
print(json.dumps(results))
"""


def synthetic_files() -> dict[str, bytes]:
    script = REPO_ROOT / "scripts" / "make_test_files.py"
    spec = importlib.util.spec_from_file_location("make_test_files_for_image", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    made: dict[str, bytes] = module.files(None)
    return made


def test_the_child_opens_real_pdfs_and_survives_malformed_ones(image: str) -> None:
    made = synthetic_files()
    normal = made["01-normal.pdf"]
    files = {
        "normal": normal,
        "protected": made["03-password-protected.pdf"],
        "protected128": made["07-password-protected-128.pdf"],
        "text": made["06-not-a-pdf.pdf"],
    }
    arguments: list[str] = []
    for name, body in files.items():
        arguments += [name, base64.b64encode(body).decode()]
    results = as_the_task(image, INSPECT, arguments=tuple(arguments))

    assert results["normal"] == {"problem": None, "pages": 3}
    assert results["protected"] == {"problem": "pdf_encrypted", "pages": None}
    assert results["protected128"] == {"problem": "pdf_encrypted", "pages": None}
    assert results["text"] == {"problem": "not_a_pdf", "pages": None}
    for name in ("garbage", "truncated", "corrupted", "nested"):
        outcome = results[name]
        # Either the parser refuses it, or the child is stopped. It is never a page count
        # the parent goes on to use, unless PDFium truly recovered a document from it.
        assert (
            outcome.get("problem") == "not_a_pdf"
            or outcome.get("error") in ("SandboxKilled", "SandboxLimit", "SandboxFailed")
            or (outcome.get("problem") is None and isinstance(outcome.get("pages"), int))
        ), (name, outcome)
    assert results["garbage"].get("problem") == "not_a_pdf" or "error" in results["garbage"]


# --- The page channel (stage 2): the real `text` and `render` jobs, and a hostile child ----------

PAGES = """
import base64, json, os, sys
from pathlib import Path
from assay_workers.sandbox import runner
from assay_workers.sandbox.confine import Limits
from assay_workers.worker.__main__ import harden
harden()
files = {n: base64.b64decode(e) for n, e in zip(sys.argv[1::2], sys.argv[2::2])}
files["garbage"] = b"%PDF-1.7 " + bytes(range(256)) * 64
open_before = sorted(os.listdir("/proc/self/fd"))
results = {}
for name, body in files.items():
    directory = Path("/work") / name
    directory.mkdir(mode=0o755)
    path = directory / "document.pdf"
    path.write_bytes(body)
    os.chmod(path, 0o444)
    pages, images = [], []
    try:
        count = runner.inspect(path).pages or 0
        runner.read_text(path, 1, count, pages.append)
        runner.render(path, list(range(1, count + 1)), images.append)
        results[name] = {
            "pages": [
                [p.page, len(p.text), p.image_coverage, p.annotations, len(p.words)] for p in pages
            ],
            "first_words": [w[4] for w in pages[0].words[:2]],
            "images": [
                [i.page, i.width, i.height, i.dpi, i.data[:8] == runner.PNG_SIGNATURE, len(i.data)]
                for i in images
            ],
        }
    except runner.SandboxError as exc:
        results[name] = {"error": type(exc).__name__, "reason": exc.reason}
hostile = {}
short = Limits(wall_seconds=10.0, cpu_seconds=8, address_space_bytes=512 * 1024**2)
for probe in ("flood", "wrong_page", "not_a_record", "descriptors", "write_a_file"):
    try:
        record = runner.pageprobe(probe, limits=short)
        hostile[probe] = {"objects": None if record is None else record.objects}
    except runner.SandboxError as exc:
        hostile[probe] = {"error": type(exc).__name__, "reason": exc.reason}
# The child wrote no file, anywhere the parent can look, and the parent kept no pipe open.
left = sorted(str(p.relative_to("/work")) for p in Path("/work").rglob("*") if p.is_file())
print(json.dumps({
    "results": results, "hostile": hostile, "left": left,
    "descriptors_kept": sorted(os.listdir("/proc/self/fd")) == open_before,
}))
"""


@pytest.fixture(scope="module")
def paged(image: str) -> dict[str, Any]:
    made = synthetic_files()
    files = {
        "normal": made["01-normal.pdf"],
        "scanned": made["08-scanned.pdf"],
        "mixed": made["09-mixed.pdf"],
        "form": made["10-filled-form.pdf"],
    }
    arguments: list[str] = []
    for name, body in files.items():
        arguments += [name, base64.b64encode(body).decode()]
    result: dict[str, Any] = as_the_task(image, PAGES, arguments=tuple(arguments))
    return result


def test_the_real_child_sends_each_pages_text_and_facts_down_the_page_channel(
    paged: dict[str, Any],
) -> None:
    results = paged["results"]
    normal = results["normal"]
    # page, characters of text, image coverage, drawn annotations, words.
    assert [p[0] for p in normal["pages"]] == [1, 2, 3]
    assert all(p[1] > 200 and p[2] == 0 and p[3] == 0 and p[4] > 20 for p in normal["pages"])
    assert normal["first_words"] == ["SYNTHETIC", "TEST"]
    # Pictures only: no text layer, and an image over the whole page.
    assert [p[1:4] for p in results["scanned"]["pages"]] == [[0, 1.0, 0]] * 3
    first, second, third = results["mixed"]["pages"]
    assert first[2] == 0 and 0.40 <= second[2] <= 0.45 and third[2] == 1.0
    assert second[1] > 100 and third[1] > 20  # each still has its typed lines
    [form] = results["form"]["pages"]
    assert form[3] == 3 and form[1] > 100  # three form fields; the questions are typed


def test_the_real_child_draws_each_page_as_a_png_and_writes_no_file(paged: dict[str, Any]) -> None:
    for name, pages in (("normal", 3), ("scanned", 3), ("mixed", 3), ("form", 1)):
        images = paged["results"][name]["images"]
        assert [i[0] for i in images] == list(range(1, pages + 1)), name
        for _page, width, height, dpi, is_png, size in images:
            assert (width, height, dpi) == (1700, 2200, 200), name
            assert is_png and 1000 < size < runner.MAX_IMAGE_BYTES, name
    # Nothing but the PDFs the parent put there: the child left no picture and no text.
    assert paged["left"] == [
        "form/document.pdf",
        "garbage/document.pdf",
        "mixed/document.pdf",
        "normal/document.pdf",
        "scanned/document.pdf",
    ]
    assert paged["descriptors_kept"] is True


def test_a_malformed_pdf_never_becomes_a_page_record(paged: dict[str, Any]) -> None:
    outcome = paged["results"]["garbage"]
    assert outcome.get("error") in (
        "SandboxFailed",
        "SandboxKilled",
        "SandboxLimit",
        "SandboxBadOutput",
    ), outcome


def test_a_child_that_misbehaves_on_the_page_channel_is_stopped_or_refused_by_name(
    paged: dict[str, Any],
) -> None:
    hostile = paged["hostile"]
    assert hostile["flood"] == {"error": "SandboxLimit", "reason": "output_limit"}
    assert hostile["wrong_page"] == {"error": "SandboxBadOutput", "reason": "bad_output"}
    assert hostile["not_a_record"] == {"error": "SandboxBadOutput", "reason": "bad_output"}


def test_a_page_job_has_the_channel_and_nothing_else_of_the_parents_and_still_writes_no_file(
    paged: dict[str, Any],
) -> None:
    hostile = paged["hostile"]
    # Standard input, output and error, and the page channel: four descriptors.
    assert hostile["descriptors"] == {"objects": 4}
    # It could write a page's picture to no file: the page channel is the only way out.
    assert hostile["write_a_file"] == {"objects": 0}


NOT_STARTED = """
import json
from pathlib import Path
from assay_workers.sandbox import runner
try:
    runner.selftest("report")
    print(json.dumps({"ran": True}))
except runner.SandboxError as exc:
    print(json.dumps({"ran": False, "error": type(exc).__name__, "reason": exc.reason}))
"""


@pytest.mark.parametrize(
    ("user", "capabilities"),
    [
        ("10001:10001", TASK_CAPABILITIES),  # not root
        ("0:0", ()),  # root, but with no capability at all
        ("0:0", ("KILL",)),  # root without CAP_SETUID and CAP_SETGID
    ],
)
def test_if_the_parent_cannot_switch_user_the_job_does_not_run(
    image: str, user: str, capabilities: tuple[str, ...]
) -> None:
    result = as_the_task(image, NOT_STARTED, user=user, capabilities=capabilities)
    assert result == {"ran": False, "error": "SandboxNotStarted", "reason": "not_started"}


def test_the_task_refuses_to_start_without_its_three_values(image: str) -> None:
    """The entry point imports and runs as the task does, and logs JSON with a level."""
    result = docker(
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "KILL",
        "--read-only",
        "--tmpfs",
        "/work:rw,mode=0755",
        "-e",
        "AWS_REGION=us-east-1",
        "-e",
        "TSS_ENDPOINT_URL=https://tss.invalid/v1",
        image,
        check=False,
    )
    assert result.returncode == 1
    entry = json.loads(result.stdout.strip().splitlines()[-1])
    assert entry["level"] == "ERROR"
    assert entry["message"] == "the task was started without a document, attempt and token"


def test_the_image_has_the_tss_client_only_and_can_read_the_ca_bundle(image: str) -> None:
    code = (
        "import importlib.util, json, os\n"
        "import assay_workers.worker.probe, assay_workers.worker.run\n"
        "print(json.dumps({\n"
        "  'service': importlib.util.find_spec('assay_tss.service') is not None,\n"
        "  'policies': importlib.util.find_spec('assay_tss.policies') is not None,\n"
        "  'provision': importlib.util.find_spec('assay_tss.provision') is not None,\n"
        "  'pulumi': importlib.util.find_spec('pulumi') is not None,\n"
        "  'fastapi': importlib.util.find_spec('fastapi') is not None,\n"
        "  'pdfium': importlib.util.find_spec('pypdfium2') is not None,\n"
        "  'bundle': os.access(os.environ['DB_CA_BUNDLE'], os.R_OK),\n"
        "}))\n"
    )
    assert as_the_task(image, code) == {
        "service": False,
        "policies": False,
        "provision": False,
        "pulumi": False,
        "fastapi": False,
        "pdfium": True,
        "bundle": True,
    }
