"""The parent side of the sandbox: start one confined child for one job, and distrust it.

`run(job, source)` starts `python -I -B -m assay_workers.sandbox.child <job> <source>`:

- as the sandbox user and group (`SANDBOX_UID`), with no supplementary groups: a different
  user from the parent, so the kernel refuses it the parent's environment and memory;
- with an empty environment: no credentials, no credentials address, no database settings;
- in its own session, with stdin closed, every other descriptor closed, and `/` as its
  directory;
- with `confine.confine` applied before its interpreter starts (limits, no new privileges,
  the seccomp filter that refuses it a network).

The parent enforces the wall-clock limit and kills the child's whole process group. It reads
at most `MAX_OUTPUT_BYTES` of output, and discards the child's stderr unread: it could hold
anything from the file.

Everything the child returns is untrusted (owner condition d). The output must be one JSON
object within the size limit that validates against the job's model, which has only
numbers, booleans and codes from fixed sets: no free text, and so no path or key the parent
could be led to use. Any other ending is a `SandboxError` with a short reason and nothing
from the child in it.

Each ending has its own name (`ending`), so the logs and the document's event say which it
was, and a limit doing its job never reads the same as a child that died some other way:

- `SandboxLimit`: stopped at one of the sandbox's own limits. `cpu_limit` (the kernel, at
  the CPU limit), `wall_clock_timeout` (the parent, at the wall clock), `memory_limit` (the
  child reported that the address-space limit stopped it), `output_limit` (more output than
  the parent accepts), `file_size_limit` (it tried to write a file).
- `SandboxKilled`: ended by a signal that is none of those: `signal_SIGSEGV`,
  `signal_SIGABRT`, `signal_SIGKILL` and so on. A crash in the parser, or a kill from
  outside the sandbox (the task running out of memory, for one). A native library that
  cannot get memory usually aborts, so the address-space limit can also end this way.
- `SandboxFailed` (`exit_<status>`), `SandboxBadOutput` (`bad_output`): it ended by itself,
  without a valid result.
- `SandboxNotStarted`: it never ran. That is the task's fault, not the file's.

Starting the child needs the parent to be root with CAP_SETUID and CAP_SETGID (to switch
user) and CAP_KILL (to stop it). If the switch is refused, the job does not run at all.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import selectors
import signal
import subprocess  # nosec B404  (starts the sandbox child: a fixed command, no shell)
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assay_workers.sandbox.confine import Limits, confine

# The unprivileged user the child runs as (workers/worker.Dockerfile creates it).
SANDBOX_UID = 10002
SANDBOX_GID = 10002
CHILD_MODULE = "assay_workers.sandbox.child"
MAX_OUTPUT_BYTES = 64 * 1024
# The child's exit status when its address-space limit stopped it (child.EXIT_MEMORY).
EXIT_MEMORY = 12
# A PDF of 100 MB cannot have more pages than this; a larger number is not a page count.
MAX_PAGES = 1_000_000
READ_CHUNK = 65536
# After its output ends, how long the child gets to exit before it is killed.
EXIT_GRACE_SECONDS = 5.0
# The child runs on Linux x86-64 whatever this module is imported on: its signals' names.
LINUX_SIGNALS = {
    4: "SIGILL",
    6: "SIGABRT",
    7: "SIGBUS",
    8: "SIGFPE",
    9: "SIGKILL",
    11: "SIGSEGV",
    15: "SIGTERM",
    24: "SIGXCPU",
    25: "SIGXFSZ",
    31: "SIGSYS",
}


# --- What a job may return ---------------------------------------------------------------


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InspectResult(_Result):
    """`problem` is a code from a fixed set, or None with a page count."""

    problem: Literal["not_a_pdf", "pdf_encrypted"] | None
    pages: int | None = Field(ge=1, le=MAX_PAGES)

    @model_validator(mode="after")
    def _one_or_the_other(self) -> InspectResult:
        if (self.problem is None) == (self.pages is None):
            raise ValueError("a problem or a page count, never both and never neither")
        return self


_Outcome = Literal[
    "allowed",
    "PermissionError",
    "OSError",
    "FileNotFoundError",
    "ProcessLookupError",
    "NotADirectoryError",
    "BlockingIOError",
]


class SelfTestResult(_Result):
    """What `jobs.selftest` reports about its own confinement. Names and numbers only."""

    uid: int | None = None
    gid: int | None = None
    groups: list[int] | None = Field(default=None, max_length=64)
    environment: list[str] | None = Field(default=None, max_length=64)
    forbidden_modules: list[str] | None = Field(default=None, max_length=16)
    socket: _Outcome | None = None
    parent_environment: _Outcome | None = None
    parent_memory_file: _Outcome | None = None
    parent_memory_read: _Outcome | None = None
    exec: _Outcome | None = None
    write_work: _Outcome | None = None
    write_tmp: _Outcome | None = None
    no_new_privs: int | None = None
    seccomp: int | None = None
    children: int | None = Field(default=None, ge=0, le=1000)


# The child is started with an empty environment. CPython then sets this one variable for
# itself when the locale is "C" (PEP 538), so it is the only name a child may report.
SET_BY_THE_INTERPRETER = frozenset({"LC_CTYPE"})


def inherited_environment(report: SelfTestResult) -> list[str]:
    """The names in the child's environment that the interpreter did not put there itself."""
    return sorted(set(report.environment or []) - SET_BY_THE_INTERPRETER)


