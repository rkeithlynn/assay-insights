"""The sandbox, as far as it can be checked without Linux: what the parent accepts from a
child, how the child is started, the seccomp program, and which modules may open a PDF.

The child itself (another user, no network, the limits) is checked where it runs:
tests/unit/test_worker_image.py, in the task's image, and the isolation run in AWS.
"""

from __future__ import annotations

import ast
import errno
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from assay_workers.sandbox import confine, jobs, runner
from assay_workers.sandbox.confine import Limits
from assay_workers.sandbox.runner import Completed

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ROOT / "workers" / "assay_workers"
SOURCE = Path("/work/job/document.pdf")


def spawn_returning(done: Completed) -> runner.Spawn:
    def spawn(argv: Sequence[str], limits: Limits) -> Completed:
        return done

    return spawn


def output(document: Any, returncode: int = 0) -> runner.Spawn:
    return spawn_returning(Completed(returncode=returncode, stdout=json.dumps(document).encode()))


# --- Everything the child returns is untrusted (owner condition d) ---------------------------


def test_a_valid_result_is_returned() -> None:
    result = runner.inspect(SOURCE, spawn=output({"problem": None, "pages": 3}))
    assert (result.problem, result.pages) == (None, 3)
    held = runner.inspect(SOURCE, spawn=output({"problem": "pdf_encrypted", "pages": None}))
    assert held.problem == "pdf_encrypted"


@pytest.mark.parametrize(
    "document",
    [
        # Not the shape.
        [],
        "ok",
        None,
        {},
        {"pages": 3},
        {"problem": None},
        # Both or neither.
        {"problem": None, "pages": None},
        {"problem": "not_a_pdf", "pages": 3},
        # A code that is not one of the two, or a path or key in its place.
        {"problem": "other", "pages": None},
        {"problem": "../../etc/passwd", "pages": None},
        {"problem": "originals/00000000-0000-4000-8000-000000000001.pdf", "pages": None},
        # Page counts that are not page counts.
        {"problem": None, "pages": 0},
        {"problem": None, "pages": -1},
        {"problem": None, "pages": runner.MAX_PAGES + 1},
        {"problem": None, "pages": "3"},
        {"problem": None, "pages": 3.0},
        {"problem": None, "pages": True},
        # Anything extra, whatever it is called.
        {"problem": None, "pages": 3, "path": "/work/other.pdf"},
        {"problem": None, "pages": 3, "key": "originals/x.pdf"},
        {"problem": None, "pages": 3, "text": "Jane Smith"},
        {"problem": None, "pages": 3, "tenant_id": "7f3a91c2-0000-4000-8000-000000000001"},
    ],
)
def test_output_that_is_not_exactly_a_result_is_refused(document: Any) -> None:
    with pytest.raises(runner.SandboxBadOutput) as refused:
        runner.inspect(SOURCE, spawn=output(document))
    assert refused.value.reason == "bad_output"
    assert not isinstance(refused.value, runner.SandboxLimit)


@pytest.mark.parametrize(
    "stdout", [b"", b"not json", b"\xff\xfe", b'{"problem": null, "pages": 3} trailing', b"{" * 9]
)
def test_output_that_is_not_json_is_refused(stdout: bytes) -> None:
    with pytest.raises(runner.SandboxBadOutput):
        runner.inspect(SOURCE, spawn=spawn_returning(Completed(returncode=0, stdout=stdout)))


def test_the_result_has_no_free_text_at_all() -> None:
    """No field of any job's result can hold a path, a key or text from a file: every
    string is one of a fixed set."""

    def fixed(annotation: Any) -> bool:
        text = str(annotation)
        return "str" not in text.replace("list[str]", "") or "Literal" in text

    for model in runner.RESULTS.values():
        for name, field in model.model_fields.items():
            if model is runner.SelfTestResult and name in ("environment", "forbidden_modules"):
                continue  # the self-test's two lists of names, compared with [] and never used
            assert fixed(field.annotation), (model.__name__, name)
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("strict") is True


def test_too_much_output_is_the_output_limit() -> None:
    big = Completed(returncode=0, stdout=b"x" * (runner.MAX_OUTPUT_BYTES + 1))
    with pytest.raises(runner.SandboxLimit) as refused:
        runner.inspect(SOURCE, spawn=spawn_returning(big))
    assert refused.value.reason == "output_limit"
    flagged = Completed(returncode=-9, stdout=b"x", overflow=True)
    with pytest.raises(runner.SandboxLimit) as killed:
        runner.inspect(SOURCE, spawn=spawn_returning(flagged))
    assert killed.value.reason == "output_limit"


