# workers

The ingestion pipeline (BUILD_SPEC.md sections 8 and 8a): package `assay_workers`.

Build step 6, first half: four Lambda handlers in one image (`workers/lambda.Dockerfile`),
deployed by `infra/assay_infra/pipeline.py`.

| Handler | Does | Trigger |
| --- | --- | --- |
| `scan.handler` | Step 2: records a GuardDuty Malware Protection scan result for one upload, pinned to the scanned version; checks size and SHA-256 against the registration; validates a companion file; quarantines an infected file | EventBridge rule on `aws.guardduty` scan results for tenant buckets |
| `starter.handler` | Step 3: one Step Functions execution per eligible PDF, named after it; holds a suspended tenant's documents | The ingest queue (SQS event source) |
| `steps.handler` | Step 4 (`fingerprint`: hash, duplicates within the tenant, encrypted PDFs, copy to `originals/`) and the state machine's failure handler | The pipeline state machine |
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

The second half of build step 6 adds steps 5 to 10 (the worker; owner decision 8 is pending).
