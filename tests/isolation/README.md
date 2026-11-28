# tests/isolation

Cross-tenant isolation tests against the real AWS account (BUILD_SPEC.md section 6). Not
run in CI.

The TSS and the database are private, so the tests cannot run from a developer's machine.
They run with pytest in a Lambda inside the VPC (`assay-dev-isolation-tests`, deployed only
when stack config `isolationTests` is true). `scripts/run_isolation.py` starts it and
prints the result.

## Running them

The tests need two active business tenants. The development stack uses two permanent
fixture tenants, "Isolation Test A" and "Isolation Test B".

```powershell
uv run scripts/run_isolation.py --tenant-a <tenant id> --tenant-b <tenant id>
```

To save the IDs so the script needs no arguments:

```powershell
pulumi config set isolationTenantIds "<tenant id>,<tenant id>" --cwd infra --stack pilartech/assay-platform/dev
```

The script exits 1 unless every test passed. A skipped test counts as a failure.

The script runs three things and waits for each:

1. The isolation test Lambda, which runs the tests in this directory.
2. The provisioning task with its `probe` command (`tss/assay_tss/provision/probe.py`).
   This part has to run as the provisioning role, and only that task can be it.
3. The worker task with its `probe` command (`workers/assay_workers/worker/probe.py`, build
   step 6, second half). Only that task's role may call the TSS's `/resolve-worker` route or
   sign in to the database as `app_worker`, and only a Fargate task shows whether the PDF
   sandbox's confinement holds on Fargate. It checks that:
   - `/resolve` refuses the worker, and `/resolve-worker` refuses an ended, an expired, a
     wrong-attempt and a wrong-token grant;
   - a live grant's credentials read that document's original and are refused another
     document of the same tenant, the other tenant's document, and the tenant's
     `quarantine/` and `derived/` fixtures;
   - an unbound `app_worker` session sees no rows; a bound one sees its one document and may
     not read `users`, the chat tables, `audit_log` or any file name; a session whose grant
     ends while it is connected sees nothing from then on;
   - the sandbox child has no network, no credentials, no AWS or database library, and
     cannot read the parent's environment or memory; it is stopped at the timeout, the
     memory limit and the output limit;
   - the task's main process has exactly CAP_SETUID, CAP_SETGID and CAP_KILL, on a read-only
     root filesystem;
   - for the text step (stage 2): the document's own text object is read, and another
     document's or the other tenant's is refused, to read and to write; writing the stored
     companion file is refused; reading the document's companion upload in the inbox is
     refused, with S3 naming `s3:GetObject` as what the session policy does not allow (the
     grant's row lists that upload, and the TSS gives the worker the PDF's upload only;
     owner, 2026-11-27); a bound session writes its own attempt's `pages` and
     `ai_usage` rows and no other's (row security), can read neither table, sets the content
     pointer for its bound attempt only, and can update no other column of `documents`;
   - the real `text` and `render` jobs run in the sandbox on a synthetic PDF, and a child
     that misbehaves on the page channel is stopped or refused by name;
   - Textract `DetectDocumentText` reads the synthetic page the sandbox drew, with the task's
     own role through the VPC's endpoint; `AnalyzeDocument` and
     `StartDocumentTextDetection` are refused; Textract pointed at a tenant's bucket is
     refused; the document's storage credentials are refused Textract.

   The isolation Lambda also checks (`test_textract.py`) that the Textract endpoint refuses
   its own role, which holds the worker's Textract permission word for word: the endpoint's
   policy is the only thing left to refuse it.

   The isolation Lambda prepares its fixture first (`worker_fixture.py`): three documents
   with fixed IDs in the two test tenants, one small object at each one's `originals/` key
   and at its `derived/text/` key (written once, kept by Object Lock), and grants in every
   state. The probe's own document also has a companion upload: a clean `batch_files` row
   and one small object at its inbox key (written once). GuardDuty scans that object the
   first time; its row is committed before it is written and already has a result, so the
   scan function ignores the scan result at INFO and no alarm fires. The probe leaves a `pages` row, an `ai_usage` row (for its one Textract call, at the
   stack's price of a page: **every isolation run costs one Textract page**, and the row
   says so) and the content pointer on its own fixture document. It ends one grant when
   the probe says it is watching, and clears the grants afterwards. The fixture documents
   are `discarded` between runs, so they show in no history view by default, and each has a
   `start` event, so the pipeline never queues or processes them.

