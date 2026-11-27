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

**The page channel** (stage 2). Two jobs send more than a small result: `text` (each page's
text layer) and `render` (pages drawn as pictures for Textract). For those, and only those,
the child inherits one more descriptor: the write end of a pipe (`spawn_with_channel`). It
writes one JSON record per line, and `Channel` hands the parent each line as it is finished,
so the parent holds one record at a time. Everything above still holds: the child writes no
file, has no network, and its standard output is still one small object of numbers. A
record is untrusted too: a strict model, the pages that were asked for and in that order, a
cap on each record (1 MB for a page, 8 MB for a picture) and on everything one child sends.
Text that arrives this way is data for the tenant's own text object; it never becomes a
path, a key, SQL or a log line. A picture is checked for its first eight bytes and its
length, and is never decoded here. A child that sends too much ends as `output_limit`; one
that sends anything else ends as `bad_output`.
"""

from __future__ import annotations

import base64
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

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


class PagesResult(_Result):
    """What a job that writes to the page channel prints at the end: how many pages it sent."""

    pages: int = Field(ge=0, le=MAX_PAGES)


RESULTS: dict[str, type[_Result]] = {
    "inspect": InspectResult,
    "selftest": SelfTestResult,
    "text": PagesResult,
    "render": PagesResult,
    "pageprobe": PagesResult,
}


# --- The page channel: what a record may hold -------------------------------------------------
#
# Two jobs send more than a small result (stage 2): the text of each page's text layer, and
# pages drawn as pictures for Textract. They write records to a pipe this module opens, one
# JSON object per line, and this module reads them as they arrive, one at a time. Standard
# output keeps its rule: one small object of numbers and codes.
#
# A record is as untrusted as a result. It must validate against its model; it must be for
# the next page that was asked for; its text must be encodable and within its cap; a picture
# must begin as a PNG does and be within its cap. Text is data: it is stored for its tenant
# and never becomes a path, a key, SQL or a log line. A picture is forwarded unopened.

# One page's text, in UTF-8, and one page record with its word positions.
MAX_PAGE_TEXT_BYTES = 256 * 1024
MAX_PAGE_RECORD_BYTES = 1024 * 1024
MAX_WORDS = 10_000
MAX_WORD_CHARS = 200
# Positions are ten-thousandths of the page, from its top left corner.
UNITS = 10_000
MAX_OBJECTS = 20_000
MAX_ANNOTATIONS = 5_000
# One page's picture, and its record (the picture in base64, and a few numbers).
MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGE_RECORD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_SIDE = 10_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# How many pages one child is asked for.
TEXT_PAGES_PER_CHILD = 25
RENDER_PAGES_PER_CHILD = 8

_Unit = Annotated[int, Field(ge=0, le=UNITS)]
_Word = tuple[_Unit, _Unit, _Unit, _Unit, Annotated[str, Field(max_length=MAX_WORD_CHARS)]]


class PageRecord(_Result):
    """One page's text layer, as the `text` job reports it."""

    kind: Literal["page"]
    page: int = Field(ge=1, le=MAX_PAGES)
    # In points, as the page is displayed.
    width: float = Field(gt=0, le=1_000_000)
    height: float = Field(gt=0, le=1_000_000)
    rotation: Literal[0, 90, 180, 270]
    objects: int = Field(ge=0, le=MAX_OBJECTS)
    # The share of the page that images cover.
    image_coverage: float = Field(ge=0, le=1)
    # Annotations that draw something: form fields, stamps, free text, ink.
    annotations: int = Field(ge=0, le=MAX_ANNOTATIONS)
    # The page has more text than a record holds; it is sent without any.
    oversize: bool
    text: str = Field(max_length=MAX_PAGE_TEXT_BYTES)
    # left, top, width, height, the word.
    words: list[_Word] = Field(max_length=MAX_WORDS)


class ImageRecord(_Result):
    kind: Literal["image"]
    page: int = Field(ge=1, le=MAX_PAGES)
    width: int = Field(ge=1, le=MAX_IMAGE_SIDE)
    height: int = Field(ge=1, le=MAX_IMAGE_SIDE)
    dpi: int = Field(ge=1, le=1200)
    png: str = Field(min_length=1, max_length=MAX_IMAGE_RECORD_BYTES)


@dataclass(frozen=True)
class PageImage:
    """One page drawn by the `render` job: PNG bytes this module never decodes."""

    page: int
    width: int
    height: int
    dpi: int
    data: bytes = field(repr=False)


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
    # The page channel carried something that is not a record this module accepts.
    refused: bool = False


