"""Where untrusted files are opened (build step 6, second half; owner condition B).

A PDF from a tenant is untrusted input to PDFium and, from stage 3, to pyHanko. Neither is
ever called in a process that holds anything worth stealing:

- `runner` (the parent side) is used by the worker task's main process, which holds the task
  role's credentials, the tenant credentials and the database connection, and never opens a
  PDF. It starts one child per job and treats everything the child returns as untrusted.
- `child` is that child: another, unprivileged user; an empty environment; no AWS client and
  no database driver; no network; limits on time, memory, files and processes; it reads one
  file and prints one small JSON object.
- `confine` is the confinement both apply: resource limits and a seccomp filter.
- `jobs` is the only code that opens an untrusted file.

This package's `__init__` imports nothing, so the child loads nothing it does not need.
"""
