"""The worker task: pipeline steps that need a PDF opened (build step 6, second half).

One Fargate task per document's attempt, started by the pipeline's state machine
(`ecs:runTask.sync`) with three values and nothing else: the document ID, the attempt and a
one-time token. It names no tenant, anywhere:

- **Storage.** It asks the Tenant Storage Service at `/resolve-worker` with those three
  values. The TSS takes the tenant from the attempt grant's database row and issues
  credentials for that one document's keys (`assay_tss.policies.worker_session_policy`).
- **Database.** It signs in as `app_worker` and presents the same three values to
  `pipeline.bind_worker`. Its row-level security policies read that binding: the session
  sees that one document and nothing else, and nothing at all once the grant has ended.
  There is no setting by which it could choose a tenant (migration 0010).

Two processes, two users (`assay_workers.sandbox`):

- the **parent** (this package) holds the credentials and the database connection and never
  opens a PDF;
- a **child** per job opens the PDF, as another user, with an empty environment, no
  network, and limits on time and memory. The parent treats what it returns as untrusted.

Part 1 runs one step, `inspect`: is the upload a PDF that opens, has it a password, how many
pages? Then it copies the PDF, and its companion file, from the inbox to `originals/`. Both
were the fingerprint Lambda's until it stopped opening PDFs. Steps 5 to 10 follow in the
later stages.

Like every other part of the pipeline it works from database rows only, never from a listing,
and refuses the isolation tests' reserved document ID.
"""