OK = b'{"problem": null, "pages": 3}'
# The default limits allow 45 seconds of CPU time.
CPU = Limits().cpu_seconds


@pytest.mark.parametrize(
    ("done", "error", "reason"),
    [
        # --- The sandbox's own limits, each by its own name --------------------------------
        # The parent stopped it at the wall clock (it knows: it did it).
        (
            Completed(returncode=-9, stdout=b"", timed_out=True),
            runner.SandboxLimit,
            "wall_clock_timeout",
        ),
        # The kernel's CPU limit: SIGXCPU at the soft limit...
        (Completed(returncode=-24, stdout=b"", cpu_seconds=CPU), runner.SandboxLimit, "cpu_limit"),
        # ...or, for a child that ignored that, SIGKILL at the hard limit, with the CPU
        # time used to show for it.
        (
            Completed(returncode=-9, stdout=b"", cpu_seconds=CPU + 1.0),
            runner.SandboxLimit,
            "cpu_limit",
        ),
        (Completed(returncode=-25, stdout=b""), runner.SandboxLimit, "file_size_limit"),
        (Completed(returncode=12, stdout=b""), runner.SandboxLimit, "memory_limit"),
        # --- Anything else is never the name of a limit ---------------------------------------
        # A SIGKILL with little CPU time used: not the CPU limit, whatever it was.
        (
            Completed(returncode=-9, stdout=b"", cpu_seconds=0.2),
            runner.SandboxKilled,
            "signal_SIGKILL",
        ),
        (Completed(returncode=-9, stdout=b""), runner.SandboxKilled, "signal_SIGKILL"),
        (Completed(returncode=-11, stdout=b""), runner.SandboxKilled, "signal_SIGSEGV"),
        (Completed(returncode=-6, stdout=b""), runner.SandboxKilled, "signal_SIGABRT"),
        (
            Completed(returncode=-7, stdout=b"", cpu_seconds=CPU * 2),
            runner.SandboxKilled,
            "signal_SIGBUS",
        ),
        (Completed(returncode=-200, stdout=b""), runner.SandboxKilled, "signal_200"),
        (Completed(returncode=1, stdout=b""), runner.SandboxFailed, "exit_1"),
        # A valid-looking result from a child that did not end well is not used.
        (Completed(returncode=1, stdout=OK), runner.SandboxFailed, "exit_1"),
        (
            Completed(returncode=0, stdout=OK, timed_out=True),
            runner.SandboxLimit,
            "wall_clock_timeout",
        ),
        (Completed(returncode=-11, stdout=OK), runner.SandboxKilled, "signal_SIGSEGV"),
    ],
)
def test_every_ending_has_its_own_name(
    done: Completed, error: type[runner.SandboxError], reason: str
) -> None:
    with pytest.raises(runner.SandboxError) as raised:
        runner.inspect(SOURCE, spawn=spawn_returning(done))
    assert type(raised.value) is error
    assert raised.value.reason == reason
    assert str(raised.value) == reason


def test_a_limit_and_an_unexpected_kill_never_share_a_name() -> None:
    """Whatever signal ends a child, an unexpected one is `signal_...`, and the names of the
    sandbox's limits are used for nothing else."""
    for number in range(1, 65):
        for cpu in (0.0, CPU - 0.1):
            done = Completed(returncode=-number, stdout=b"", cpu_seconds=cpu)
            error = runner.ending(done, Limits())
            assert error is not None
            if isinstance(error, runner.SandboxLimit):
                # Only the two signals that are limits by definition.
                assert number in (runner.SIGXCPU, runner.SIGXFSZ)
                assert error.reason in runner.LIMITS
            else:
                assert isinstance(error, runner.SandboxKilled)
                assert error.reason.startswith("signal_") and error.reason not in runner.LIMITS
    with pytest.raises(ValueError):
        runner.SandboxLimit("signal_SIGKILL")
    assert runner.ending(Completed(returncode=0, stdout=OK), Limits()) is None


def test_the_cpu_limit_warns_before_it_kills() -> None:
    """SIGXCPU at the soft limit names the ending; the hard limit follows a second later."""
    assert confine.CPU_HARD_MARGIN_SECONDS == 1
    source = (WORKERS / "sandbox" / "confine.py").read_text(encoding="utf-8")
    assert "(limits.cpu_seconds, limits.cpu_seconds + CPU_HARD_MARGIN_SECONDS)" in source


# --- How the child is started ---------------------------------------------------------------------