Spawn = Callable[[Sequence[str], Limits], Completed]


def child_command(job: str, source: Path, *arguments: str) -> list[str]:
    """Isolated mode: no environment variables, no user site directory, no script directory
    on the path. `-B`: no bytecode is written."""
    return [sys.executable, "-I", "-B", "-m", CHILD_MODULE, job, str(source), *arguments]


class ChannelOverflow(Exception):
    """More came down the page channel than the parent accepts."""


class RecordRefused(Exception):
    """What came down the page channel is not a record the parent accepts."""


class Channel:
    """The parent's end of the page channel: bytes in, whole lines out, one at a time.

    It holds at most one unfinished line (`max_line` bytes) and passes each finished line to
    `on_line` before it reads further, so the parent never holds more than one record however
    many the child sends. `max_total` bounds everything one child may send.
    """

    def __init__(self, on_line: Callable[[bytes], None], *, max_line: int, max_total: int) -> None:
        self._on_line = on_line
        self._max_line = max_line
        self._max_total = max_total
        self._pending = bytearray()
        self._scanned = 0
        self.total = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        if self.total > self._max_total:
            raise ChannelOverflow
        self._pending += data
        while True:
            end = self._pending.find(b"\n", self._scanned)
            if end < 0:
                self._scanned = len(self._pending)
                if len(self._pending) > self._max_line:
                    raise ChannelOverflow
                return
            if end > self._max_line:
                raise ChannelOverflow
            line = bytes(self._pending[:end])
            del self._pending[: end + 1]
            self._scanned = 0
            self._on_line(line)

    def finish(self) -> None:
        """The child closed its end. A line it never finished is not a record."""
        if self._pending:
            raise RecordRefused


# Starts a child that is given the page channel: `command(descriptor)` is its command line,
# once the descriptor it will write to is known.
ChannelSpawn = Callable[[Callable[[int], Sequence[str]], Limits, Channel], Completed]


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
    return _spawn(lambda _descriptor: argv, limits, None)


def spawn_with_channel(
    command: Callable[[int], Sequence[str]], limits: Limits, channel: Channel
) -> Completed:
    """The same, for a job that also writes to the page channel: the child inherits one more
    descriptor, the write end of a pipe, and `channel` is fed what arrives on the other end
    as it arrives."""
    return _spawn(command, limits, channel)