RESULTS: dict[str, type[_Result]] = {"inspect": InspectResult, "selftest": SelfTestResult}


# --- Errors -----------------------------------------------------------------------------


class SandboxError(Exception):
    """The child did not return a valid result. `reason` is a short code from this module,
    safe to log and store; it never holds anything the child wrote."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


CPU_LIMIT = "cpu_limit"
WALL_CLOCK_TIMEOUT = "wall_clock_timeout"
MEMORY_LIMIT = "memory_limit"
OUTPUT_LIMIT = "output_limit"
FILE_SIZE_LIMIT = "file_size_limit"
LIMITS = frozenset({CPU_LIMIT, WALL_CLOCK_TIMEOUT, MEMORY_LIMIT, OUTPUT_LIMIT, FILE_SIZE_LIMIT})
BAD_OUTPUT = "bad_output"


class SandboxLimit(SandboxError):
    """Stopped at one of the sandbox's own limits, which is the limit doing its job."""

    def __init__(self, limit: str) -> None:
        if limit not in LIMITS:
            raise ValueError("not a sandbox limit")
        super().__init__(limit)


class SandboxKilled(SandboxError):
    """Ended by a signal that is not one of the sandbox's limits: a crash in the parser, or
    a kill from outside the sandbox. Never the name of a limit."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(f"signal_{LINUX_SIGNALS.get(signal_number, signal_number)}")


class SandboxFailed(SandboxError):
    """It exited by itself with a failure status, and no result."""

    def __init__(self, returncode: int) -> None:
        super().__init__(f"exit_{returncode}")


class SandboxBadOutput(SandboxError):
    """It exited well, but what it printed is not a valid result."""

    def __init__(self) -> None:
        super().__init__(BAD_OUTPUT)


class SandboxNotStarted(SandboxError):
    """The child could not be started as the sandbox user. The job did not run."""

    def __init__(self) -> None:
        super().__init__("not_started")


# --- Starting the child --------------------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    timed_out: bool = False
    overflow: bool = False
    # Processor time the child (and anything it started and waited for) used, in seconds.
    cpu_seconds: float = 0.0


Spawn = Callable[[Sequence[str], Limits], Completed]


def child_command(job: str, source: Path) -> list[str]:
    """Isolated mode: no environment variables, no user site directory, no script directory
    on the path. `-B`: no bytecode is written."""
    return [sys.executable, "-I", "-B", "-m", CHILD_MODULE, job, str(source)]


def _children_cpu_seconds() -> float:
    """Processor time used so far by this process's finished children."""
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    import resource

    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def _kill(process: subprocess.Popen[bytes]) -> None:
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def spawn(argv: Sequence[str], limits: Limits) -> Completed:
    """Run the child confined, and collect at most MAX_OUTPUT_BYTES of its output. Linux."""
    try:
        cpu_before = _children_cpu_seconds()
        process = subprocess.Popen(  # noqa: S603  # nosec B603  (a fixed command)
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            cwd="/",
            close_fds=True,
            user=SANDBOX_UID,
            group=SANDBOX_GID,
            extra_groups=[],
            umask=0o077,
            start_new_session=True,
            # Runs in the child after the switch of user and before exec. This process is
            # single-threaded, which is what makes preexec_fn safe.
            preexec_fn=functools.partial(confine, limits),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SandboxNotStarted from exc

    assert process.stdout is not None and process.stderr is not None  # noqa: S101  # nosec B101
    deadline = time.monotonic() + limits.wall_seconds
    output = bytearray()
    timed_out = overflow = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        while selector.get_map() and not (timed_out or overflow):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _events in selector.select(remaining):
                data = os.read(key.fd, READ_CHUNK)
                if not data:
                    selector.unregister(key.fileobj)
                elif key.fileobj is process.stdout:
                    output += data
                    if len(output) > MAX_OUTPUT_BYTES:
                        overflow = True
                        break
                # stderr is read only so the child never blocks on it, and is dropped.
    if timed_out or overflow:
        _kill(process)
    try:
        returncode = process.wait(max(deadline - time.monotonic(), 0) + EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process)
        returncode = process.wait()
    finally:
        # Anything the child left running in its process group goes with it.
        _kill(process)
        process.stdout.close()
        process.stderr.close()
    return Completed(
        returncode=returncode,
        stdout=bytes(output),
        timed_out=timed_out,
        overflow=overflow,
        # One child at a time, so the difference is this child's.
        cpu_seconds=_children_cpu_seconds() - cpu_before,
    )


# --- Running a job ---------------------------------------------------------------------------


SIGKILL, SIGXCPU, SIGXFSZ = 9, 24, 25


def ending(done: Completed, limits: Limits) -> SandboxError | None:
    """How a child that did not end well ended, by name. None if it exited with status 0.

    The parent's own two stops come first: it knows it made them. Then the kernel's:
    SIGXCPU is the CPU limit and SIGXFSZ the file-size limit, by definition. A SIGKILL is
    the CPU limit only if the child had used its CPU time (it ignored SIGXCPU and reached
    the hard limit); any other SIGKILL came from somewhere else and is reported as that.
    """
    if done.timed_out:
        return SandboxLimit(WALL_CLOCK_TIMEOUT)
    if done.overflow or len(done.stdout) > MAX_OUTPUT_BYTES:
        return SandboxLimit(OUTPUT_LIMIT)
    if done.returncode < 0:
        number = -done.returncode
        if number == SIGXCPU:
            return SandboxLimit(CPU_LIMIT)
        if number == SIGXFSZ:
            return SandboxLimit(FILE_SIZE_LIMIT)
        if number == SIGKILL and done.cpu_seconds >= limits.cpu_seconds:
            return SandboxLimit(CPU_LIMIT)
        return SandboxKilled(number)
    if done.returncode == EXIT_MEMORY:
        return SandboxLimit(MEMORY_LIMIT)
    if done.returncode != 0:
        return SandboxFailed(done.returncode)
    return None


def run(job: str, source: Path, *, limits: Limits | None = None, spawn: Spawn = spawn) -> _Result:
    """One job on one file, in a confined child. Returns the validated result or raises
    `SandboxError`."""
    model = RESULTS[job]
    limits = limits or Limits()
    done = spawn(child_command(job, source), limits)
    error = ending(done, limits)
    if error is not None:
        raise error
    try:
        document = json.loads(done.stdout.decode("utf-8", errors="strict"))
    except ValueError:
        raise SandboxBadOutput from None
    if not isinstance(document, dict):
        raise SandboxBadOutput
    try:
        return model.model_validate(document)
    except ValueError:
        raise SandboxBadOutput from None


def inspect(source: Path, *, limits: Limits | None = None, spawn: Spawn = spawn) -> InspectResult:
    result = run("inspect", source, limits=limits, spawn=spawn)
    assert isinstance(result, InspectResult)  # noqa: S101  # nosec B101
    return result


def selftest(probe: str, *, limits: Limits | None = None, spawn: Spawn = spawn) -> SelfTestResult:
    """`probe` is one of jobs.SELFTESTS. It names no file: the child opens none."""
    result = run("selftest", Path(probe), limits=limits, spawn=spawn)
    assert isinstance(result, SelfTestResult)  # noqa: S101  # nosec B101
    return result
