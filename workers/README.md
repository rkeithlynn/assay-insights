# workers

The ingestion pipeline (BUILD_SPEC.md sections 8 and 8a): package `assay_workers`.

Four Lambda handlers in one image (`workers/lambda.Dockerfile`) and the worker task
(`workers/worker.Dockerfile`), deployed by `infra/assay_infra/pipeline.py`.

| Handler | Does | Trigger |
| --- | --- | --- |
| `scan.handler` | Step 2: records a GuardDuty Malware Protection scan result for one upload, pinned to the scanned version; checks size and SHA-256 against the registration; validates a companion file; quarantines an infected file | EventBridge rule on `aws.guardduty` scan results for tenant buckets |
| `starter.handler` | Step 3: one Step Functions execution per eligible PDF, named after it; holds a suspended tenant's documents | The ingest queue (SQS event source) |
| `steps.handler` | Step 4 (`fingerprint`: hash, duplicates within the tenant, a PDF header check); `claim` and `release` (the worker task's slot and grant); and the failure handler | The pipeline state machine |
| `sweeper.handler` | Closes batches open 24 hours; times out scans that never arrived; queues again eligible documents never started | EventBridge Scheduler, every five minutes |

Rules every handler follows:

- It signs in to Aurora as `app_tenant` and works in one tenant at a time (`runtime.tenant_tx`).
  Before a tenant is known it may call only the two `pipeline` lookups (migration 0008).
- It reaches a tenant's bucket only with a TSS `PIPELINE` context, by key and version. There is
  no listing call anywhere (`tests/unit/test_no_listing.py`).
- The tenant ID comes from a pipeline job message or, for a scan result, from the database row
  of the registered upload whose key the event names (owner decision 3, build step 6).
- The isolation tests' reserved document ID (`assay_tss.keys.RESERVED_DOCUMENT_ID`) is refused
  before anything is read or written.
- A companion file's content is never logged.

- No Lambda opens a PDF. A Lambda has one user and its credentials in its environment, so
  the Lambda image ships no PDF library at all (`tests/unit/test_sandbox.py`).

## The worker task

`python -m assay_workers.worker`: one Fargate task per document's attempt, started by the
state machine (`ecs:runTask.sync`) after `claim`. It is the only part of the pipeline that
opens a PDF. Part 1 of the second half runs one step, `inspect` (does the PDF open, has it a
password, how many pages), then copies the PDF and its companion file to `originals/`. Steps
5 to 10 follow in the later stages.

It is bound to one document by things it cannot choose:

- **Storage:** `/resolve-worker` on the TSS, with the document ID, the attempt and the grant's
  token. The TSS takes the tenant from the grant's row and issues credentials for that one
  document's keys (`tss/README.md`).
- **Database:** it signs in as `app_worker` and calls `pipeline.bind_worker` with the same
  three values. Its policies show that one document while the grant is live, and nothing
  otherwise. It has no access to `users`, the chat tables or `audit_log`, and to no file name
  (migration 0010).

It runs as two processes and two users (`assay_workers.sandbox`):

| | Parent | Sandbox child |
| --- | --- | --- |
| User | root, with CAP_SETUID, CAP_SETGID and CAP_KILL only | `sandbox` (uid 10002), no capability |
| Holds | the task role's credentials, the tenant credentials, the database session | nothing: an empty environment |
| Opens a PDF | never | yes, one, read-only |
| Network | VPC endpoints, Aurora | none (a seccomp filter refuses `socket`) |
| Limits | the task's | 60 s wall clock, 45 s CPU, 2 GiB address space, no file writes, 16 processes, 64 KB of output |

The parent treats everything the child returns as untrusted: one JSON object, within the
size limit, that validates against a model with only numbers and codes from fixed sets. No
path or key ever comes from the child. If the child cannot be started as the sandbox user,
the job does not run.

Every way a child can end without a result has its own name, in the log line (`ending`) and
in the document's event (`error_type`, shown to master admins only):

| Ending | Name |
| --- | --- |
| The kernel stopped it at the CPU limit | `sandbox.cpu_limit` |
| The parent stopped it at the wall clock | `sandbox.wall_clock_timeout` |
| The address-space limit stopped it, and it said so | `sandbox.memory_limit` |
| It printed more than the parent accepts | `sandbox.output_limit` |
| It tried to write a file | `sandbox.file_size_limit` |
| Any other signal: a crash, or a kill from outside the sandbox | `sandbox.signal_SIGSEGV`, `sandbox.signal_SIGKILL`, ... |
| It exited with a failure, or printed no valid result | `sandbox.exit_<status>`, `sandbox.bad_output` |

The first five are the sandbox's limits doing their job; the rest are not, and the two kinds
have different log messages. In every case the tenant sees only `pdf_unreadable` ("The PDF
could not be opened"), and the line is logged at ERROR, which raises the worker's alarm.

What Fargate cannot give one process, and so the sandbox does not have: its own cgroup
(memory is limited by address space, with the task's 4 GB as the backstop), its own network,
process or mount namespace, or a task-level seccomp or AppArmor profile.

`WORKER_COMMAND=probe` runs the isolation probe instead (`assay_workers.worker.probe`,
started by `scripts/run_isolation.py`).