def test_the_child_runs_isolated_as_the_sandbox_user_with_an_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"] = argv
        seen.update(kwargs)
        raise PermissionError("not root")

    monkeypatch.setattr(subprocess, "Popen", popen)
    # The children's CPU time is read from the kernel, which only Linux has in this form.
    monkeypatch.setattr(runner, "_children_cpu_seconds", lambda: 0.0)
    # The switch of user was refused: the job does not run at all.
    with pytest.raises(runner.SandboxNotStarted) as refused:
        runner.inspect(SOURCE)
    assert refused.value.reason == "not_started"

    argv = seen["argv"]
    assert argv[1:5] == ["-I", "-B", "-m", "assay_workers.sandbox.child"]
    assert argv[5:] == ["inspect", SOURCE.as_posix()] or argv[5:] == ["inspect", str(SOURCE)]
    assert seen["env"] == {}
    assert (seen["user"], seen["group"], seen["extra_groups"]) == (10002, 10002, [])
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["close_fds"] is True and seen["start_new_session"] is True
    assert seen["cwd"] == "/"
    # The limits and the filter go on between fork and exec.
    assert seen["preexec_fn"].func is confine.confine
    assert isinstance(seen["preexec_fn"].args[0], Limits)


def test_the_default_limits() -> None:
    limits = Limits()
    assert limits.wall_seconds == 60 and limits.cpu_seconds < limits.wall_seconds
    assert limits.address_space_bytes == 2 * 1024**3
    assert limits.file_size_bytes == 0  # the child writes no file


# --- The seccomp program --------------------------------------------------------------------------


def evaluate(program: list[confine.Instruction], *, nr: int, arch: int, arg0: int = 0) -> int:
    """A classic-BPF interpreter for the few instructions the filter uses."""
    data = {confine.OFFSET_NR: nr, confine.OFFSET_ARCH: arch, confine.OFFSET_ARG0_LOW: arg0}
    accumulator, pc = 0, 0
    for _ in range(len(program) + 1):
        code, jt, jf, k = program[pc]
        pc += 1
        if code == confine.BPF_LD_W_ABS:
            accumulator = data[k]
        elif code == confine.BPF_RET_K:
            return k
        elif code == confine.BPF_JEQ_K:
            pc += jt if accumulator == k else jf
        elif code == confine.BPF_JGE_K:
            pc += jt if accumulator >= k else jf
        elif code == confine.BPF_JSET_K:
            pc += jt if accumulator & k else jf
        else:  # pragma: no cover
            raise AssertionError(f"unknown instruction {code:#x}")
    raise AssertionError("the program did not return")


X86_64 = confine.AUDIT_ARCH_X86_64
EPERM = confine.SECCOMP_RET_ERRNO | errno.EPERM
ENOSYS = confine.SECCOMP_RET_ERRNO | errno.ENOSYS
ALLOW = confine.SECCOMP_RET_ALLOW


@pytest.fixture(scope="module")
def first_filter() -> list[confine.Instruction]:
    return confine.build_filter(
        list(confine.DENIED.values()),
        enosys=[confine.SYS_CLONE3],
        clone_flags=confine.CLONE_NAMESPACE_FLAGS,
    )


def test_the_filter_refuses_the_network_and_other_processes(
    first_filter: list[confine.Instruction],
) -> None:
    for name, number in confine.DENIED.items():
        assert evaluate(first_filter, nr=number, arch=X86_64) == EPERM, name
    for name in ("socket", "connect", "bind", "ptrace", "process_vm_readv", "bpf", "unshare"):
        assert name in confine.DENIED


def test_the_filter_allows_what_a_parser_needs(first_filter: list[confine.Instruction]) -> None:
    # read, write, open, close, mmap, brk, openat, exit_group, execve (the child's own start).
    for number in (0, 1, 2, 3, 9, 12, 257, 231, confine.SYS_EXECVE):
        assert evaluate(first_filter, nr=number, arch=X86_64) == ALLOW, number


def test_the_filter_refuses_new_namespaces_and_other_abis(
    first_filter: list[confine.Instruction],
) -> None:
    clone = confine.SYS_CLONE
    assert evaluate(first_filter, nr=clone, arch=X86_64, arg0=0x00010F00) == ALLOW  # a thread
    for flag in (0x10000000, 0x40000000, 0x00020000, 0x20000000):  # user, net, mount, pid
        assert evaluate(first_filter, nr=clone, arch=X86_64, arg0=0x11 | flag) == EPERM, hex(flag)
    # clone3 hides its flags in a structure: "not implemented", so the C library uses clone.
    assert evaluate(first_filter, nr=confine.SYS_CLONE3, arch=X86_64) == ENOSYS
    # Another architecture's numbering, and the x32 ABI's.
    assert evaluate(first_filter, nr=41, arch=0x40000003) == confine.SECCOMP_RET_KILL_PROCESS
    assert evaluate(first_filter, nr=confine.X32_SYSCALL_BIT | 41, arch=X86_64) == EPERM


