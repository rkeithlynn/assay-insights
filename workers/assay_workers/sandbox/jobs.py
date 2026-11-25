"""The only code that opens an untrusted file. Runs in the sandbox child, never elsewhere.

Each job takes the path of one file and returns a small dictionary of plain values: codes
from a fixed set and numbers. No text from the file, no path, no key: the parent validates
the result against the job's model (`runner.RESULTS`) and uses nothing else.

- `inspect` (build step 6, second half, part 1): is this a PDF that opens, how many pages
  has it, does it need a password? It moved here from the fingerprint Lambda, which has one
  user and its credentials in its environment, and so must never open a PDF.
- `selftest`: reports what the child can see and do, and can misbehave on request. The image
  tests and the isolation run in AWS use it to show the confinement holds where it runs.
"""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any

PDF_MAGIC = b"%PDF-"
# The PDF header may follow a little leading junk; PDF readers accept up to 1 KB of it.
HEADER_WINDOW = 1024
# pypdfium2.raw.FPDF_ERR_PASSWORD: the document needs a password to open.
PDFIUM_PASSWORD_ERROR = 4

NOT_A_PDF = "not_a_pdf"
PDF_ENCRYPTED = "pdf_encrypted"
# Libraries the child must never have loaded: AWS clients and database drivers.
FORBIDDEN_MODULES = ("boto3", "botocore", "psycopg", "sqlalchemy", "assay_db", "assay_tss")


def inspect(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        head = handle.read(HEADER_WINDOW)
    if PDF_MAGIC not in head:
        return {"problem": NOT_A_PDF, "pages": None}
    import pypdfium2

    try:
        pdf = pypdfium2.PdfDocument(path)
    except pypdfium2.PdfiumError as exc:
        if getattr(exc, "err_code", None) == PDFIUM_PASSWORD_ERROR:
            return {"problem": PDF_ENCRYPTED, "pages": None}
        return {"problem": NOT_A_PDF, "pages": None}
    try:
        return {"problem": None, "pages": len(pdf)}
    finally:
        pdf.close()


# --- selftest -----------------------------------------------------------------------------


def _attempt(action: Any) -> str:
    """ "allowed", or the name of the error that stopped it."""
    try:
        action()
    except OSError as exc:
        return type(exc).__name__
    return "allowed"


def _read_one_byte(path: str) -> None:
    with Path(path).open("rb") as handle:
        handle.read(1)


def _open_socket() -> None:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()


def _read_parent_memory() -> None:
    """process_vm_readv on the parent: refused by the filter, and by the kernel's own rule
    that one user may not read another's process."""

    class IoVec(ctypes.Structure):
        _fields_ = (("base", ctypes.c_void_p), ("len", ctypes.c_size_t))

    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(8)
    local = IoVec(ctypes.cast(buffer, ctypes.c_void_p), 8)
    remote = IoVec(ctypes.c_void_p(0x400000), 8)
    result = libc.process_vm_readv(os.getppid(), ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "process_vm_readv refused")


def _status(field: str) -> int | None:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1])
    return None


def _report() -> dict[str, Any]:
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    # Where a directory happens to be writable, the file-size limit (zero) stops the write.
    # Take that as an error to report, not as the signal that would end this process.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    parent = os.getppid()
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "groups": sorted(os.getgroups()),
        "environment": sorted(os.environ),
        "forbidden_modules": sorted(m for m in FORBIDDEN_MODULES if m in sys.modules),
        "socket": _attempt(_open_socket),
        "parent_environment": _attempt(lambda: _read_one_byte(f"/proc/{parent}/environ")),
        "parent_memory_file": _attempt(lambda: _read_one_byte(f"/proc/{parent}/mem")),
        "parent_memory_read": _attempt(_read_parent_memory),
        "exec": _attempt(lambda: os.execv("/bin/true", ["/bin/true"])),  # noqa: S606  # nosec B606
        "write_work": _attempt(lambda: Path("/work/sandbox-selftest").write_bytes(b"x")),
        "write_tmp": _attempt(lambda: Path("/tmp/sandbox-selftest").write_bytes(b"x")),  # noqa: S108  # nosec B108
        "no_new_privs": _status("NoNewPrivs"),
        "seccomp": _status("Seccomp"),
    }


def _spin() -> dict[str, Any]:
    """Uses the processor without end: only the CPU limit, or the wall clock, stops it."""
    while True:
        pass


def _sleep() -> dict[str, Any]:
    """Waits without end and uses no processor time: only the parent's wall clock stops it."""
    while True:
        time.sleep(3600)


def _exhaust_memory() -> dict[str, Any]:
    hoard = []
    while True:
        hoard.append(bytearray(64 * 1024 * 1024))


def _flood() -> dict[str, Any]:
    chunk = "x" * 65536
    while True:
        sys.stdout.write(chunk)


def _fork_bomb() -> dict[str, Any]:
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    children = 0
    try:
        while children < 1000:
            if os.fork() == 0:
                while True:
                    pass
            children += 1
    except OSError:
        pass
    return {"children": children}


SELFTESTS = {
    "report": _report,
    "spin": _spin,
    "sleep": _sleep,
    "memory": _exhaust_memory,
    "flood": _flood,
    "fork": _fork_bomb,
}


def selftest(path: Path) -> dict[str, Any]:
    """The probe's name is the file's name: `/dev/null/..` is never opened."""
    return SELFTESTS[path.name]()


JOBS = {"inspect": inspect, "selftest": selftest}
