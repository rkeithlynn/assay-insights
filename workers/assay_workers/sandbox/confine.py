"""Confinement for the sandbox child: resource limits and a seccomp filter (Linux, x86-64).

Applied twice:

1. `confine(limits)` runs in the child between `fork` and `exec` (the parent's `preexec_fn`),
   after the switch to the sandbox user. It sets the resource limits, sets `no_new_privs`
   and installs the first filter. Because it is in place before the child's interpreter
   starts, it does not depend on anything the child's code does.
2. `forbid_exec()` is called by the child itself, once its interpreter is up and before it
   opens the file: from then on it cannot start another program.

The filter is a deny-list, returning EPERM. What matters most on Fargate is the network: a
task cannot give one process its own network namespace or firewall rule, so refusing
`socket` is the only way to keep the child off the network (the VPC endpoints, the database,
the task's credentials address). The rest removes ways to look into other processes and
kernel features a PDF parser has no use for. `clone3` answers ENOSYS, so the C library falls
back to `clone`, whose flags the filter can read: creating any new namespace is refused.

Nothing here is a substitute for the separate user and the empty environment: those are what
keep the credentials out of reach. This narrows what a compromised parser can try next.
"""

from __future__ import annotations

import ctypes
import errno
import sys
from collections.abc import Sequence
from dataclasses import dataclass

# --- Resource limits ----------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """What one child may use. The wall-clock limit is enforced by the parent, which kills
    the child's process group; the rest by the kernel."""

    wall_seconds: float = 60.0
    cpu_seconds: int = 45
    # Address space, the only per-process memory limit a Fargate task can set (it has no
    # cgroup of its own to give a child). The task's memory is the backstop.
    address_space_bytes: int = 2 * 1024**3
    # The child writes no file. stdout is a pipe, which this limit does not cover.
    file_size_bytes: int = 0
    open_files: int = 64
    # Counted per user: only sandbox children run as the sandbox user, one at a time.
    processes: int = 16