def test_the_second_filter_forbids_exec() -> None:
    program = confine.build_filter(list(confine.EXEC.values()))
    for number in confine.EXEC.values():
        assert evaluate(program, nr=number, arch=X86_64) == EPERM
    assert evaluate(program, nr=0, arch=X86_64) == ALLOW


def test_every_jump_fits_and_lands_inside_the_program(
    first_filter: list[confine.Instruction],
) -> None:
    jumps = (confine.BPF_JEQ_K, confine.BPF_JGE_K, confine.BPF_JSET_K)
    for index, (code, jt, jf, _k) in enumerate(first_filter):
        assert 0 <= jt <= 255 and jf == 0
        if code in jumps:
            assert index + 1 + jt < len(first_filter)
        else:
            assert jt == 0
    assert first_filter[-1][0] == confine.BPF_RET_K
    assert len(first_filter) < 4096  # BPF_MAXINSNS


def test_seccomp_is_refused_off_linux_instead_of_being_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(OSError):
        confine.install_filter(confine.build_filter([41]))


# --- Which modules may open a PDF, and what the child may load -----------------------------------

PDF_LIBRARIES = ("pypdfium2", "pyhanko", "pdfplumber", "pdfminer")
# The child's modules: the standard library, PDFium, and each other.
CHILD_MODULES = ("child.py", "jobs.py", "confine.py", "__init__.py")
CHILD_MAY_IMPORT = (
    "__future__",
    "assay_workers.sandbox",
    # Stage 2: a page's picture is written as a PNG with `zlib`, and sent in base64. Both are
    # the standard library; there is still no image library and nothing of the parent's.
    "base64",
    "zlib",
    "collections",
    "ctypes",
    "dataclasses",
    "errno",
    "json",
    "os",
    "pathlib",
    "pypdfium2",
    "resource",
    "signal",
    "socket",
    "sys",
    "time",
    "typing",
)


def imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_only_the_sandbox_jobs_import_a_pdf_library() -> None:
    offenders = []
    for tree in ("api/assay_api", "db/assay_db", "schemas/assay_schemas", "tss/assay_tss"):
        offenders += [p for p in (ROOT / tree).rglob("*.py") if _opens_pdfs(p)]
    offenders += [p for p in WORKERS.rglob("*.py") if _opens_pdfs(p)]
    assert [p.relative_to(ROOT).as_posix() for p in offenders] == [
        "workers/assay_workers/sandbox/jobs.py"
    ]


def _opens_pdfs(path: Path) -> bool:
    return any(name.startswith(PDF_LIBRARIES) for name in imports(path))


def test_the_child_imports_only_the_standard_library_pdfium_and_itself() -> None:
    for name in CHILD_MODULES:
        for module in imports(WORKERS / "sandbox" / name):
            assert module.startswith(CHILD_MAY_IMPORT), (name, module)
    # In particular: no AWS client, no database driver, nothing of the parent's.
    assert set(jobs.FORBIDDEN_MODULES) >= {"boto3", "botocore", "psycopg", "sqlalchemy"}


def test_the_parent_never_calls_a_sandbox_job_in_its_own_process() -> None:
    """`jobs` is imported by the child only; the parent reaches it through `runner`."""
    for path in (WORKERS / "worker").glob("*.py"):
        for module in imports(path):
            assert module != "assay_workers.sandbox.jobs", path.name
            assert module != "assay_workers.sandbox.child", path.name
    assert "assay_workers.sandbox.jobs" not in imports(WORKERS / "sandbox" / "runner.py")


def test_the_lambda_image_ships_no_pdf_library() -> None:
    dockerfile = (ROOT / "workers" / "lambda.Dockerfile").read_text(encoding="utf-8")
    assert "--extra worker" not in dockerfile
    assert "--package assay-workers --extra worker" in (
        ROOT / "workers" / "worker.Dockerfile"
    ).read_text(encoding="utf-8")
    manifest = (ROOT / "workers" / "pyproject.toml").read_text(encoding="utf-8")
    base, extras = manifest.split("[project.optional-dependencies]")
    assert "pypdfium2" not in base and "pypdfium2" in extras