## What they show

Every cross-tenant test runs in both directions (A against B, and B against A).

| File | Shows |
| --- | --- |
| `test_storage.py` | A tenant's TSS-issued credentials work on its own bucket, key, vector index and face collection, and are refused on the other tenant's. `UPLOAD` cannot read and `READ` cannot write. Only `PIPELINE` reads `inbox/` and `quarantine/`, current or noncurrent, and no session lists a bucket (build step 6). Credentials last at most 15 minutes. A tenant session cannot start another session, cannot change key, bucket, vector bucket or collection tags, and cannot invoke the Cognito admin Lambda. |
| `test_storage.py` (runner probes) | The runner's own role, whose IAM policy *allows* it, still cannot assume the tenant role (trust policy), read, write or tag a tenant bucket (bucket policy), use or tag a tenant key (key policy), read tenant vectors, or invoke the Cognito admin Lambda. |
| `test_tss_api.py` | The TSS refuses an unsigned request, an unknown tenant, another service's name in the source identity, and any route but `resolve`. |
| `test_rows.py` | As `app_tenant` with one tenant set, the other tenant's rows cannot be read or inserted, `tenant_storage` cannot be read at all, and with no tenant set nothing is visible. |
| The provisioning role's probe | The provisioning role cannot read an object that exists, write an object, list object versions, read, query or write vectors, or use the tenant's key directly. It can list keys, which hold IDs only. It may use the key only through S3 Vectors, to create the vector bucket and index (an accepted exception; BUILD_SPEC.md section 6). Three settings calls it is allowed must succeed. |

The tests write only fixture objects, in a test tenant's own bucket, once, with that
tenant's pipeline credentials, all under the reserved document ID
(`assay_tss.keys.RESERVED_DOCUMENT_ID`, defined there and nowhere else):

| Key | Since | Shows |
| --- | --- | --- |
| `keys.isolation_fixture_text_key()`, `derived/text/{id}.json` | build step 4 | Cross-tenant refusals; a READ session reads it back |
| `keys.isolation_fixture_quarantine_key()`, `quarantine/{id}.pdf` | build step 6 | Only PIPELINE reads `quarantine/` |
| `keys.isolation_fixture_inbox_key()`, `inbox/{id}/{id}`, two versions | build step 6 (2026-11-24) | Only PIPELINE reads `inbox/`, current or noncurrent |

Object Lock keeps them, so later runs find them and write nothing. The current version of
the inbox fixture names the older version's ID in its body, because no tenant session may
list versions. The API and the pipeline refuse the reserved ID: no document is ever created
or processed with it. Every other write the tests attempt must be refused, and every
database transaction is rolled back. Tagging probes use a tag no policy looks at.

The fixture objects are there because a refused read proves something only for an object
that exists. S3 refuses a missing key too, to any caller that may not list the bucket, and
since build step 6 no tenant session may. Such a refusal names `s3:ListBucket`, not the read
(seen 2026-11-24). So every read that must be refused is tried on a fixture object. The
inbox test also requires S3's reason, `s3:GetObject` or `s3:GetObjectVersion`, and checks
that a missing key gets the `s3:ListBucket` reason instead.

The inbox fixture is the only one GuardDuty scans (each tenant's plan covers `inbox/` only).
Its two writes, on the first run in a tenant, each produce a scan result. The scan Lambda
ignores results for exactly that key at INFO, so they raise no alarm. Every other inbox key
that is not a registered upload still logs at ERROR and raises the alarm.

## Adding a test

- Put it in this directory; the runner image copies the whole directory.
- Use `assert_denied` for a refusal. It passes only on an access-denied error, so a missing
  object or an unreachable endpoint cannot look like a refusal.
- Pair every "refused" test with one that shows the same call working for the rightful
  tenant, so an environment where everything fails cannot pass.
- Build step 8 adds the cross-tenant face search (BUILD_SPEC.md section 8a).