def apply_limits(limits: Limits) -> None:
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    import resource

    for name, value in (
        (resource.RLIMIT_CPU, limits.cpu_seconds),
        (resource.RLIMIT_AS, limits.address_space_bytes),
        (resource.RLIMIT_FSIZE, limits.file_size_bytes),
        (resource.RLIMIT_NOFILE, limits.open_files),
        (resource.RLIMIT_NPROC, limits.processes),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(name, (value, value))


# --- seccomp ------------------------------------------------------------------------------

# linux/filter.h, linux/seccomp.h, linux/audit.h, linux/prctl.h
BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_JSET_K = 0x45
BPF_RET_K = 0x06
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
AUDIT_ARCH_X86_64 = 0xC000003E
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
# struct seccomp_data: int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6].
OFFSET_NR = 0
OFFSET_ARCH = 4
OFFSET_ARG0_LOW = 16
# The x32 ABI's syscall numbers have this bit set; none is allowed.
X32_SYSCALL_BIT = 0x40000000

# x86-64 syscall numbers (arch/x86/entry/syscalls/syscall_64.tbl).
SYS_CLONE = 56
SYS_CLONE3 = 435
SYS_EXECVE = 59
SYS_EXECVEAT = 322
NETWORK = {
    "socket": 41,
    "connect": 42,
    "accept": 43,
    "bind": 49,
    "listen": 50,
    "socketpair": 53,
    "accept4": 288,
}
OTHER_PROCESSES = {
    "ptrace": 101,
    "process_vm_readv": 310,
    "process_vm_writev": 311,
    "kcmp": 312,
    "pidfd_send_signal": 424,
    "pidfd_open": 434,
    "pidfd_getfd": 438,
}
KERNEL_FEATURES = {
    "personality": 135,
    "pivot_root": 155,
    "chroot": 161,
    "acct": 163,
    "settimeofday": 164,
    "mount": 165,
    "umount2": 166,
    "swapon": 167,
    "swapoff": 168,
    "reboot": 169,
    "iopl": 172,
    "ioperm": 173,
    "init_module": 175,
    "delete_module": 176,
    "quotactl": 179,
    "clock_settime": 227,
    "kexec_load": 246,
    "add_key": 248,
    "request_key": 249,
    "keyctl": 250,
    "unshare": 272,
    "perf_event_open": 298,
    "name_to_handle_at": 303,
    "open_by_handle_at": 304,
    "setns": 308,
    "finit_module": 313,
    "kexec_file_load": 320,
    "bpf": 321,
    "userfaultfd": 323,
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "open_tree": 428,
    "move_mount": 429,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "fspick": 433,
}
DENIED = {**NETWORK, **OTHER_PROCESSES, **KERNEL_FEATURES}
EXEC = {"execve": SYS_EXECVE, "execveat": SYS_EXECVEAT}
# clone(2) flags that create a namespace: NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID,
# NEWNET (linux/sched.h).
CLONE_NAMESPACE_FLAGS = (
    0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000
)

Instruction = tuple[int, int, int, int]  # code, jt, jf, k


def build_filter(
    denied: Sequence[int], *, enosys: Sequence[int] = (), clone_flags: int = 0
) -> list[Instruction]:
    """A classic-BPF seccomp program: EPERM for `denied`, ENOSYS for `enosys`, EPERM for a
    `clone` with any of `clone_flags`, and everything else allowed. Another architecture's
    syscalls kill the process; x32 ones are refused."""
    # Jumps name a label; they are resolved once the layout is known. Only "jump if true"
    # is used: when the test fails, the next instruction runs.
    program: list[tuple[int, str | None, int]] = []  # code, label to jump to, k
    labels: dict[str, int] = {}

    def emit(code: int, k: int, to: str | None = None) -> None:
        program.append((code, to, k))

    def label(name: str) -> None:
        labels[name] = len(program)

    emit(BPF_LD_W_ABS, OFFSET_ARCH)
    emit(BPF_JEQ_K, AUDIT_ARCH_X86_64, "arch_ok")
    emit(BPF_RET_K, SECCOMP_RET_KILL_PROCESS)
    label("arch_ok")
    emit(BPF_LD_W_ABS, OFFSET_NR)
    emit(BPF_JGE_K, X32_SYSCALL_BIT, "deny")
    for number in sorted(set(enosys)):
        emit(BPF_JEQ_K, number, "enosys")
    if clone_flags:
        emit(BPF_JEQ_K, SYS_CLONE, "clone")
    for number in sorted(set(denied)):
        emit(BPF_JEQ_K, number, "deny")
    emit(BPF_RET_K, SECCOMP_RET_ALLOW)
    if clone_flags:
        label("clone")
        emit(BPF_LD_W_ABS, OFFSET_ARG0_LOW)
        emit(BPF_JSET_K, clone_flags, "deny")
        emit(BPF_RET_K, SECCOMP_RET_ALLOW)
    label("deny")
    emit(BPF_RET_K, SECCOMP_RET_ERRNO | errno.EPERM)
    label("enosys")
    emit(BPF_RET_K, SECCOMP_RET_ERRNO | errno.ENOSYS)

    resolved: list[Instruction] = []
    for index, (code, to, k) in enumerate(program):
        jump = 0
        if to is not None:
            jump = labels[to] - (index + 1)
            if not 0 <= jump <= 255:
                raise ValueError("seccomp program too long for a short jump")
        resolved.append((code, jump, 0, k))
    return resolved


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


def install_filter(program: Sequence[Instruction]) -> None:
    """Set `no_new_privs` and add the filter to this process (and every program it execs).
    Raises OSError if the kernel refuses, so a child is never left unconfined."""
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    filters = (_SockFilter * len(program))(*(_SockFilter(*instruction) for instruction in program))
    prog = _SockFprog(len(program), filters)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP) failed")


def confine(limits: Limits) -> None:
    """Between fork and exec, as the sandbox user: limits, no new privileges, the filter."""
    apply_limits(limits)
    install_filter(
        build_filter(list(DENIED.values()), enosys=[SYS_CLONE3], clone_flags=CLONE_NAMESPACE_FLAGS)
    )


def forbid_exec() -> None:
    """From the child itself, before it opens the file: no other program can be started."""
    install_filter(build_filter(list(EXEC.values())))