def _close(*descriptors: int | None) -> None:
    for descriptor in descriptors:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _spawn(
    command: Callable[[int], Sequence[str]], limits: Limits, channel: Channel | None
) -> Completed:
    read_end: int | None = None
    write_end: int | None = None
    try:
        cpu_before = _children_cpu_seconds()
        if channel is not None:
            read_end, write_end = os.pipe()
        process = subprocess.Popen(  # noqa: S603  # nosec B603  (a fixed command)
            list(command(-1 if write_end is None else write_end)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={},
            cwd="/",
            close_fds=True,
            # The page channel's write end, and nothing else, stays open in the child.
            pass_fds=() if write_end is None else (write_end,),
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
        _close(read_end, write_end)
        raise SandboxNotStarted from exc
    # Only the child holds the write end now, so the channel ends when the child does.
    _close(write_end)

    assert process.stdout is not None and process.stderr is not None  # noqa: S101  # nosec B101
    deadline = time.monotonic() + limits.wall_seconds
    output = bytearray()
    timed_out = overflow = refused = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            if read_end is not None:
                selector.register(read_end, selectors.EVENT_READ)
            while selector.get_map() and not (timed_out or overflow or refused):
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
                    elif channel is not None and key.fileobj == read_end:
                        try:
                            channel.feed(data)
                        except ChannelOverflow:
                            overflow = True
                            break
                        except RecordRefused:
                            refused = True
                            break
                    # stderr is read only so the child never blocks on it, and is dropped.
        if timed_out or overflow or refused:
            _kill(process)
        try:
            returncode = process.wait(max(deadline - time.monotonic(), 0) + EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(process)
            returncode = process.wait()
        if channel is not None and not (timed_out or overflow or refused):
            try:
                channel.finish()
            except RecordRefused:
                refused = True
    finally:
        # Anything the child left running in its process group goes with it, also when the
        # parent's own handling of a record failed (a full disk, say) and is on its way up.
        _kill(process)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(EXIT_GRACE_SECONDS)
        process.stdout.close()
        process.stderr.close()
        _close(read_end)
    return Completed(
        returncode=returncode,
        stdout=bytes(output),
        timed_out=timed_out,
        overflow=overflow,
        refused=refused,
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
    if done.refused:
        # The parent stopped it for what it sent down the page channel.
        return SandboxBadOutput()
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


# --- Jobs that write to the page channel ---------------------------------------------------

# Drawing a page takes longer than reading its text.
RENDER_LIMITS = Limits(wall_seconds=120.0, cpu_seconds=90)


def _checked_text(record: PageRecord) -> PageRecord:
    """Text that can be stored and read back: valid UTF-8, within the cap, no NUL."""
    try:
        size = len(record.text.encode("utf-8", errors="strict"))
        "".join(word[4] for word in record.words).encode("utf-8", errors="strict")
    except ValueError:
        raise RecordRefused from None
    if size > MAX_PAGE_TEXT_BYTES or "\x00" in record.text:
        raise RecordRefused
    if record.oversize and (record.text or record.words):
        raise RecordRefused
    return record


def _checked_image(record: ImageRecord) -> PageImage:
    """The picture's bytes, checked for their first eight and their length only. This module
    never decodes a picture."""
    try:
        data = base64.b64decode(record.png, validate=True)
    except ValueError:
        raise RecordRefused from None
    if not data.startswith(PNG_SIGNATURE) or len(data) > MAX_IMAGE_BYTES:
        raise RecordRefused
    return PageImage(
        page=record.page, width=record.width, height=record.height, dpi=record.dpi, data=data
    )


def _pages[R: _Result, T](
    job: str,
    source: Path,
    arguments: Sequence[str],
    *,
    expect: Sequence[int],
    model: type[R],
    check: Callable[[R], T],
    handle: Callable[[T], None],
    max_record: int,
    limits: Limits | None,
    spawn: ChannelSpawn,
) -> None:
    """Run a page job and hand `handle` each record as it arrives: exactly the pages in
    `expect`, in that order, each once, or `SandboxError`. `handle` has finished with one
    record before the next is read."""
    limits = limits or Limits()
    waiting = list(reversed(expect))

    def on_line(line: bytes) -> None:
        try:
            record = model.model_validate_json(line)
        except ValueError:
            raise RecordRefused from None
        if not waiting or getattr(record, "page", None) != waiting.pop():
            raise RecordRefused
        handle(check(record))

    channel = Channel(on_line, max_line=max_record, max_total=len(expect) * (max_record + 1))
    done = spawn(
        lambda descriptor: child_command(job, source, str(descriptor), *arguments), limits, channel
    )
    error = ending(done, limits)
    if error is not None:
        raise error
    try:
        result = PagesResult.model_validate_json(done.stdout)
    except ValueError:
        raise SandboxBadOutput from None
    if waiting or result.pages != len(expect):
        raise SandboxBadOutput


def read_text(
    source: Path,
    first: int,
    last: int,
    handle: Callable[[PageRecord], None],
    *,
    limits: Limits | None = None,
    spawn: ChannelSpawn = spawn_with_channel,
) -> None:
    """The text layer of pages `first` to `last` (from 1), one record a page, in order."""
    _pages(
        "text",
        source,
        [str(first), str(last)],
        expect=range(first, last + 1),
        model=PageRecord,
        check=_checked_text,
        handle=handle,
        max_record=MAX_PAGE_RECORD_BYTES,
        limits=limits,
        spawn=spawn,
    )


def render(
    source: Path,
    pages: Sequence[int],
    handle: Callable[[PageImage], None],
    *,
    limits: Limits | None = None,
    spawn: ChannelSpawn = spawn_with_channel,
) -> None:
    """Each of `pages` drawn as a PNG, in the order given."""
    _pages(
        "render",
        source,
        [",".join(str(page) for page in pages)],
        expect=list(pages),
        model=ImageRecord,
        check=_checked_image,
        handle=handle,
        max_record=MAX_IMAGE_RECORD_BYTES,
        limits=limits or RENDER_LIMITS,
        spawn=spawn,
    )


def pageprobe(
    probe: str,
    *,
    limits: Limits | None = None,
    spawn: ChannelSpawn = spawn_with_channel,
) -> PageRecord | None:
    """`probe` is one of jobs.PAGE_PROBES: a child that misbehaves on the page channel, or
    reports about itself in a page record's numbers. One page is asked for; the record that
    arrives, if one does, is returned."""
    got: list[PageRecord] = []
    _pages(
        "pageprobe",
        Path(probe),
        [],
        expect=[1],
        model=PageRecord,
        check=_checked_text,
        handle=got.append,
        max_record=MAX_PAGE_RECORD_BYTES,
        limits=limits,
        spawn=spawn,
    )
    return got[0] if got else None
