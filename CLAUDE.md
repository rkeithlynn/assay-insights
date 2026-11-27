# CLAUDE.md

`BUILD_SPEC.md` is the contract for this build. Read the relevant sections before any
change. Where this file and the spec differ, the spec wins.

## Working rules

- **AWS:** profile `pilar-dev`, Region `us-east-1`.
- **Pulumi:** organization `pilartech`, project `assay-platform`, stack `dev`
  (`pilartech/assay-platform/dev`). The program lives in `infra/`. The organization moved
  to Pulumi Cloud's Free plan on 2026-11-27 (its trial had ended, and previews printed a
  notice saying so). Nothing in the code or the stack's name changed.
- **One build step at a time.** Work on exactly one step from BUILD_SPEC.md section 12.
  Stop when it is done and report back. Do not start the next step unasked.
- **One branch per build step.** Each step's work goes on its own branch, `build-step-N`,
  cut from an up-to-date `main`. Fixes for that step go on the same branch. Push it, wait
  for CI to pass, then the owner fast-forwards `main` to it. Never commit build work
  straight to `main`.
- **Never run `pulumi up` or `pulumi destroy`** (or `pulumi refresh`, `pulumi state ...`,
  or anything else that changes stack state or AWS resources). Run `pulumi preview`, show
  the owner the result, and wait for the owner to run `pulumi up`.
- **No identifiers in code.** Never put account IDs, email addresses, hostnames or model
  IDs in code. Read them from stack config (`infra/Pulumi.<stack>.yaml`) or the
  `ai_models` table.
- **When in doubt, stop and ask.** If the spec is unclear, or something in AWS doesn't
  match it, stop and ask the owner. Do not guess and do not work around it.
- Give the developer commands in PowerShell, one line each. Scripts the developer runs
  are Python (`uv run ...`), not shell scripts.

## Build progress (BUILD_SPEC.md section 12)

| Step | Status |
| --- | --- |
| 1. Repo scaffold, CI checks, Pulumi project, DNS zone and certificate | **Complete** (2026-11-23) |
| 2. Network, database, migrations, database roles and RLS | **Complete** (2026-11-23) |
| 3. Cognito pools, managed login branding, API skeleton with token verification, `GET /me` | **Complete** (2026-11-23) |
| 4. Tenant Storage Service, tenant provisioning, `tests/isolation` | **Complete** (2026-11-23) |
| 5. Portal shell, sign-in, Tenants and Users screens, `scripts/seed.py` | **Complete** (2026-11-24) |
| 6. Batches and upload, malware scan routing, pipeline steps 4 to 10, ingestion history | **First half complete** (2026-11-24). **Second half, stage 1 complete and verified in dev** (2026-11-27): the worker task, attempts and "Process again", step 9, companion attach and replace, the quarantine notice, "Mark as reviewed", refresh-token rotation on both portal clients. **Stage 2 (step 5, text, Textract) complete and verified in dev** (2026-11-27). Stages 3 and 4 not started. Next: stage 3, which needs its plan first. See "Start here" below |
| 7–9 | Not started |

## Start here: stage 2 of the second half of step 6 is deployed and verified; stage 3 needs its plan (state on 2026-11-27)

A new session starts from this section and BUILD_SPEC.md section 8. Everything below it is
the record: read the part a task touches. **Stage 2 (step 5, text) is built, deployed and
verified in dev.** Stage 3 is not started and needs its own plan first.

### Where things stand

- **Branch and pull request.** All work is on `build-step-6`, pushed, in pull request #5
  against `main`. `main` has not been fast-forwarded yet (the owner does that). Stage 3
  continues on `build-step-6`.
- **Deployed: stage 2, as stack update 41** (2026-11-27, commit 524a0aa), with Aurora at
  migration `0012` (owner).
- **Verified in dev by the owner (2026-11-27).**
  - The isolation suite passes: 73 in the isolation Lambda, 28 in the provisioning role's
    probe, 101 in the worker probe, 28 Cognito checks.
  - The Text line is right for the five synthetic files: 01 and 04 all from the text layer;
    08 three pages by OCR; 09 one from the text layer and two by OCR; 10 one by OCR.
  - The 1,000-page file was read in 7 minutes 48 seconds ("The 1,000-page run" below).
- **Built after that, not deployed.** One `pulumi up` carries both; neither has a
  migration.
  1. The upload screen's fixes ("Upload: files that went missing" below): the portal build.
  2. The scan role's object access limited to `inbox/` ("The scan role's reach" below): one
     role policy. **The owner tests it on Isolation Test A before anything else is
     uploaded.**
- **Tested (2026-11-27), with the upload fixes and the scan role's switch in.** The whole
  suite with the test PostgreSQL and the image tests required (`ASSAY_REQUIRE_DB_TESTS=1`,
  `ASSAY_REQUIRE_IMAGE_TESTS=1`): 1,796 passed, none failed, none skipped (1,792 at stage 2,
  and four new tests of the scan role's policy). The portal's type check, lint and 118 unit
  tests pass, and the rendered check passes, 58 of 58 (the 48 layout checks and the 10 new
  upload checks). **`pulumi preview` was not run for these changes:** the owner's AWS
  sign-in had expired for Pulumi, so run it, and read it, before `pulumi up`. What it should
  show is under "The scan role's reach".
- **Rule 6** (checked 2026-11-27, before stage 2 was built): the AI services opt-out policy
  in effect says `optOut` by default with no exception (last updated 2026-11-24 04:37:58
  UTC), and Bedrock has no invocation logging configuration in us-east-1. The check is the
  first part of `uv run scripts/check_deployment.py` in every mode;
  `uv run scripts/check_deployment.py --ai-services` runs it alone. **Run it again, and read
  it, before stage 3's deploy:** Bedrock is the next service to get tenant content.
- **What the pipeline does now.** Upload, scan, close, fingerprint (Lambda), claim, the
  worker task's `inspect` and `text` steps, release, `companion` (Lambda). Then the
  execution ends and the document stays `processing`: steps 6 to 8 and 10 do not exist yet,
  and the steps panel says so (`laterStepsPending`, keyed on the text step's result).
  **Textract is the first AI service that gets tenant content**, since 2026-11-27.

### Stage 2: what was built

Step 5 of BUILD_SPEC.md section 8, in the worker task, after `inspect`
(`workers/assay_workers/worker/text.py`):

1. **The text layer.** Sandbox children read it, 25 pages a child, and send one record a
   page down the **page channel**. The parent scores each page as it arrives
   (`worker/quality.py`).
2. **Textract.** Pages that fail the score are drawn by sandbox children as greyscale PNGs,
   8 pages a child, and the task's main process sends each to `DetectDocumentText`, as
   bytes, two calls at a time (`worker/ocr.py`). One `ai_usage` row per successful call,
   written at once: every call that succeeded is recorded before the step fails, also a
   page that succeeded after its neighbour had failed. Once a call has failed the step, no
   page that was not yet under way is sent.
3. **The text object.** `derived/text/{doc}.json`, uploaded from a file with fresh
   credentials for that one document. Its S3 version ID goes into the step's event and into
   `documents.text_version_id`.
4. **The rows.** One `pages` row a page for the attempt, and the content pointer
   (`documents.content_attempt`), in the transaction that writes the step's result.

**The page channel** (`sandbox/runner.py`, `sandbox/jobs.py`). A sandbox child's standard
output keeps its rule: one JSON object of numbers and codes, at most 64 KB. The two page
jobs (`text`, `render`) also get one inherited descriptor, the write end of a pipe, and write
one JSON record per line to it. The parent reads the pipe as the child writes and holds one
record at a time. The child still writes no file (the file-size limit stays 0), has no
network and no credentials, and the seccomp filter and the limits are unchanged. The parent
never opens a PDF or a picture: a picture is checked for its first eight bytes and its
length, and forwarded.

| Cap | Value |
| --- | --- |
| One page's text | 256 KB |
| One page record (text and word positions) | 1 MB |
| One page's picture | 5,000,000 bytes (AWS's pages say 5 MB in one place and 10 MB in another) |
| One picture record (the picture in base64) | 8 MB |
| One `text` child | 25 pages, so 25 MB at most |
| One `render` child | 8 pages, so 64 MB at most |
| The text object | 128 MB (`text_too_large` above it) |
| Textract calls in flight from one task | 2 |

**What an attacker who controls the PDF, and by exploit the child, can now get out:** text
of their choosing (256 KB a page), which is stored in that tenant's own text object, and
bytes of their choosing that begin as a PNG does (5 MB a page), which go unopened to
Textract and nowhere else. No credentials, no other document, no network, no path or key.

**The quality score** (`worker/quality.py`). 1.0 for a blank page; 0.0 for a page with
annotations that draw content (form fields, stamps, free text, ink) or with content and no
text; otherwise `legible x (1 - image coverage)`. `legible` is the share of ordinary
characters times the share of longer words that contain a vowel (English only; it needs 30
such words before it says anything). A page below the threshold goes to Textract.

**A page is `native` or `ocr`.** An `ocr` page's text is what Textract read on its picture,
the typed parts included. When such a page's text layer reads well by itself (it went to
Textract because of an image or annotations), the text layer's text is kept in the text
object too, as `text_layer`, with `is_source: false` and no word positions.

**What stops a document at this step:**

| Case | Status | Code |
| --- | --- | --- |
| More pages than `textMaxPages` | `failed` | `too_many_pages` (before any text is read) |
| A sandbox child did not end well | `failed` | `page_unreadable`, error type `sandbox.<ending>`; ERROR line, `worker-errors` |
| The child could not be started | the step fails | `text_failed`, `SandboxNotStarted` |
| Textract busy for 5 minutes on one page, or refusing the task | the step fails | `text_failed`, error type Textract's own (`ThrottlingException`, `AccessDeniedException`, ...) |
| The text object over 128 MB | `failed` | `text_too_large` |
| The task over 30 minutes | the step fails | `text_failed`, `States.Timeout` |

A page Textract will not take (`UnsupportedDocumentException`, `BadDocumentException`,
`DocumentTooLargeException`, `InvalidParameterException`) is recorded as unread and the
document goes on. The Text line of both step panels shows the count.

### The owner's decisions on stage 2 (2026-11-27)

1. **How page text leaves the sandbox:** the page channel, above.
2. **An `ai_usage` row for Textract:** `service` `textract`, `operation`
   `DetectDocumentText`, no `model_id`, Textract's own `model_version`, the `attempt` and
   `page_no`, tokens 0, `units` 1, `unit` `page`, `unit_price` and `cost` from stack config,
   and the request ID. One row per successful call.
3. **The threshold is 0.90** (`textQualityThreshold`), and the vowel factor stays.
4. **Permissions** (all six approved; "New grants and permissions" below).
5. **A document may have 1,000 pages** (`textMaxPages`). The spec had no page limit.
6. **A page with images over about a tenth of it, or with form fields or stamps, always goes
   to Textract**, even when its text layer is good: the text is then what a person sees.
   **Only a page's source text (`text`) feeds extraction, search and embedding.** The stored
   `text_layer` is there to compare with; nothing in stage 2 reads it.
7. **A document with unread pages goes on**, and stage 4 makes it `needs_review`. The unread
   count shows in the Text line of the step panel in the tenant view and the master view.
8. **The price of a page** is the owner's, in stack config (`textractPricePerPage`). No
   price is in code.
9. **The stored companion file left the worker's session policy**
   (`originals/{doc}.companion.json`): the companion step writes it, not the task.
10. **Every version of the text object is kept.** Object Lock's default (governance, 1 day)
    applies to `derived/text/` as to everything in the bucket, no role may delete a version,
    and there is no lifecycle rule. "Process again" writes a new version; the old one stays.
    Each attempt's version ID is in its `text` event (`detail.version_id`), so an old
    version can be found without listing. No lifecycle rule and no delete permission were
    added. (A discarded document never has a text object: only an open batch can be
    discarded.)
11. **The spend guard:** an alarm on Textract pages in 24 hours across the account
    (`textractDailyPagesAlarm`, 2,000), sent to the alarms topic.
12. **The Textract endpoint's policy admits the worker task's role only.** The plan's table
    said "plus the role in 3" (the isolation Lambda's); that would have defeated the test the
    role exists for, so it was built the narrower way: the isolation role holds the same IAM
    permission, with the same `aws:SourceVpce` condition, and the endpoint must refuse it.

### New grants and permissions (stage 2; also in the pull request)

- **IAM, worker task role:** `textract:DetectDocumentText` on `*` (Textract has no
  resource-level permissions), only with `aws:SourceVpce` equal to the Textract endpoint.
- **IAM, isolation Lambda's role:** the same statement, word for word, so that the endpoint's
  policy is the only thing that can refuse it.
- **Textract endpoint policy:** from the default (allow everything) to one statement:
  `textract:DetectDocumentText` for the worker task's role. Narrower than before. It is a
  `VpcEndpointPolicy` resource: never delete or recreate it (an endpoint without one allows
  everything).
- **Database (migration `0012`):**
  - `app_worker`: INSERT on 8 columns of `pages` and 15 of `ai_usage`, each with a policy
    that admits only its bound tenant, document and attempt. It can read neither table.
  - `app_worker`: UPDATE on `documents.content_attempt` and `documents.text_version_id`. A
    trigger (`worker_content_pointer_guard`) refuses the worker any value but its bound
    attempt, and an empty version.
  - `app_worker`: EXECUTE on the new `pipeline.bound_attempt()` (owned by `pipeline_lookup`).
  - `platform_reader`: four more integer columns of `platform.ingestion_events_v`, for a
    `text` event only: `pages`, `pages_native`, `pages_ocr`, `pages_unread`. Counts, shown
    only if the stored value is a plain whole number.
- **Narrowed:** the worker's session policy lost `originals/{doc}.companion.json`.
- **No change** to the tenant role, any bucket, key or vector bucket policy, any KMS key or
  grant, security groups, the set of endpoints, queues, topics, the state machine's role or
  any Lambda's role. No `update_tenants.py` run: no tenant stack resource changes.

### How stage 2 was deployed and checked (the owner, 2026-11-27)

Done in this order on 2026-11-27, and every check passed. Kept as the order for a later
stage.

Stack config, all four required (a stack without one fails `pulumi preview`):
`textractPricePerPage` (set by the owner: `0.0015`), `textQualityThreshold` (`0.90`),
`textMaxPages` (`1000`), `textractDailyPagesAlarm` (`2000`). All four are in
`infra/Pulumi.dev.yaml`.

1. `uv run scripts/check_deployment.py --ai-services` and read the output. Both must be ok.
2. Deploy when no document is in progress: `pulumi up --cwd infra --stack pilartech/assay-platform/dev`.
3. Straight away: `uv run scripts/migrate.py` (to `0012`). Until it is in, the text step
   fails, so upload nothing in between. Then `uv run scripts/migrate.py --command verify`.
4. `uv run scripts/run_isolation.py --tenant-a cfd08356-aac4-4dfe-a3f5-32a2f3294c4e --tenant-b 3f2d2180-2e99-4991-8cee-de1fd53bcbb5`.
   **If "textract: reads a synthetic page (the task's role)" is refused with
   `AccessDeniedException`, stop and tell the owner:** it would mean Textract does not
   honour the `aws:SourceVpce` condition, and removing it is the owner's decision.
   (2026-11-27: allowed in both runs. Textract honours the condition.)
5. `uv run scripts/make_test_files.py --fresh`, upload as Infinity Loans, and read each
   document's Text line: 01 and 04 all from the text layer; 08 three read by OCR; 09 one
   from the text layer and two by OCR; 10 one by OCR.
6. Compare the pages read by OCR with Textract's own count: CloudWatch, `AWS/Textract`,
   `SuccessfulRequestCount`, `Operation` `DetectDocumentText`. (`ai_usage` has no read path
   from outside the VPC.)
7. Check the alarms are `OK`, then `uv run scripts/check_deployment.py`.
8. The 1,000-page run: `uv run scripts/make_test_files.py --fresh --pages-1000`, upload
   `11-scanned-1000-pages.pdf` alone, and compare with the figures below. It sends 1,000
   pages to Textract, half the spend alarm's threshold.

### The 1,000-page run (measured 2026-11-27)

`11-scanned-1000-pages.pdf`, uploaded alone as Infinity Loans (owner). 1,000 pages read by
OCR, none unread. Read from AWS afterwards, read-only. The estimates made before the run are
kept beside each figure.

| What | Measured | The estimate |
| --- | --- | --- |
| The text step | **7 minutes 48 seconds** (the task's log: `inspected` 20:50:25.9 UTC, `text read` 20:58:13.7 UTC). About 2.1 pages a second; Textract answered 133 to 142 pages in each full minute | 12 to 17 minutes; the task's limit is 30 |
| The task's memory | **Peak 102 MB of 4,096 MB**. Container Insights, one sample a minute: 57, 96, 102, 102, 101, 96, 98, 97 MB | Near 0.5 GB; worst case near 2.3 GB |
| CPU | 280 to 330 of the task's 1,024 units (461 in the first minute) | - |
| Task storage in use | 0.3 GB | 40 MB of pictures, and the text |
| Textract calls | **1,000 successful** (`AWS/Textract` `SuccessfulRequestCount`, `Operation` `DetectDocumentText`; per minute 20, 133, 137, 136, 140, 138, 138, 142, 16). Response time 600 ms on average, 1,352 ms at most | 1,000 |
| Throttling | **None.** `ThrottledCount`, `ServerErrorCount` and `UserErrorCount` have no data point in the run's minutes | - |
| The spend alarm | `OK`. The day's total was 1,008 successful calls, accounted for below | Half the threshold |

- **The day's 1,008 successful calls, all accounted for** (2026-11-27 UTC, by the minute;
  CloudTrail names the caller of each):
  - 1,000 for this file (20:50 to 20:58 UTC).
  - 6 for the first batch's 08, 09 and 10 (3, 2 and 1 pages; 20:29 UTC).
  - 2 that no document accounts for: the worker probe's positive control, "textract: reads
    a synthetic page (the task's role)", once in each of the day's two isolation runs
    (20:14:25 UTC, task `77ae943c…`, and 20:25:58 UTC, task `26f0e421…`). Both came from the
    worker task's role through the Textract endpoint. The page is one the probe draws, never
    a tenant's. Each call wrote its `ai_usage` row for the probe's fixture document in
    Isolation Test A, at no price (the probe does not know the price), and AWS bills the
    page all the same. **Every isolation run costs one Textract page.**
  - The 8 user errors are the probes' deliberate refusals, four in each run.
- **Retries: none that reached Textract.** The task tries a call again only after throttling,
  a server error or no answer, and Textract counted no throttling and no server error. A
  call that Textract completed and the task then repeated would have been counted twice, and
  the count is exactly 1,000. **Not visible:** a connection that failed before it reached
  Textract. The task does not log a retry, so nothing else can show one.
- **`ai_usage` rows: not counted.** No read path reaches `ai_usage` from outside the VPC
  (the migration Lambda's `verify` reads no rows; `platform_reader` has no view over it).
  The evidence is indirect: 1,000 successful calls, one row written for each success, and a
  failed insert fails the step, which ended well. Counting them needs a new read path.
- **What the memory figure does not show.** One sample a minute, so a spike between two
  samples is not seen. And the synthetic scans are small one-bit pictures: a real scan's
  page picture is larger (up to the 5,000,000-byte cap), so real documents will use more.
  The caps still bound the worst case near 2.3 GB.
- Where to read these again: Container Insights' performance log group
  `/aws/ecs/containerinsights/assay-dev-pipeline-worker/performance` (filter on the task's
  ID), and CloudWatch's `AWS/Textract` metrics.

### Not verified, and how and when each gets settled

| What | Settled by | When |
| --- | --- | --- |
| `aws:SourceVpce` on the worker's Textract permission: whether Textract honours it | **Settled 2026-11-27: it does.** The worker probe's "textract: reads a synthetic page (the task's role)" was allowed in both isolation runs, and CloudTrail shows both calls coming through the Textract endpoint | Done |
| The words of the endpoint policy's refusal ("VPC endpoint policy" in the message) | **Settled 2026-11-27:** the isolation Lambda's suite passed (73 of 73, `test_textract.py` included), so the message is as the test requires | Done |
| Textract's answer when it is pointed at an object the caller cannot read | **Settled 2026-11-27: `InvalidS3ObjectException`**, for the task's own document and for another tenant's alike (the worker probe's log, both runs) | Done |
| Textract's size limit for one picture (5 MB or 10 MB) | Not needed: pictures stay under 5,000,000 bytes | - |
| PDFium: greyscale drawing without an image library, form fields, image coverage | **Settled 2026-11-27:** `tests/unit/test_worker_image.py`, with the real child in the task's image (the four synthetic files) | Done |
| That Textract reads the dot-matrix letters of the synthetic scans well | **In part (2026-11-27):** the owner's check of 08, 09 and 10 shows every scanned page read by OCR and none unread. How well is not known: `ocr_confidence` is in `pages`, which has no read path yet | With the first screen that shows a page's text |
| 1,000 scanned pages inside 30 minutes, and the memory figures | **Settled 2026-11-27:** 7 minutes 48 seconds, peak memory 102 MB ("The 1,000-page run" above) | Done |
| How soon the spend alarm fires | **Settled 2026-11-27** from AWS's alarm evaluation page: with no evaluation window set, an alarm slides. It is checked every minute against the 24 hours before it, so it fires within minutes of the 2,001st page, and stays in ALARM until those 24 hours hold 2,000 or fewer. It notifies on the change of state only. Not seen firing | Read |

### Upload: files that went missing on 2026-11-27, and what the screen does now

**What happened** (owner, as Infinity Loans, four additions between 15:28 and 15:42
Eastern). Twice, fewer files appeared in the batch than were selected, with nothing on the
screen to say so: six selected and three listed, then three selected and two listed.

**What the server saw** (the load balancer's access log and the API's own log, read-only).
One registration request per batch, each answered 201, none failed and none repeated. By
their sizes (about 123 bytes a file in the request, about 2,075 in the answer) the two short
batches registered three files and two. So the missing files were never sent to the API:
never registered, never given an upload link, never uploaded. Request bodies are not logged,
so no file name can be seen, and nothing shows what the browser was handed.

**Verdict: unproven, and the evidence points away from the portal.**

- No path in the code registers fewer files than the page is handed without saying so.
- A rendered test adds the same six files 60 times at each of two widths (chosen 40 times,
  dropped 20), in a different order each time, against a stand-in API that answers after
  varying delays. All six were registered and listed every time, on that day's code too.
- What no test and no log can show is what the Windows file picker handed the page. In both
  short batches the missing files are the first in name order, which is what a click
  without Ctrl partway through a selection leaves. That fits. It is not proof.

**One real defect was found on the way. It was not the cause** (no registration was refused
that day). A refused action's reason vanished: the screen read the batch again after a
refused registration or close, and that reload cleared the error. "The batch already has a
file with that name." showed for a moment and then nothing, with the files not added. The
first reading of the code missed it; the test written for it fails on the old code.

**What the screen does now** (owner's decisions, 2026-11-27; `portal/src/pages/Upload.tsx`,
`portal/src/upload.ts`):

1. **The line.** Every addition says how many files the page was handed and how many are
   now in the batch: "6 files chosen: 6 added." or "6 files chosen: 4 added, 2 not added."
   The number chosen shows at once ("6 files chosen: adding them."), before the API has
   answered. The line stays until the next addition or until it is dismissed. A short
   selection is seen the moment it happens.
2. **"Not added" keeps every entry until it is dismissed.** A later addition adds to the
   list and never clears it.
3. **No picker while an addition runs.** The file input has no disabled state in this
   Cloudscape version, so a disabled button stands in its place. A drop made then is not
   queued and not ignored: each of its files is listed under "Not added" ("Other files were
   still being added. Add it again.").
4. **Each file goes to the link registered under its own name**, never by its position in
   the answer (`matchRegistered`). The API stores a name without surrounding spaces, so
   names are compared that way. A file the answer left out is listed under "Not added".
5. **No silent return.** Adding files with no batch loaded shows an error.
6. **A refused action's reason stays** until it is dismissed or the next action succeeds.
   Reading the batch again clears only an error that reading it raised.

**The tests** (`portal/e2e/upload.e2e.ts`, part of the `portal-render` job, about a minute
more): the additions above, the line, "Not added", an addition made while another runs, and
a refused registration. Each fix was shown to be guarded: the tests were run against a copy
of the portal with that one fix undone, inside the container only, and failed there.
`portal/src/upload.test.ts` covers the two helpers.

**Lessons.**

- A reload after an action must not erase what the action reported. Look for "set the
  error, then read again" where the read clears errors.
- Playwright's `getByText` matches a substring and ignores case: "Not added" also matched
  "1 not added." Give an element a test ID when its words can appear elsewhere.
- The server's logs can show how many files a request registered, by its size, and never
  which files.

### The scan role's reach: `inbox/` only, behind a switch (built 2026-11-27, not deployed)

The owner's decision (2026-11-27), as its own change after stage 2.

- **What changes.** Two statements of the GuardDuty scan role's IAM policy:
  `AllowMalwareScan` (read an object and its versions) and `AllowPostScanTag` (read and
  write an object's tags). They go from every object of every tenant bucket to the objects
  under `inbox/`, the one prefix each tenant's protection plan scans
  (`provisioning.scan_role_policy`, `inbox_only`). Before, the role could read originals
  and text objects, and decrypt them through S3. Only GuardDuty can assume it.
- **What does not change.** `s3:ListBucket`, the validation object's write, the
  notification and managed-rule statements, and the key statement (decrypt through S3 on a
  tenant key, which reaches only what S3 lets the role read). No tenant bucket policy
  changes: every statement there is a Deny, so the role's own policy does the allowing and
  narrowing it is enough. No tenant stack run, no migration, no image.
- **The switch.** Stack config `malwareScanInboxOnly`, `"true"` in dev. `"false"`, or the
  key left out, is the policy as it was. Either way it is one `pulumi up` that updates one
  role policy. Stack outputs: `malware_scan_role_name`, `malware_scan_inbox_only`.
- **One policy for every tenant.** The role is shared, so the switch narrows every tenant's
  bucket at once. "Isolation Test A first" is the order of testing: nothing is uploaded to
  another tenant until a clean file and EICAR have gone through Isolation Test A.
- **Checked before the deploy** (IAM's policy simulator, read-only, 2026-11-27, one object
  key at a time):

  | Request | Switch on | Switch off |
  | --- | --- | --- |
  | The six read and tag actions on an `inbox/` key | allowed | allowed |
  | The same on `originals/`, `derived/text/` and `quarantine/` keys | `implicitDeny` | allowed |
  | The same on the validation object | `implicitDeny` | allowed |
  | `s3:PutObject` on the validation object | allowed | allowed |
  | `s3:ListBucket` and the two notification actions on the bucket | allowed | allowed |

- **Not known, and the risk.** AWS's page for the role says that with an object prefix its
  access should be limited to the targeted prefixes, and shows no such policy. Two things
  are not known:
  1. That GuardDuty reads and tags nothing outside the prefix when it scans.
  2. Whether GuardDuty reads or tags its validation object
     (`malware-protection-resource-validation-object`, at the bucket's root) after writing
     it. With the switch on the role can still write that object, and can no longer read or
     tag it: the owner's instruction was to change the two statements and nothing else.
  Either would show as uploads that are not scanned, or as a plan that leaves `ACTIVE`.
  Nothing is lost silently, and `"false"` undoes it.
- **The baseline** (read 2026-11-27, before the change): all four tenants' plans are
  `ACTIVE`, with the prefix `inbox/` and no status reason.

**The test, in this order** (owner; PowerShell, one line each):

1. `pulumi preview --cwd infra --stack pilartech/assay-platform/dev --diff`, and read it. It
   was not run when this was built (the AWS sign-in had expired for Pulumi). **What it
   should show, from the code, not from a run:** one role policy updated
   (`assay-dev-malware-scan-policy`, the two statements' `Resource`), the portal build
   replaced with what depends on it (the upload screen's fixes), two new stack outputs, and
   no image, role, bucket policy, endpoint or database change. Anything else: stop and ask.
   Then deploy when nobody is uploading: `pulumi up --cwd infra --stack pilartech/assay-platform/dev`.
2. Read the deployed policy. Both statements must end in `/inbox/*`:
   `aws iam get-role-policy --role-name assay-dev-malware-scan-role-cf2a8e2 --policy-name assay-dev-malware-scan-policy-4c8a356 --profile pilar-dev --query "PolicyDocument.Statement[?Sid=='AllowMalwareScan' || Sid=='AllowPostScanTag'].[Sid,Resource]" --output text`
   (the deployed names in dev, read on 2026-11-27; the role's name is also the stack output
   `malware_scan_role_name`).
3. **A clean file, Isolation Test A.** `uv run scripts/make_test_files.py --fresh`, sign in
   to the tenant portal as Isolation Test A's admin, open a batch and add `01-normal.pdf`.
   It must show "Scanned, clean", normally within a minute or two. Close the batch: the
   document goes to Processing and its Text step passes.
4. **EICAR, Isolation Test A**, in AWS CloudShell:
   `python3 ingest_check.py eicar --host dev.assayinsights.ai --tenant cfd08356-aac4-4dfe-a3f5-32a2f3294c4e`.
   The file must end `infected` and the document `quarantined`, and the security notice
   must arrive.
5. Every plan still `ACTIVE`, with no status reason:
   `(aws guardduty list-malware-protection-plans --profile pilar-dev --region us-east-1 --query "MalwareProtectionPlans[].MalwareProtectionPlanId" --output text) -split "\s+" | ForEach-Object { aws guardduty get-malware-protection-plan --malware-protection-plan-id $_ --profile pilar-dev --region us-east-1 --query "[ProtectedResource.S3Bucket.BucketName,Status,StatusReasons]" --output json }`
   Run it again the next day: it is not known when GuardDuty next checks a plan's role.
6. `uv run scripts/run_isolation.py --tenant-a cfd08356-aac4-4dfe-a3f5-32a2f3294c4e --tenant-b 3f2d2180-2e99-4991-8cee-de1fd53bcbb5`,
   then `uv run scripts/check_deployment.py`.
7. Only then upload as another tenant.

**What a failure looks like.**

| What is seen | What it means |
| --- | --- |
| The file shows "On hold: could not be scanned", with "The malware scan could not read the stored file." (`scan_access_denied`), within a minute or two | GuardDuty's read was refused: it needs more than `inbox/` |
| The file stays "Waiting for its scan"; after 30 minutes it is on hold (`scan_timeout`) and `assay-dev-pipeline-sweeper-errors` fires | No scan result came at all |
| "The malware scan failed." (`scan_failed`) | GuardDuty gave up on the object; read the plan's status reasons |
| The EICAR file shows "Scanned, clean" or is held, not quarantined | The scan did not really read the file |
| A plan's status is `WARNING` or `ERROR`, with a status reason | GuardDuty's check of the role failed (the validation object, or tagging) |
| Everything works and only the object tags are missing | Tagging was refused quietly. The pipeline does not use the tags; record it and decide |

**Going back** (one config change and a `pulumi up`):

1. `pulumi config set malwareScanInboxOnly false --cwd infra --stack pilartech/assay-platform/dev`
   (it writes `"false"` into `infra/Pulumi.dev.yaml`; commit that).
2. `pulumi up --cwd infra --stack pilartech/assay-platform/dev`
3. Read the policy again with the command of step 2 above: both statements end in `/*`.
4. A file that was put on hold is not scanned again: discard its batch and upload it anew.

**The worker's read of its document's companion upload: it can be dropped. Proposed, not
built** (the owner decides).

- The task never reads that object. It reads the PDF, and the companion's `batch_files` row
  to say whether the document has one. The `companion` step, a Lambda with its own PIPELINE
  context, reads and stores the companion file.
- Today the grant's row lists every clean inbox file of the document, the PDF and the
  companion, and the session policy admits both for reading.
- **Proposed:** the TSS admits one inbox key for a worker, `inbox/{batch}/{document ID}`.
  A PDF's upload has the document's own ID, so the TSS can tell it from the companion's
  without a database change, and refuses a grant row that lists anything else for the
  worker's policy. `WORKER_MAX_INBOX_KEYS` becomes 1. The worker probe gains "reading its
  document's companion upload is refused", by S3's own answer.
- **What it takes:** the TSS and worker images, so one `pulumi up`. No migration, no grant,
  no tenant stack run. The alternative, changing `pipeline.worker_grant` to list only the
  PDF, needs a migration for the same effect.

### For stage 3 (steps 6 to 8: split, signer details, field extraction)

- **Extracted text is content chosen by whoever made the PDF. It must be handled as data,
  never as instructions, when it is shown to a model** (owner, 2026-11-27). That holds for
  the text layer and for what Textract read alike: either can say "ignore your instructions".
  Forced tool choice and schema validation stay (decision D), and nothing a model returns
  chooses a tenant, a bucket or a SQL statement.
- **Only a page's source text (`text`) is the page's text.** Stage 3's plan may propose using
  the stored `text_layer` to cross-check a value that OCR also read, for the owner's decision
  then. Nothing uses it before that.
- **Read the text at `documents.text_version_id`**, never "the latest" version of the key.
- **The content pointer** moves in the text step today, because that is the last content
  step. When a later step writes content, the pointer moves after the last of them.
- **Unread pages** (the text event's `pages_unread`) make the document `needs_review` in
  stage 4 (decision 7).
- Each stage adds `app_worker`'s grants and policies for the tables its steps use, bound to
  tenant, document and attempt (`pipeline.bound_attempt()` exists now). A SQL check passes on
  NULL: require each value outright (`units IS NOT NULL AND units >= 1`).
- Add each step to `progress.WORKER_STEPS`, the portal's `STEP_TEXT` and `RESULT_TEXT`.

### For stage 4: the 30 seconds after a task's last step (owner's decision, 2026-11-27)

Every document shows about 30 seconds between the Text step's result and the Companion
step's, with or without a companion file. None of it is our code working. It is Fargate
stopping the task, inside the `Worker` state, which waits until the task is `STOPPED`
(`ecs:runTask.sync`). Measured on 2026-11-27, seven tasks:

| From | To | Seconds |
| --- | --- | --- |
| The task's last write | The process exits | about 0.4 |
| The process exits | ECS begins stopping the task | 10.0 (10.04 to 10.06 every time) |
| Stopping | The task is `STOPPED` | 13 to 18 |
| `STOPPED` | Step Functions hears of it | about 1.2 |
| `release` | Done | 0.13 warm, 3.2 after nine idle minutes |
| `companion` | Done | 0.3 to 0.4 |

- **The plan for stage 4:** the final status is set inside the task, at its last write, and
  the companion step runs before the task or beside it. The document then shows finished
  when the work is finished, and the teardown happens out of sight. No new permission.
- **Rejected by the owner:** a task-token callback (`.waitForTaskToken`). It would give the
  worker task's role, the least trusted one, a Step Functions permission.
- **Only if batch throughput ever matters:** a state machine that polls for the task's
  result and releases the tenant's worker slot before the teardown ends.
- **Change nothing before stage 4.**

### What a new session would otherwise rediscover

Each is recorded in full further down; this is the index.

| What | In one line | Where |
| --- | --- | --- |
| Cognito's refusals with rotation on | The old refresh flow answers `UnsupportedOperationException`, for a valid token and an invalid one alike; a rotated-out token answers `RefreshTokenReuseException` and revokes nothing else. Never give a client an empty flow list; no pool may have a custom-auth trigger | "Stage 1, part 2", "What Cognito answers on a client with rotation on" |
| Lambda concurrency | A new account allows 10 concurrent executions. Development has 1,000, with 50 reserved for the scan function (a cap) and 300 for the TSS (a guarantee). Never cap a function whose caller is synchronous | "One scan result failed once" |
| The TSS API's access log | The API Gateway logging role must admit a call with no source account (`StringEqualsIfExists`). Never change it to `StringEquals`: the log goes silent with no error | "The TSS API's access log received nothing" |
| IAM propagation | A resource policy that names a role created in the same run as a principal can be refused for some seconds. Such a policy depends on `iam_propagation` (a 30-second wait) | "Policies that name a new role" |
| Line endings | A file Git marks as LF must be LF on disk; images are built from the bytes on disk. `uv run scripts/check_line_endings.py --fix`. Scripted edits pass `newline="\n"` | "Line endings: files Git marks as LF" |
| Image builds | "`<repository>@sha256:...`: not found" was the build's own check, not a missing image. Images are confirmed through ECR's API; dependencies and code are two layers | "not found" under "Step 2" |
| Checks by name | A check that expects "refused" in general hides what was refused. Give each mechanism a case only it can end, and require the service's own answer | "First isolation run after the deploy", the rotation notes |
| Write a resource as AWS stores it | Otherwise every preview shows a difference (ECS container definitions, API policies, bucket encryption) | Steps 3 and 4 |
| Deployed names | They are not Pulumi resource names (`assay-dev-pipeline-scan`, not `...-fn`). List before writing a command | "Deployed names are not Pulumi resource names" |
| Reading the database | Aurora is private. `uv run scripts/migrate.py --command verify` is the read-only way in; Audit history is the way to read `audit_log` | Step 2, "Reading audit_log" |
| AWS sign-in | The owner's SSO session expires during a long session; `pulumi preview` then fails. The owner runs `aws sso login --profile pilar-dev` | - |
| The page channel | A sandbox child's standard output is still one small object of numbers. Page text and pictures leave by a pipe the parent reads one record at a time; the child writes no file | "Stage 2: what was built" |
| A SQL check passes on NULL | `units >= 1` admits a row with no units. Require the value outright; a database test as the role caught it | Stage 2, migration `0012` |
| `SET col = col` proves nothing about UPDATE | It is refused for the missing SELECT alone. The probe sets a column to its default and rolls back | The worker probe, stage 2 |
| The spend alarm slides | An alarm with a one-day period is checked every minute against the last 24 hours, not at midnight | "Not verified, and how and when" |
| Shell edits with backslashes | A bash heredoc halves them and can hang on `python`. Write the script with the Write tool, then run it with `uv run python` | - |
| One test session at a time | Each pytest session resets the test PostgreSQL when it starts (it drops every `assay_test_*` database and the roles). A second session started meanwhile destroys the first one's databases: 62 failures and 31 errors, all "does not exist", on 2026-11-27 | `tests/unit/pgtest.py` |
| Two calls in flight | Answers from two threads do not arrive in page order. A test that assumes they do passes on one machine and fails on another (CI found it); and a page that succeeded after its neighbour failed was still billed | `worker/text.py`, `_send_to_textract` |
| A reload that erases an error | The Upload screen read the batch again after every refused action, and the reload cleared the error: a refused registration showed its reason for a moment, then nothing | "Upload: files that went missing" |
| What the file picker hands the page | Neither the server nor a test can see it. The screen now says how many files it was handed and how many it added | "Upload: files that went missing" |
| The 30 seconds after the Text step | Fargate stopping the task, not our code. Stage 4 moves the final status into the task | "For stage 4" |
| The policy simulator, several resources | With more than one `--resource-arns` it gives one decision for all of them together. Simulate each resource alone | "The scan role's reach" |
| One Textract page per isolation run | The worker probe's positive control is a real, billed call on a synthetic page, with its own `ai_usage` row | "The 1,000-page run" |

### Open items and known gaps

Decisions the owner has still to make, or things to do:

- **`main`**: fast-forward to `build-step-6` (pull request #5, CI green).
- **Deploy the upload fixes** (the next `pulumi up`; the portal build), and look at the
  line after an addition in a browser.
- **When the usage and cost view is built: verify that `11-scanned-1000-pages.pdf`
  (Infinity Loans, 2026-11-27) shows exactly 1,000 Textract rows** (owner, 2026-11-27). No
  read path reaches `ai_usage` today, and none is to be added for this. The evidence so far
  is indirect: 1,000 successful calls, one row written for each, and a step that ended well.
- **Right-size the worker task only after real scanned documents have been measured**
  (owner, 2026-11-27). It peaked at 102 MB of 4,096 MB on synthetic one-bit scans, which
  says little about real ones.
- **Stage 4: the final status inside the task, the companion step before or beside it**
  (owner, 2026-11-27; "For stage 4" above). Nothing changes before then.
- **Deploy and test the scan role's limit to `inbox/`** (built 2026-11-27 behind
  `malwareScanInboxOnly`, on in dev; "The scan role's reach" above has the steps, what a
  failure looks like and the way back). Try it on Isolation Test A first, with a clean file
  and EICAR.
- **The worker's read of its document's companion upload in the inbox: proposed, not
  built** ("The scan role's reach" above). The task reads only the PDF.
- **Concurrency numbers**: 50 and 300 rest on a small sample (43 scan runs, 9 at once at
  most). Measure again once real batches have run.
- **The 74 files Git checks out with CRLF** on the owner's machine (the portal's `.ts` and
  `.tsx`, `.json`, `.svg`; `text=auto` under `core.autocrlf=true`). Making them LF would
  change the bytes of the SVGs Cognito's branding is built from, so it is a change to
  deploy on purpose.
- **Dependencies**: ESLint 10.15.0 and `typescript-eslint` 8.74.0 after 2026-12-07; the
  `fastapi`, `pydantic`, `aws-lambda-powertools` and `pypdfium2` caps, deliberately.
- **Before production**: VPC flow logs to the Log Archive account; an approval-gated
  break-glass role; exercise the break-glass rule, log and alarm once; a Cognito signing-key
  rotation needs a `pulumi up`. **Before Staging or Production**: raise the Lambda quota
  first, then set the two reservations.
- **Before production: there is no designed way to delete a tenant's data** (owner,
  2026-11-27): offboarding, a retention period, a deletion request. No role may delete an
  object version, there is no lifecycle rule, and every version of every object is kept
  (inbox uploads, originals, text objects, noncurrent versions). It needs its own design
  covering originals, derived objects and noncurrent versions together. **Not designed.**

Known gaps: things not proven, or proven only in part:

- **The image build's "not found" failure.** The cause is inferred, not seen: CloudTrail
  shows the check's request never reached ECR, and the registry client's source shows how
  a failed request reads as "not found", but the failed request's own error was never kept.
  The fix is deployed. On 2026-11-27 eight images were pushed together (update 39) and each
  was confirmed by the first call to ECR's API, within a second. But that day their layers
  uploaded in seconds, not minutes, so the condition behind both failures did not recur and
  **the retry path has never run in AWS**. If it happens again the message now says whether
  the request failed or ECR had no image; record which.
- **`/oauth2/token`'s answer for a rotated-out refresh token** is not documented by AWS and
  was not captured. The portal's tests assume `invalid_grant`. In a browser the stale tab
  did show the session-ended message (owner, 2026-11-27).
- **The worker's own password check has never been reached by an upload.** GuardDuty held
  both protected PDFs tried (40-bit and 128-bit RC4) at the scan. AES-encrypted PDFs were
  not tried. The probe runs the check on Fargate.
- **The IAM propagation wait** has not been shown to work: only a run that creates a role
  and such a policy together can show it, and the next one is a new stack.
- **A RunTask refused for the vCPU quota**: how it reaches Step Functions is not known. The
  design does not depend on it.
- **The scan event's `versionId`**: not verified directly that it names the version scanned.
- **Platform-pool MFA enrolment** is not recorded, and **failed sign-ins** are not audited
  (both owner decisions; "Known limits").
- **Cognito's `amr` claim**: whether it shows MFA has still not been checked.
- **The worker's session policy** still names the companion key for writing; the task no
  longer writes it.
- **The break-glass log** is empty because the role has never been assumed.

Step 1 was verified on 2026-11-23:

- The hosted zone `dev.assayinsights.ai` is delegated from Squarespace to its four
  Route 53 name servers (`pulumi stack output name_servers`).
- The ACM certificate for `dev.assayinsights.ai` and `admin.dev.assayinsights.ai` is
  `ISSUED`. It replaced the first certificate, which failed with `CAA_ERROR`.
- `pulumi preview` on the `dev` stack shows 6 unchanged.
- CI passes on `main`.

Step 2 was verified on 2026-11-23:

- `uv run scripts/migrate.py` created all 7 database roles as the Aurora master user, with
  `roles_verified: true`, and migrated to revision `0001`.
- `uv run scripts/migrate.py --command verify`, run in Aurora as `migrator`, reported
  `rls_verified: true` for all 23 tenant tables (`tenant_id`, RLS enabled and forced, a
  policy on each) and `roles_verified: true` (no role has SUPERUSER, BYPASSRLS,
  REPLICATION, CREATEDB, CREATEROLE or an unexpected membership).
- `pulumi preview` on the `dev` stack shows 65 unchanged.
- Fixes found during deployment, all on `build-step-2`: the ECR login kept out of the
  Windows Credential Manager, the RDS CA bundle made readable by Lambda's non-root user,
  and a bootstrap that works under Aurora's non-superuser master user.
- CI on the `build-step-2` pull request must pass before `main` is fast-forwarded.

Step 3 was verified on 2026-11-23:

- Both user pools exist on the Essentials plan (tenant `us-east-1_qPZSlzN6i`, MFA optional;
  platform `us-east-1_lXRGqH8C7`, MFA on), each with its groups, app client and managed
  login v2 domain (`assay-dev-tenant`, `assay-dev-admin`, both `ACTIVE`). Each client
  has its branding (form logo and SVG favicon); the owner checks the look in a browser.
- The API runs on Fargate as task definition revision 2 (image `src-26064c7b61b2a28e`),
  rollout `COMPLETED`, and its target behind the internal ALB is `healthy`.
- `uv run scripts/migrate.py --command verify` reported revision `0002`,
  `rls_verified: true` and `roles_verified: true`.
- `pulumi preview` on the `dev` stack shows 111 unchanged.
- Fixes found during deployment, all on `build-step-3`: no custom invitation template
  (the provider sent an empty SMS message), a 3.71:1 login logo with the branding checked
  before deploy, and the task's container definition written as ECS stores it, with an
  empty JWKS refused.
- Not yet tested in AWS: `GET /me` end to end, which needs CloudFront and seeded users
  (step 5), and whether a real access token's `amr` shows MFA.
- CI on the `build-step-3` pull request must pass before `main` is fast-forwarded.

Step 4 was verified on 2026-11-23:

- The TSS runs as a Lambda in the VPC behind a private REST API with IAM authorization.
  The tenant role trusts only the TSS's role. The break-glass role, its rule and alarm,
  and the alarm topic (subscription confirmed) are in place.
- `uv run scripts/migrate.py` moved Aurora to revision `0003` with `rls_verified: true` and
  `roles_verified: true`.
- Both fixture tenants were provisioned through the state machine and are `active`:
  "Isolation Test A" is `cfd08356-aac4-4dfe-a3f5-32a2f3294c4e` and "Isolation Test B" is
  `3f2d2180-2e99-4991-8cee-de1fd53bcbb5`. Each has its key, bucket, vector bucket and
  index, malware protection plan and face collection.
- `uv run scripts/run_isolation.py` passed for those two tenants (reported by the owner):
  62 of 62 tests in the isolation Lambda, and 28 of 28 checks in the provisioning role's
  probe. Run it with
  `--tenant-a cfd08356-aac4-4dfe-a3f5-32a2f3294c4e --tenant-b 3f2d2180-2e99-4991-8cee-de1fd53bcbb5`;
  stack config `isolationTenantIds` is not set.
- `pulumi preview` on the `dev` stack shows 200 unchanged.
- Fixes found during deployment, all on `build-step-4`:
  - the TSS API's policy and the bucket encryption rule written as AWS stores them;
  - the provisioning role allowed to tag a tenant key at creation, with `tenant_id`
    write-once;
  - `kms:Decrypt` through S3 Vectors for the provisioning role, to create a vector index
    (an accepted exception; BUILD_SPEC.md section 6);
  - the tenant role's trust policy split into one statement per STS action;
  - the provisioning role's probe reading an object that exists.
  The notes under "Step 4" below explain each.
- Carried forward: the AI services opt-out policy (applied and verified, see "Rule 6"
  under "Tenant isolation"); step 6 must follow the two "For step 6" notes below (no file
  names in keys, and the reserved fixture document ID).
- CI on the `build-step-4` pull request (#3) must pass before `main` is fast-forwarded.

Step 5, first half, was verified on 2026-11-23:

- `pulumi up` finished after two fixes on `build-step-5`: a security group rule description
  with an apostrophe (EC2 refuses it at create time; `test_infra_program.py` now checks every
  description in the program) and the portal origin's empty `s3_origin_config` (AWS does not
  store it, so every preview showed a change). `pulumi preview` then showed 278 unchanged.
- `scripts/seed.py` ran clean. `run_isolation.py` passed: 65 tests in the isolation Lambda,
  28 checks in the provisioning role's probe, 7 Cognito client checks (owner).
- In a browser (owner): the master admin's first sign-in with a new password and MFA
  enrolment; the Tenants screen with five tenants; an Infinity Loans admin's first sign-in,
  the portal's MFA setup and a second sign-in; the Users screen; the master admin's reset of
  that admin's MFA, after which their session stopped and the next sign-in asked for MFA
  setup; sign-out of both.
- Read-only checks (2026-11-24 UTC): the auth-event queue and dead-letter queue are empty and
  nothing was ever sent to the dead-letter queue; the four auth-event alarms are `OK`. The
  trigger logged 7 `sign_in` events queued and the consumer 7 recorded, with no ERROR line and
  no "not queued" line. The load balancer log shows `POST /api/auth/signed-out` 204 four
  times (and once 403), `POST /api/admin/users/{id}/reset-mfa` 204 once, and `GET /api/users`
  403 twice (the revoked session). Access logs arrive: 7 CloudFront and 7 load balancer log
  objects; 95 WAF log events, all ALLOW.
- **Not read directly:** the `audit_log` rows and the two users' `last_sign_in_at` and
  `status`. No existing read path can (see "Reading audit_log" below). The evidence is
  indirect: `auth_events.record()` returns `recorded` only after it has written the audit row
  and updated the user, and the API writes each change and its audit row in the transaction
  it commits before answering 204. (Since read directly: Audit history, second half.)

Step 5 was completed and verified on 2026-11-24:

- **Second half** (Audit history, CSV export, Models; migration `0005`): deployed; the owner
  used Audit history, a full CSV export and the Models panel in a browser, and reviewed the
  export. `model_selected` rows are written.
- **Audit-trail follow-ups** (migration `0006`, `session_started`, `mfa_enabled`,
  `temporary_password_accepted`): verified in a browser by the owner. `session_started` rows
  carry the IP address and browser; a sign-in from managed login's session cookie produced a
  `session_started` row and no `sign_in` row, so a cookie sign-in has its own `auth_time`
  and its own row; `mfa_enabled` is written. The trigger's own log (`user_status`, read on
  2026-11-24) settles the `FORCE_CHANGE_PASSWORD` assumption: every first sign-in on the
  tenant pool logged `temporary_password_accepted` with `FORCE_CHANGE_PASSWORD`, then
  `sign_in` with `CONFIRMED` 9 to 15 seconds later (3 first sign-ins; 12 `sign_in` and 1
  `password_reset`, all `CONFIRMED`, and no other status).
- **Portal layout fixes** (banner, row menus, navigation, Audit table at 1280 px): deployed;
  the owner confirmed them on both portals.
- **Cross-tab sign-out** and the **invited users' "Resend invitation"** menu: deployed, and
  covered by the unit and rendered tests; not separately checked in a browser.
- **Migration `0007`** applied by `uv run scripts/migrate.py`. The owner checked the abandoned
  new-password form on the tenant pool: the user stays Invited, and the next attempt returns
  them to the new-password screen. `uv run scripts/migrate.py --command verify` (read-only,
  2026-11-24) reported revision `0007`, `rls_verified: true` and `roles_verified: true`.
- Read-only checks (2026-11-24): the auth-event queue and its dead-letter queue are empty;
  all 7 alarms are `OK` (`api-5xx`, the four auth-event alarms, `break-glass-assumed`,
  `provision-failed`).
- `pulumi preview` on the `dev` stack shows 278 unchanged.
- CI on the `build-step-5` pull request (#4), including the `portal-render` job, must pass
  before `main` is fast-forwarded.
- Known limits: "Known limits (owner decisions, build step 5)" below. What carries forward:
  "Carried forward to step 6" below.

Step 6, first half, was verified on 2026-11-24 (evening, US Eastern; 2026-11-25 UTC):

- Deployed through 99bd9d1: `pulumi up`, `uv run scripts/migrate.py` (`0008`, then `0009`),
  `uv run scripts/update_tenants.py` (bucket CORS, inbox-only malware scanning, plans `ACTIVE`).
  `uv run scripts/migrate.py --command verify` (read-only, after the evening's checks):
  revision `0009`, `rls_verified: true`, `roles_verified: true`. All 19 alarms `OK`; the ingest
  queue and both dead-letter queues empty.
- `run_isolation.py` passed with the inbox fixture (69 of 69 in the Lambda; owner).
- In a browser and with `scripts/ingest_check.py` (owner, Infinity Loans):
  - A normal upload passed fingerprint after the `TaggingDirective=REPLACE` fix, with no
    permission added.
  - A rejected companion: the PDF flagged, all four errors listed, Close blocked, and the
    dialog offered fix, remove or close without the companion.
  - `06-not-a-pdf.pdf`: `failed`, "The file is not a PDF.", no alarm.
  - `ingest_check.py bad-companion`: six errors from the server; close refused with
    `409 batch_not_ready`.
  - `ingest_check.py eicar` (CloudShell): batch `0c490d9d-…`, document `21b2e1ee-…`, file
    `infected` (`threats_found`), document `quarantined`. The tenant view shows the reason and
    no Open PDF. GuardDuty raised `Object:S3/MaliciousFile` (severity 8, "EICAR-Test-File (not
    a virus)") a second after the upload. The finding's object hash is EICAR's SHA-256. Its
    `VersionId` is the inbox version, so the infected version was identified without reading
    it. Organization alerting did not email it; that is the foundation stack's, not this
    project's.
  - Throttle test (reserved concurrency 0 on `assay-dev-pipeline-steps`): document
    `16998f40-…` failed. Tenants see "The fingerprint step failed."; master admins see
    `fingerprint_failed · Lambda.TooManyRequestsException`. The throttle was removed
    (`get-function-concurrency` empty) and `pulumi preview` showed 337 unchanged.
- How each part of the EICAR document's handling is known (read-only, 2026-11-25 UTC; no
  object content read), and what can't be read directly: see "Quarantine evidence" in the
  step 6 notes.
- Known limits carried into the second half: "Known limits (owner decisions, build step 5)"
  and the step 6 notes. What is still open: "Carried forward to step 6", "Still open".

### Carried forward to step 6

Done in the first half of step 6 (see "Step 6, first half" below): inbox keys by opaque file ID;
the reserved fixture ID in `assay_tss.keys`, imported by the isolation tests and refused by
the API and the pipeline (tested); no listing anywhere (session policies and a source test);
the API and the pipeline functions as TSS callers, each with its own accesses; the ingest
queue in the SQS endpoint's policy; bucket CORS and the "update every tenant" command
(`scripts/update_tenants.py`); every new alarm on the alarm topic; the new screens in
`SCREENS`. No new KMS key, so `platform_key_arns` is unchanged.

Still open:

- **Rule 6, before stage 3's deploy:** re-check the AI services opt-out policy and that
  Bedrock invocation logging is off, and show the owner the output, before any content first
  goes to Bedrock (last checked 2026-11-27: `optOut` for every service, logging off). The
  check is `uv run scripts/check_deployment.py --ai-services`, and it runs first in every
  mode of that script. Stage 2 sends page pictures to Textract since 2026-11-27.
- **The second-half plan** (owner, 2026-11-24) is approved; its decisions are under "Step 6,
  second half". What it had to include is all built in stage 1: one worker task per document
  (owner decision 1), reprocessing with attempt numbers, attaching or replacing a companion
  file after ingestion, the quarantine notice (the threat name comes from the scan event
  itself: `detail.scanResultDetails.threats[0].name`, with no GuardDuty permission), and
  refresh-token rotation on both app clients.
- **Titan Text Embeddings V2 (owner decisions 1 and 2):** called by model ID in us-east-1 (it
  has no `us.` profile; a named exception to section 2), with an `ai_models` row (job
  `embed`) and its price left blank for the owner. It arrives with stage 4, in that stage's
  migration; `seed.py` refuses a catalog with a blank price.
- **Remove the "later steps are not available yet" note** (`laterStepsPending` in
  `portal/src/pages/ingestionFormat.ts` and its Alert in `Steps`, both marked
  `TODO(build step 6, second half)`) once steps 5 to 10 exist (stage 4). Until then a document that
  passed fingerprint stays Processing, and the steps panel says why. Add each new step's
  results to `RESULT_TEXT` (plain words; the stored codes are never shown).
- **Portal:** add every new screen to `SCREENS` in `portal/e2e/layout.e2e.ts` (and its data
  to `e2e/fixtures.ts`); use `RowActions` for any row menu. The navigation is now 218 px
  ("Ingestion history"). Run `uv run scripts/check_portal_render.py` after any visible change
  and look at the screenshots. Not covered by it: the CloudFront CSP.
- **Still to check:** whether Cognito's `amr` claim shows MFA (it could replace the
  pre-token trigger's `AdminGetUser`, step 3 note).
- **Dependencies:** bump ESLint to 10.15.0 and `typescript-eslint` to 8.74.0 after
  2026-12-07 (dev-only `minimatch` advisory). Raise the `fastapi` and `pydantic` caps
  deliberately. New in step 6: `aws-lambda-powertools` capped below 3.40 and `pypdfium2`
  below 5.16 (releases younger than two weeks on 2026-11-24).
- **Before production (unchanged):** VPC flow logs to the Log Archive account; an
  approval-gated break-glass role; a Cognito signing-key rotation needs a `pulumi up`.
- **Before production (owner, 2026-11-25): the break-glass rule, log and alarm have never
  been exercised in AWS; test once deliberately.** Nobody has assumed the role, so the
  EventBridge rule has matched nothing, `/aws/events/assay-dev-break-glass` is empty and
  `break-glass-assumed` has never fired. `check_deployment.py` reports the log as not proven.
- **Before deploying to Staging or Production (owner, 2026-11-25): request the Lambda
  concurrent-executions quota increase in that account first.** A new account allows 10
  concurrent executions (Service Quotas L-B99A9384), which is too few for the pipeline: see
  "One scan result failed once" in the step 6 notes. Request 1,000, as for Development:
  `aws service-quotas request-service-quota-increase --service-code lambda --quota-code L-B99A9384 --desired-value 1000 --profile <profile> --region us-east-1`,
  and wait until it is granted before the first `pulumi up`.
  - **The two reserved-concurrency settings cannot be set before the quota is raised.**
    `scanReservedConcurrency` (50) and `tssReservedConcurrency` (300) need an account that
    allows at least 750: the two of them, and 400 unreserved. Lambda always keeps 100
    unreserved, so in an account that allows 10 it refuses the setting and `pulumi up`
    fails. Leave both keys out of a new stack's config until the quota is granted, then
    set both (the program refuses one without the other) and deploy.
  - `uv run scripts/check_deployment.py --before-deploy --stack <stack>` reads the account's
    limit and warns if it is below what the stack's settings need.
- **After every deploy's checks, run `uv run scripts/check_deployment.py`** (read-only): it
  fails if a configured log destination has received nothing, or if the scan function's or
  the TSS's reserved concurrency in AWS is not what the stack's config says, and repeats the
  concurrency warning. See the step 6 notes.

### Reading audit_log

Aurora is private and every tenant table has forced row-level security. As of step 5 only
`platform_reader` may read `audit_log` across tenants, and only the API task can sign in as
it. The migration Lambda signs in as the master user or `migrator` (no policy, so no rows),
the seed Lambda as `platform_writer` (no SELECT on `audit_log`), the isolation Lambda as
`app_tenant` (INSERT only). `GET /admin/audit` (second half of step 5) is the first read path.

**Tenant users' email addresses in Audit history (owner decision, 2026-11-23).** Both master
roles see every audit row's actor and target, including tenant users' email addresses (a
tenant user's sign-in row has their email as the actor). This is deliberate: section 7
asks for an audit history of sign-ins and user changes across tenants. D8 ("no view in
schema `platform` shows a business tenant's non-admin users") applies to the admin list
views, not to the audit record.

## Known limits (owner decisions, build step 5)

These stay as they are into step 6 unless the owner decides otherwise.

- **Inbox objects stay in the bucket** (owner decision 10, build step 6). No lifecycle rule
  expires them: it would need `s3:PutLifecycleConfiguration`, a setting that can destroy data,
  for the provisioning role. A PDF moved to `originals/`, an infected file moved to
  `quarantine/`, a removed file and a discarded batch's files all leave their inbox versions
  (or a delete marker over them). They are never read again: only pipeline sessions can read
  `inbox/`, and the pipeline works only from database rows.
- **The portal's CSP admits every virtual-hosted S3 bucket in the Region**
  (`https://*.s3.<region>.amazonaws.com`; owner decision 11, build step 6). CSP cannot name
  only the tenant buckets (`<prefix>-t-*` is not a valid source). The browser reaches a bucket
  only through a presigned link, which names one key of one tenant's bucket for 15 minutes at
  most.

- **A sign-out from a session already ended by an MFA reset or a disable is not recorded**:
  the account check refuses the token (`session_revoked`), so `POST /auth/signed-out` answers
  403. The reset or disable that ended the session is recorded.

- **A suspended tenant's users can still complete managed login** (D10). Cognito is not
  changed when a tenant is suspended; every API request is refused with `tenant_suspended`
  and the portal says so. Reactivating needs no Cognito change either.
- **A user who has never finished a first sign-in cannot reset their password**
  (observed by the owner, 2026-11-24). While Cognito has them in `FORCE_CHANGE_PASSWORD`,
  "Forgot password" on the managed login page sends no email, and an admin's reset is refused
  (`cognito-admin-api` answers `wrong_state`). The AWS pages read on 2026-11-24 do not
  describe this case. The way back is a new invitation (`AdminCreateUser` with `RESEND`),
  which "resend[s] the invitation message ... and reset[s] the temporary-password duration
  with a new temporary password" (AdminCreateUser API reference). So the Users screen offers
  an Invited user "Resend invitation", explained in the menu, never "Reset password".
  - **Temporary passwords last 7 days in both pools** (`TemporaryPasswordValidityDays: 7`,
    `TEMPORARY_PASSWORD_DAYS` in `infra/assay_infra/auth.py`; checked with
    `describe-user-pool` on 2026-11-24). After that the user cannot sign in until an admin
    resends the invitation. A temporary password is good for one sign-in. The platform pool
    has no resend in the portal: its users are created by `scripts/seed.py` with the owner's
    credentials, and re-running it finds them without resending. An expired platform
    invitation is resent by hand (`aws cognito-idp admin-create-user --message-action
    RESEND`, owner's credentials).
  - **An abandoned new-password form, tenant pool** (owner decision, 2026-11-24, migration
    0007): `temporary_password_accepted` changes nothing in `users`; only `sign_in` sets
    `last_sign_in_at` and moves `invited` to `active`. A user who enters the temporary
    password and abandons the new-password form stays Invited, with "Resend invitation"
    offered. (Under 0006 they were shown Active and offered a reset Cognito refuses.)
- **An abandoned new-password form, platform pool, shows the user Active** (owner decision,
  2026-11-24). There `temporary_password_accepted` is the only event a first sign-in sends
  (the trigger does not run again after MFA setup in managed login), so it keeps the
  sign-in's effect: `last_sign_in_at` set and `invited` to `active`, even if the user never
  set a new password. Cognito still has them in `FORCE_CHANGE_PASSWORD`, so they cannot reset
  their own password either. The way back is a resent invitation, by hand with the owner's
  credentials (the portal has no platform resend):
  `aws cognito-idp admin-create-user --user-pool-id <platform pool> --username <email> --message-action RESEND --profile pilar-dev`.
- **Failed sign-ins are not audited** (section 7a). Only successful sign-ins
  (post-authentication trigger), password resets (post-confirmation), sign-outs and password
  changes (reported by the portal) are recorded.
- **A password change made outside the portal leaves no audit row** (D3): the portal calls
  Cognito's `ChangePassword` directly, then reports it. CloudTrail still records
  `ChangePassword` (it is in the user pool actions CloudTrail logs).
- **Cognito sign-in rows have no IP address or browser**: the post-authentication event
  carries neither (without threat protection). Rows the API writes take the viewer's IP
  from `CloudFront-Viewer-Address`. The portal's `session_started` row, written right after
  the sign-in, carries both; the trigger's `sign_in` row stays the record of the sign-in.
- **Two tabs that share one refresh token: the second to refresh is signed out** (a known
  effect of refresh-token rotation; owner, 2026-11-27). With rotation on, a refresh returns
  a new refresh token and the old one stops working 10 seconds later.
  - **Who it affects.** Only tabs that hold the same refresh token: a duplicated tab, or one
    the browser restored (both copy the tab's sessionStorage). Tabs that signed in
    separately each have their own tokens and never meet it.
  - **What happens.** Whichever of the two refreshes second, more than 10 seconds after
    the first, is refused (`RefreshTokenReuseException`; `invalid_grant` is what the
    portal's tests assume `/oauth2/token` answers, which AWS does not document). The first
    tab stays signed in: on the replica, reusing a rotated-out token revoked nothing else
    (the isolation run checks that every time). Inside the 10 seconds both are accepted.
  - **What the user sees in the second tab.** The sign-in screen with "Your session has
    ended. Sign in again to continue." and a Sign in button. Not an error page, not the app
    left open over a dead token, and no retry loop (`Root.tsx`, `SESSION_ENDED`; tested in
    `Root.test.tsx`). If the refusal comes in the background, a minute before the access
    token expires, the tab goes to that screen then. If the tab is reloaded later, it gets
    there after one failed refresh. Sign in usually needs no password: managed login's own
    session is still open, unless someone signed out.
  - The same screen shows for the other two ways a session cannot be renewed: 8 hours after
    the sign-in, and after an admin's disable or MFA reset. The tab cannot tell which it
    was, so the message does not say.
  - It writes no audit row (there is no valid token left to report with) and does not sign
    out the portal's other tabs.
  - A refresh that got no answer (offline, a timeout) is not a refusal: the app stays open
    until the access token expires, the portal tries once more, and only then shows the
    sign-in screen, keeping the stored tokens so a reload can try again.
- **A portal tab that is closed or offline when the user signs out in another tab keeps its
  refresh token** until it expires or is revoked by an admin action (a disable or an MFA
  reset, which also ends its sessions through `sessions_revoked_at`). Sign-out signals only
  the tabs open in that browser at the time (BroadcastChannel). An offline tab misses the
  message and can refresh once it is back; a closed tab that the browser restores (reopen
  closed tab, session restore) brings its sessionStorage back, refresh token included.
- **Platform-pool MFA enrolment is not recorded** (owner decision, 2026-11-24). Platform
  users set up TOTP in managed login, and no Cognito trigger runs for it (post-authentication
  did not run after MFA setup either). Only CloudTrail sees it (`VerifySoftwareToken`, with
  the user's `sub`, no pool ID, and Cognito's own address as the source). Recording it would
  need an EventBridge rule that sends to the auth-events queue, a new sender on that queue.
  It is not needed for the trail: the platform pool requires MFA, so a platform user enrols
  exactly at their first sign-in and at the first sign-in after an MFA reset, both of which
  the trail already shows: the `temporary_password_accepted` row, or the `mfa_reset` row
  followed by that user's next `session_started` (whether Cognito runs the trigger for a
  sign-in that ends in MFA setup is not known; on the first sign-in it did not).
  Tenant-pool enrolment, made on the portal's MFA screen, is recorded (`mfa_enabled`).

## Fixed facts (BUILD_SPEC.md section 2)

| Item | Value |
| --- | --- |
| AWS account | Development, `429100266783`, in the Workloads OU of the Pilar organization |
| Region | `us-east-1` for everything. A root SCP denies all Regions except `us-east-1` and `us-east-2` |
| Bedrock rule | Call models only through `us.` inference profiles from `us-east-1`. Direct model IDs in other Regions and `global.` profiles are denied by SCP |
| Extraction and routing model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Answer model | `us.anthropic.claude-sonnet-4-6` |
| Embedding model | Amazon Titan Text Embeddings V2 |
| Tenant portal host | `dev.assayinsights.ai` |
| Admin portal host | `admin.dev.assayinsights.ai` |
| DNS | Route 53 hosted zone `dev.assayinsights.ai` in the Development account. The registrar is Squarespace; the owner adds the NS records there by hand once the zone exists |
| IaC | Pulumi in Python, state in Pulumi Cloud (organization `pilartech`), project `assay-platform`, stack `dev`, run with `pulumi up` from VS Code |
| Already in place | Control Tower landing zone, organization CloudTrail, AWS Config, GuardDuty and Security Hub (managed by a separate `pilar-foundation` stack). Do not manage any of these here. Account Factory creates no VPC |
| Language | Python 3.12+, `uv` for locked dependencies. Portal in React and TypeScript |

These values belong in stack config or the `ai_models` table, not in code. They are
listed here for reference only.

### Development environment

Development happens on Intel (x86-64) Windows 11 Professional, in VS Code with
PowerShell, Docker Desktop on WSL 2, and `pulumi up` run by hand.

- Build every container image for `linux/amd64`. Set Fargate tasks to `X86_64` and
  Lambda functions to `x86_64`. Do not use ARM (Graviton) anywhere.
- Never package Lambda code or native dependencies (`pypdfium2`, `psycopg`, `pyHanko`)
  from the Windows host. Build them inside a Linux container, or ship those functions as
  container images.
- Pin Python to the newest version that AWS Lambda supports and that every native
  dependency publishes Linux x86-64 builds for; prefer one release behind the latest.
  `.python-version` is currently **3.14** (Lambda runtime `python3.14`, base image
  `public.ecr.aws/lambda/python:3.14`). On 2026-11-23, psycopg-binary, pypdfium2,
  cryptography, and pyHanko's native dependencies (lxml, aiohttp, cffi) all had
  Linux x86-64 wheels for 3.14. Before adding a native dependency, confirm it has a
  3.14 Linux x86-64 wheel. Use the same version in container images and manage it with
  `uv`. Do not rely on the Python installed on the machine. The pin appears in
  `.python-version`, both `pyproject.toml` files (`requires-python`, ruff
  `target-version`, mypy `python_version`) and `uv.lock`. Change them together.
- `.gitattributes` forces LF line endings for Dockerfiles, shell scripts used inside
  containers, and Python files.
- Use `pathlib` and forward slashes; never hard-code Windows or POSIX paths.

## Tenant isolation: non-negotiable (BUILD_SPEC.md section 6)

These rules are not negotiable. A change that weakens one must be rejected.

1. Each business tenant gets its own S3 bucket (`assay-dev-t-{8 hex}`; never the
   customer's name), its own customer-managed KMS key, and its own S3 Vectors bucket and
   index. Bucket settings: Block Public Access, SSE-KMS with the tenant key and Bucket
   Keys on, versioning, Object Lock in governance mode with 1-day default retention,
   GuardDuty Malware Protection for S3 enabled, and a bucket policy denying every
   principal except the tenant role and a break-glass role.
2. Only the Tenant Storage Service (TSS) may assume the tenant role. It passes session
   tags `tenant_id`, `bucket` and `vector_bucket`, a source identity naming the calling
   service and job, and an inline session policy listing the exact bucket, key, index and
   Rekognition collection. Credentials last 15 minutes. The role's policy is one template
   built from `${aws:PrincipalTag/...}`.
3. No other code builds an S3 key or picks a KMS key. Callers ask the TSS for a storage
   context and build object keys only through it.
4. The tenant ID comes only from the verified token or a pipeline job message, never from
   a request body, query string or model output.
5. Every tenant table has a `tenant_id` column and a forced row-level security policy.
   The app sets `SET LOCAL app.tenant_id` at the start of every transaction.
6. Model invocation logging stays off. Apply the AI services opt-out policy if the
   organization hasn't already applied it.

Owner decisions on these rules (2026-11-23):

- **Rule 1, bucket policy.** Two more principals are named besides the tenant role and the
  break-glass role, each for one job: the GuardDuty scan role (read and tag objects) and
  the provisioning role (bucket settings, never object data). Without them malware
  scanning and bucket management could not work.
- **Tags are part of the lock.** Only the provisioning role may tag or untag a tenant KMS
  key, or any other resource whose tags or name the tenant role's policy matches on.
- **No S3 key may contain a file name.** Every part of a key is an ID or fixed text. An
  upload is stored under an opaque `file_id` and its `file_name` is kept only in the
  database, so a role that can list keys (the provisioning role can) learns nothing about
  what a tenant uploaded. `assay_tss.keys` builds keys from UUIDs only.
- **Break glass.** `assay-dev-break-glass` is assumable only by the owner's IAM Identity
  Center `AdministratorAccess` role (`breakGlassPrincipalArn`), and every AssumeRole of it
  raises an alarm. **Production needs a separate, approval-gated role instead.**
- **Rule 6.** The owner attached the AI services opt-out policy at the organization root
  (policy `p-90y1yu22fy`, from the management account) on 2026-11-23. Verified from this
  account on 2026-11-24 UTC: the effective policy is
  `{"services":{"default":{"opt_out_policy":"optOut"}}}` (every AI service, Rekognition and
  Textract included), last updated 2026-11-24 04:37:58 UTC, and Bedrock model invocation
  logging is off (`aws bedrock get-model-invocation-logging-configuration` returns no
  configuration). The policy ID is the owner's; a member account cannot list the policies
  attached to it. Checked again on 2026-11-24 for the step 6 plan: the same effective policy,
  last updated 2026-11-24 04:37:58 UTC, and no invocation logging configuration. Check both
  again before any content first goes to Bedrock, Textract or
  Rekognition:
  `aws organizations describe-effective-policy --policy-type AISERVICES_OPT_OUT_POLICY --profile pilar-dev`.

Related rules elsewhere in the spec that protect isolation:

- Master roles never see tenant content: no file names, no page text, no answers, no
  PDFs, no fraud scores.
- Treat document text as untrusted input. The model gets no tools beyond the extraction
  schema, and nothing it returns chooses a tenant, a bucket or a SQL statement.
- Never send a face image to Claude. Rekognition is reached only with TSS-issued tenant
  credentials, using one collection per tenant.
- `tests/isolation` must show that every cross-tenant access attempt fails, against the
  real account.

## Repository

```
infra/      Pulumi program (Python). Components live in infra/assay_infra/
db/         Database package assay_db: models, Alembic migrations, roles, migration Lambda
api/        FastAPI app (ECS Fargate)
workers/    Pipeline code: Lambda handlers and the Fargate worker
tss/        Tenant Storage Service (Lambda) and its client package
schemas/    Pydantic v2 models shared by api/ and workers/
portal/     React + TypeScript + Vite + Cloudscape single-page app
scripts/    seed.py and operational scripts
tests/      tests/unit (CI) and tests/isolation (real AWS, not in CI)
assets/     Logo and favicon SVGs
```

- The Python projects form one `uv` workspace (root `pyproject.toml`). Add new packages
  as workspace members. Every Dockerfile that runs `uv export --locked` must then copy the
  new member's `pyproject.toml`, and `WORKSPACE_MANIFESTS` (`assay_infra.images`) must list
  it; `test_tenancy_images.py` fails otherwise. A new member changes every image's tag, so
  every image rebuilds on the next `pulumi up`.
- CI (`.github/workflows/ci.yml`) runs ruff (lint and format), mypy (strict), pytest,
  pip-audit and Bandit. It never deploys.
- **GitHub Actions pinning (SOC 2).** Pin every action to a full 40-character commit
  SHA, with the release in a trailing comment:
  `uses: owner/repo@<sha> # vX.Y.Z`. Never use a tag or branch alone. Before using or
  bumping a version, verify that it exists, and never guess it from memory. Some
  actions, astral-sh/setup-uv among them, publish no moving major tag such as `v10`.
  - `gh api repos/<owner>/<repo>/releases/latest --jq .tag_name` gives the latest release.
  - `gh api repos/<owner>/<repo>/tags --jq '.[] | .name + " " + .commit.sha'` gives the
    tag's commit SHA.
  - `gh api repos/<owner>/<repo>/commits/<sha>` confirms that the commit exists.
  - Check that every `with:` input the workflow passes is still in the action's
    `action.yml` at that SHA. Fetch it raw:
    `gh api -H "Accept: application/vnd.github.raw" "repos/<owner>/<repo>/contents/action.yml?ref=<sha>"`.
  - Prefer a release that has been out for at least a couple of weeks over one that is
    only days old. setup-uv is on v10.3.3 rather than v10.4.0 for this reason.
- Run the checks locally with `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `uv run pytest`. After any portal change that shows on screen, also
  `uv run scripts/check_portal_render.py` (Docker), and look at the screenshots it writes to
  `.portal-render/results/screens/`.
- The database tests (`tests/unit/test_db_security.py`) need a throwaway PostgreSQL 17;
  without one they skip locally, and CI (`ASSAY_REQUIRE_DB_TESTS=1`) fails instead:
  `docker run -d --name assay-pg-test -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55432:5432 postgres:17`
  then `$env:ASSAY_TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:55432/postgres"`.
- Every AWS resource is tagged `app=assay-insights`, `env=dev` through `aws:defaultTags`
  in stack config.

## Notes from the build

- Python 3.14+ no longer creates a default event loop. Pulumi mock tests create one
  explicitly (see `tests/unit/test_dns.py`).
- The ACM certificate is created with its DNS validation records, but nothing waits for
  issuance yet, so `pulumi up` doesn't block on delegation. Add an
  `aws.acm.CertificateValidation` waiter when CloudFront first uses the certificate.
- The hosted zone is `protect=True`. Replacing it would change its name servers and break
  the Squarespace delegation.
- **DNS layout.** The parent zone `assayinsights.ai` is hosted at Squarespace
  (`nsd1`–`nsd4.squarespacedns.com`) with DNSSEC turned on. Only `dev.assayinsights.ai`
  is delegated to Route 53, through four NS records at Squarespace. Every other name in
  the parent stays at Squarespace, and neither this repo nor Pulumi manages it. The
  `dev` delegation is deliberately unsigned, so never add a DS record for `dev` at
  Squarespace and never enable DNSSEC signing on the Route 53 zone.
  - Observed 2026-11-23: Squarespace publishes DNSKEYs for the parent, but `.ai` had no
    DS record for `assayinsights.ai`, so resolvers treated the zone as unsigned (no AD
    flag). Earlier that morning it had validated. A DS that doesn't match the DNSKEYs
    would break resolution of the whole domain, `dev` included.
- **ACM `CAA_ERROR`.** ACM checks CAA records up the tree to `assayinsights.ai`. If the
  parent's name servers fail, validation fails with `CAA_ERROR` and ACM does not retry.
  The fix is to replace the certificate:
  `pulumi up --cwd infra --stack pilartech/dev --replace '<certificate URN>'`.
  Use single quotes, because the URN contains `$aws`.

### Step 2 (network, database, migrations, roles, RLS)

- **VPC flow logs** go to this account's CloudWatch Logs (30 days) for now. Move them to
  the Log Archive account before production.
- **The default VPC** (`172.31.0.0/16`) exists in the account despite section 2. The owner
  decided on 2026-11-23 to leave it alone. Pulumi does not manage it.
- **Workload zone.** Workloads and interface endpoints run in `workloadAzId` (`use1-az2`).
  S3 Vectors is not offered in `use1-az3`; check every endpoint service before changing it.
  (There is no `cognito-idp` endpoint; see step 3.)
- **Migrations run only in the migration Lambda**, started by `uv run scripts/migrate.py`
  after `pulumi up` (and monthly by EventBridge Scheduler for `audit_log` partitions).
  `--command verify` is read-only. It checks in Aurora that every table except
  `ai_models` has `tenant_id` and forced RLS with a policy (`assay_db.checks.verify_rls`),
  and that no role has SUPERUSER, BYPASSRLS or an unexpected membership
  (`roles.verify_roles`). Every `upgrade` runs the same checks before committing. Aurora
  is private, so this command is the way to inspect it.
  Never run them from `pulumi up`. The Lambda bootstraps the roles as the RDS master user,
  then runs Alembic as `migrator`.
- **Database sign-in.** Every login role uses IAM database authentication (`rds_iam`); only
  the master user has a password (RDS-managed, in Secrets Manager). Grant a service
  `rds-db:connect` for exactly the database user it needs.
- **`platform_reader` never gets direct table access** beyond `tenants` and `audit_log`.
  Admin routes that need more (tenant admin names, user and document counts, models) get a
  narrow view in schema `platform`, owned by `platform_view_owner`, which is granted only
  the columns the view needs.
- **New tables** need, in their own migration: `ENABLE` and `FORCE ROW LEVEL SECURITY`,
  policies and grants for each role that uses them. Migrations keep their SQL inline rather
  than importing it, so history doesn't change. `test_db_security.py` checks that every
  table except `ai_models` has `tenant_id` and forced RLS, and checks the exact privilege
  matrix, so update `EXPECTED_PRIVILEGES` there in the same change.
- **Aurora's master user is not a superuser** (CREATEROLE, CREATEDB, `rds_superuser`).
  Under PostgreSQL 16+ it may not mention `SUPERUSER`, `BYPASSRLS` or `REPLICATION` in
  `CREATE ROLE` or `ALTER ROLE`, even to switch them off. It may alter or grant only roles
  it holds ADMIN OPTION on (those it created, plus `rds_iam`). So the bootstrap relies on
  the defaults when it creates a role, and `verify_roles()` checks attributes and
  memberships in the same transaction instead of forcing them. The database tests run the
  bootstrap as `rds_master_test`, a non-superuser stand-in, and sign in directly as each
  role. Never test database code only as a superuser.
- `platform_writer` cannot read `tenant_storage`, so provisioning retries use
  `INSERT ... ON CONFLICT DO NOTHING` with no conflict target (a target needs `SELECT`).
- **Lambda container images** are built by `scripts/build_image.py` (run by a Pulumi
  `command.local.Command`) with `--provenance=false --sbom=false`. Docker Engine 29 adds
  provenance attestations by default, which pushes an OCI image index that Lambda
  rejects. The `pulumi-docker-build` provider cannot turn attestations off, so don't use it
  for Lambda images. Base images in Dockerfiles are pinned by digest.
- **"`<repository>@sha256:...`: not found" during `pulumi up` was the build's own check, not
  a missing image** (diagnosed 2026-11-25, read-only). It failed twice in 99 pushes:
  `assay-dev-isolation-tests` on 2026-11-24 19:54 UTC and `assay-dev-pipeline-worker` on
  2026-11-25 16:52 UTC (stack update 37).
  - **What CloudTrail shows.** Both times ECR accepted the manifest (`PutImage`, tagged,
    with the digest the error names; the image has been in the repository ever since). The
    check (`docker buildx imagetools inspect`, on the host) came 37 and 38 seconds later,
    and its request for the manifest never reached ECR: there is no `BatchGetImage` for the
    digest, where every successful push has two. ECR saw only a
    `BatchCheckLayerAvailability` for the manifest's digest, which is the registry client's
    fallback (it asks for the digest as a layer), and a manifest is never a layer.
  - **Why it read as "not found".** In the resolver buildx 0.38.1 ships, a manifest request
    that fails to connect is ranked below a 404, and the fallback still runs; its 404 then
    replaces the real error. The client gives a connection 30 seconds.
  - **Why the connection failed.** Both were runs in which every image rebuilt: eight
    builds push at once and fill the uplink for minutes (below). In those runs the check
    waited 2 to 33 seconds when it passed; the two failures are the two longest waits. In
    runs that upload little it answers in 0 to 2 seconds. **Not seen directly:** the
    failed request's own error, which the script did not keep.
  - **Ruled out:** a lifecycle rule expiring it (the rule expires the oldest images beyond
    10 by push time; CloudTrail's `PolicyExecutionEvent`s show only old tagged images
    going, and no repository holds an untagged manifest); a cache or attestation manifest
    (attestations are off, no cache is exported, and the digest in the error is the pushed
    image's own); a wrong reference.
  - **The fix** (`scripts/build_image.py`): the pushed image is confirmed through ECR's own
    API (`BatchGetImage` by digest, up to 7 tries over about 90 seconds), and a failure
    says whether the request failed or ECR had no image. No registry client is used for
    the check. And an image already in the repository under its tag is used as it is: the
    tag is the hash of the sources and tags cannot be overwritten, so a run that failed
    after its push costs nothing to repeat.
  - **What the next full rebuild showed (stack update 39, 2026-11-27 14:41 UTC; CloudTrail,
    read-only).** Eight images were pushed within four seconds of each other, and each was
    confirmed by the first `BatchGetImage` from the build script, 0 to 1 second after its
    `PutImage`. The update succeeded first time. **It does not prove the fix**: that day
    the layers uploaded in 2 to 6 seconds, not minutes, so the connection was never full
    and no confirmation had to be retried. Why the upload was so much faster than on
    2026-11-25 (about 5 Mbit/s then) is not known. The retry path is covered by unit tests
    only.
  - **Why a full rebuild takes 6 to 7 minutes: it is the upload, not the build.** In update
    37 all eight builds finished within 23 seconds (their layers were cached). Then 259 MB
    went to ECR over about 7 minutes, roughly 5 Mbit/s. Each image's last layer holds every
    Python dependency and the code together (`COPY --from=build /out ...`: 26 to 53 MB
    compressed), so one changed source file uploads the dependencies of every image again.
    **Done (owner decision, 2026-11-25): the dependencies and the code are two layers**
    in every Dockerfile that installs locked dependencies (eight; the three Cognito
    trigger images have none). The build stage installs the wheels to `/deps` and copies
    the code to `/code`; the final stage copies `/deps`, then `/code`, to the same place
    with the same modes. A code change now uploads the code layer only.
  - **The owner's conditions: image contents and the pinned base image digests must not
    change.** Checked once, by building every image from its Dockerfile as it was and as
    it is, from the same sources, and comparing every file in them (path, type, mode,
    owner, size, SHA-256), the installed packages, the image's settings (environment,
    command, user, working directory, ports, volumes) and the `FROM` lines.
    **Result (2026-11-25): identical in all eight.** The same packages (21 to 40 an
    image), the same files (10,402 to 23,898 an image) with the same modes, owners, sizes
    and hashes, the same settings and the same `FROM` lines; one more layer each. The one
    path that differed in every image is `/etc/hostname`, which is not in an image: Docker
    writes each container's own ID there when it starts.
  - The code layers are small: all the code trees together are about 840 KB, 220 KB
    compressed, against 26 to 53 MB for the old layer.
  - **The lasting test** is `tests/unit/test_image_layers.py`. `tests/unit/image_packages.json`
    records every distribution each image's interpreter could see **before** the split;
    the test builds each image and fails if one is gained, lost or at another version. It
    also holds every base image to the three pinned digests, and the two `COPY` lines to
    their order. **After a deliberate change to `uv.lock` or a base image, run
    `uv run scripts/image_packages.py --write`** and read what the record gained and lost
    before committing it. A new Dockerfile that installs dependencies must be added to
    `IMAGES` in that script.
  - This changes every one of those Dockerfiles, so all eight images rebuild and upload
    once more on the next `pulumi up` (the slow kind, for the last time for code changes).
- **Line endings: files Git marks as LF must be LF on disk** (owner, 2026-11-27, after it
  happened twice). On 2026-11-25, 18 files in the working tree had CRLF although Git stores
  them with LF (`git ls-files --eol`: `i/lf w/crlf`): `.dockerignore`, `api/pyproject.toml`
  and sources that go into images among them. Git shows no difference for such a file, but
  an image is built from the bytes on disk and tagged with their hash, so the image holds
  the CRLF copy under a tag no clean checkout produces.
  - **Git did not write them, and `core.autocrlf` is not the cause.** It is `true` on the
    owner's machine (Git for Windows' system default), but a file marked `eol=lf` is
    checked out with LF whatever it says: a fresh clone under that setting had no such
    file with CRLF (2026-11-27). All 18 were last saved one at a time, most on 2026-11-24
    between 12:42 and 15:23, so an editor or a tool saved them with the platform's line
    endings. Which one is not known. Two known ways: VS Code creates new files with CRLF
    on Windows (`files.eol` is `auto`), and Python's `Path.write_text()` writes CRLF on
    Windows unless it is given `newline="\n"`. **Scripted edits must pass `newline="\n"`.**
  - What `core.autocrlf=true` does do: every file that is only `text=auto` (the portal's
    `.ts` and `.tsx`, `.json`, `.svg`, 74 files) is checked out with CRLF. That is
    harmless to Git; the portal build's hash and the branding SVGs then depend on the
    machine. **Not changed:** making them LF would change the bytes of the SVGs Cognito's
    branding is built from, which is a change to deploy on purpose.
  - **Prevention, four layers.**
    1. `uv run scripts/check_line_endings.py` lists the files (`--fix` rewrites them with
       LF; line endings only, and Git sees no change). `tests/unit/test_line_endings.py`
       runs it, so `uv run pytest` fails on the development machine, where the problem
       arises.
    2. CI runs the same script. A Linux checkout has no CRLF, so there it can only catch
       CRLF that was committed.
    3. `assay_infra.images.source_tag` refuses an image source with CRLF, so `pulumi
       preview` and `pulumi up` stop before an image is built from one.
    4. `.editorconfig` sets LF for exactly the patterns `.gitattributes` marks (a test keeps
       the two lists in step). VS Code needs the EditorConfig extension for it
       (`.vscode/extensions.json` recommends it); without the extension, set
       `"files.eol": "\n"` in VS Code's user settings.
  - `*.md` is now marked `eol=lf` too: `tests/isolation/README.md` goes into the isolation
    image, and was the one image source Git could check out with CRLF. A test fails if
    anything that goes into an image is not marked.
  - After `--fix`, `git status` lists the rewritten files as modified until they are
    staged once, although `git diff` shows nothing: Git's cached file sizes are stale.
- **ECR login on Windows.** An ECR token (about 3,100 characters) is too long for the
  Windows Credential Manager ("The stub received bad data"). `scripts/build_image.py`
  logs in, builds and pushes with `DOCKER_CONFIG` pointing at a temporary directory that
  has no `credsStore`. It also removes every `docker-credential-*` helper directory from
  `PATH` for those subprocesses, because without a `credsStore` the CLI otherwise falls
  back to `wincred`. It checks the token stayed in the file, then deletes the directory.
  The user's Docker config and credential store are never touched.
- **Files in Lambda images must be readable by a non-root user**: Lambda runs code as uid
  993, not root. `ADD <url>` always creates files with mode 0600. `COPY --chmod` applies its
  mode to any parent directory it creates too, so create the directory first
  (`RUN install -d -m 0755 ...`). `tests/unit/test_migrate_image.py` builds the image and
  checks the RDS CA bundle as uid 993: genuine (pinned checksum), 0444 in a 0755 directory,
  read by libpq, and `verify-full` still enforced. It needs Docker and skips without it,
  except in CI (`ASSAY_REQUIRE_IMAGE_TESTS=1`).
- **Aurora engine version** is in `ignore_changes`, because minor versions upgrade
  automatically. To upgrade on purpose, change `dbEngineVersion` and lift the ignore for
  that `pulumi up`.

### Step 3 (Cognito, managed login, API skeleton, `GET /me`)

- **No Cognito VPC endpoint.** `com.amazonaws.us-east-1.cognito-idp` exists (use1-az2, az4,
  az6), but AWS documents that "user pools with domains assigned are incompatible with AWS
  PrivateLink", and managed login needs a domain. So section 4's fallback applies:
  - **The JWKS is read at `pulumi up`** (`assay_infra.auth.fetch_jwks`, on the machine
    running Pulumi) and passed to the API task in `COGNITO_*_JWKS`. A Cognito signing-key
    rotation therefore needs a redeploy (`pulumi up`, which re-reads it and rolls the task).
  - **Cognito admin calls** (invite, disable, resets, global sign-out) will run in a Lambda
    outside the VPC that the API invokes, built in step 5 with the first route that needs
    one. That step also adds the `lambda` interface endpoint.
- **ECS container definitions are written as ECS stores them**
  (`assay_infra.api.container_definitions`): `hostPort` equal to `containerPort`
  (awsvpc), `cpu` 0, and empty `mountPoints`, `volumesFrom`, `systemControls` and
  `capabilities.add`. After the first deploy of step 3, leaving them out made every preview
  replace the task definition. Any new container field must match what
  `aws ecs describe-task-definition` returns, or the diff comes back.
- **A task can never start with an empty JWKS.** `check_environment()` refuses to build the
  task definition unless every variable the API reads is set and both JWKS hold an RS256
  signing key, so `pulumi preview` and `pulumi up` fail first. If one still arrives, the
  API logs "API settings unusable; not starting" and exits with status 1 before serving
  (`assay_api.__main__`), so the target never turns healthy and ECS's circuit breaker
  keeps the previous task. `test_api_settings.py` and `test_api_image.py` cover both.
- **The ALB-to-CloudFront hop must use HTTPS** when the edge is built in step 5. Until
  then the internal ALB has a plain HTTP:80 listener and its security group has no inbound
  rule, so nothing can reach it. ALB access logs go in with the edge's logging bucket.
- **Tenant ID and MFA in the access token.** Cognito puts `custom:tenant_id` in the ID token
  only. The tenant pool's pre-token-generation trigger (event V2_0, Essentials plan,
  `api/assay_api/cognito_pre_token.py`) copies it into the access token as `tenant_id`, and
  adds `mfa` (true when `AdminGetUser` shows TOTP). It runs outside the VPC with no database,
  and refuses to issue a token without a valid tenant ID. If `AdminGetUser` fails, `mfa` is
  false and the sign-in still goes ahead. `custom:tenant_id` is immutable (set once, at
  user creation) and is never in an app client's write attributes.
  - **Check later:** whether Cognito's `amr` claim in a real access token shows MFA. If it
    does, it can replace the `AdminGetUser` call.
- **Every tenant and admin route takes its caller from `assay_api.security`**
  (`TenantMember`, `TenantAdmin`, `PlatformMember`, `MasterAdmin`, or `AnySignedIn` for
  `GET /me` and `POST /auth/signed-out`). They verify the token, the pool, the role and MFA,
  then run the account check (`assay_api.accounts.load_account`) in the request's
  transaction: user row exists, not disabled, tenant `active`, token group equals
  `users.role`. Access tokens live 60 minutes, so this is what refuses a disabled user or a
  suspended tenant on the next request. `test_api_routes.py` fails if a route skips it.
  A tenant-admin without `mfa` gets only `GET /me` (with `mfa_required: true`).
- **Tenant routes run as `app_tenant`** with `set_config('app.tenant_id', ..., true)` from
  the token; **admin routes run as `platform_reader`** in read-only transactions. Platform
  users are read through `platform.platform_users_v` (migration `0002`), whose owner's RLS
  policy on `users` shows only the platform tenant's users.
- **Managed login branding** (`infra/assay_infra/branding.py`). Prefix domains
  (`tenantAuthDomainPrefix`, `platformAuthDomainPrefix`), because a custom domain needs an
  A record on `dev.assayinsights.ai`, which arrives with CloudFront. Gold on white fails
  WCAG AA, so links are navy and focus rings `#B07D12`.
  - Cognito validates branding only at `pulumi up`, so the program checks every asset when
    it runs and a bad one fails `pulumi preview` and `tests/unit/test_branding.py`. The
    checks are the documented limits (file types per category, base64 at most 1,000,000
    characters, request at most 2 MB, the SVG element and attribute allow-lists, with
    disallowed attributes such as `role` and `aria-label` stripped before upload), one
    undocumented limit and a few rules of our own.
  - The undocumented limit, learned from the second failed `pulumi up` of step 3: logos
    need a width:height ratio between 1:1 and 4:1, read from the SVG's viewBox. The portal
    logo (520x120, 4.33:1) fails, so the form logo is `assets/assay-insights-logo-login.svg`:
    the same artwork with viewBox `0 -10 520 140` and matching width and height (3.71:1,
    vertically centred). The portal keeps `assay-insights-logo.svg`.
  - Our own rules: favicons square, width and height (if given) matching the viewBox
    ratio, and no `href`, `style`, `a`, `image`, `audio`, `video`, `canvas` or `script`.
- **No custom Cognito message templates while email uses `COGNITO_DEFAULT`.** The first
  `pulumi up` of step 3 failed creating the platform pool: the provider sends all three
  invitation template fields (email message, subject, SMS message) whenever
  `invite_message_template` is set, so the unset SMS message went out as `""`
  (minimum 6 characters). The API reference also allows a custom email message and subject
  only with SES (`DEVELOPER`). So invitations, verification and reset codes use Cognito's
  built-in text, and the invitation has no portal link. When SES arrives, add the template
  with every field valid (SMS: 6 to 140 characters with `{username}` and `{####}`).
  `test_infra_auth_api.py` fails if a template or message setting appears while email
  uses `COGNITO_DEFAULT`. Cognito validates these only at deploy time; `pulumi preview`
  does not.
- **User pools are `protect=True`** with Cognito deletion protection on: replacing a pool
  deletes its users. Custom attributes cannot change after creation.
- **Section 7a's Cognito audit triggers belong to build step 5** (portal shell, sign-in,
  `seed.py`), not step 3, for three reasons:
  1. Cognito waits at most 5 seconds for a trigger and fails the sign-in on a timeout,
     while Aurora at 0 ACU takes about 15 seconds to resume. A synchronous `audit_writer`
     insert from the trigger cannot meet "never block a sign-in". Step 5 needs a design
     decision from the owner, for example a trigger that only queues the event on SQS (the
     endpoint exists) and a consumer Lambda that writes `audit_log`.
  2. A platform-pool `sign_in` row needs the platform tenant's ID, which `audit_writer`
     cannot read and which exists only after `seed.py`.
  3. `password_changed` comes from the change-password path and `sign_out` from
     `POST /auth/signed-out`. Both are first used by the portal's user menu in step 5.
- **The API image** (`api/Dockerfile`) is python:3.14-slim pinned by digest. It runs as uid
  10001 with a read-only root filesystem and all capabilities dropped.
  `tests/unit/test_api_image.py` checks that, plus the health check, a 401 from `GET /me`,
  JSON logs and the RDS CA bundle. The trigger image (`api/pre-token.Dockerfile`) is the
  Lambda base plus two files and uses the base image's boto3.
- **FastAPI 0.16x keeps included routers lazily** (`_IncludedRouter`), so `app.routes`
  does not list their endpoints. Walk them with `fastapi.routing.iter_route_contexts`.
- **Young releases.** `api/pyproject.toml` caps `fastapi` and `pydantic` below releases
  younger than two weeks (on 2026-11-23). Raise the caps deliberately.
- **Test client: `httpx`, not `httpx2`.** Starlette 1.10 builds `TestClient` on `httpx2`
  and deprecates plain `httpx`, but still supports `httpx>=0.27,<0.29` (its `full` extra).
  `httpx2` (github.com/pydantic/httpx2, PyPI owner Kludex) is not published by the httpx
  maintainers (encode), so the owner decided on 2026-11-23 not to use it. Starlette's
  deprecation warning is filtered in pytest config. Neither package is in any image.

### Step 4 (Tenant Storage Service, tenant provisioning, `tests/isolation`)

- **Where things are.** `tss/assay_tss` holds the types, the only object-key builders, every
  isolation policy (`policies.py`), the Lambda (`service.py`, `handler.py`) and the
  provisioning task (`provision/`). `db/assay_db/tenants.py` is the add-tenant path.
  `infra/assay_infra/tss.py`, `provisioning.py` and `isolation.py` deploy them. See
  `tss/README.md` for who may do what to a tenant's storage.
- **Tenant stacks keep their state in S3, not Pulumi Cloud** (owner decision): the
  provisioning task has no internet route. Stack `assay-tenant/<tenant_id>` lives in the
  state bucket (`pulumi stack output tenant_state_bucket`), with secrets under its KMS key.
  Only the provisioning role can read state objects or use that key, so a tenant stack can
  be inspected only by running the task. Bucket settings stay with the account's IAM, and
  an administrator can list stack names (tenant IDs).
- **The Pulumi CLI and AWS provider plugin are baked into the task image**
  (`tss/provision.Dockerfile`), each checked against a pinned SHA-256. Their versions must
  equal `pulumi` and `pulumi-aws` in `uv.lock`; `test_tenancy_images.py` fails otherwise.
  To upgrade, change `uv.lock`, both version strings and both checksums together. The CLI's
  SHA-256 is in the release's `pulumi-<version>-checksums.txt`. The plugin's release
  publishes a SHA-1 only: download it, check the SHA-1, and pin the SHA-256 you compute.
- **The task's root filesystem is read-only.** `/work` is a task volume and the only
  writable path (`HOME`, `TMPDIR`, `PULUMI_HOME`). `TMPDIR` must already exist when Python
  starts, because importing `pulumi` reads it, so it is `/work` itself and not a
  subdirectory. The image test previews the real tenant program with no network, which is
  also what checks that every resource in it is valid for the provider.
- **One add-tenant path.** `assay_db.tenants.add_tenant` inserts the tenant
  (`provisioning`) and its audit row; the provisioning state machine does the rest. Its
  input is `{"request": ...}` (`assay_tss.provision.task.ProvisionRequest`). The request
  carries the first admin's name and email, because no table holds them until the admin's
  user row is written at the end; the state machine never logs execution data. In step 4
  the task inserts the row itself (`scripts/add_tenant.py`); step 5's `POST /admin/tenants`
  inserts it first, then starts the same execution. A retry starts a new execution with
  the last execution's request and `retry: true`.
- **Every provisioning step can be repeated.** The failed step is recorded in `audit_log`
  (`provisioning_failed`, `detail.step`: `tenant`, `storage`, `storage_row`, `invite_admin`,
  `activate`, or `task` when the task itself died and the state machine's catch recorded
  it). A tenant that is not `provisioning` is never overwritten with `failed`.
- **The TSS trusts the database row only as far as it matches.** `resolve` refuses a
  `tenant_storage` row whose role, region or names differ from the configured tenant role
  and the names derived from the tenant ID. Names are `<prefix>-t-<first 8 hex of the
  tenant ID>`; the Rekognition collection has the bucket's name and the vector bucket
  adds `-vectors`.
- **A TSS caller needs an entry in `callers`** (`TenantStorage` in `infra/__main__.py`),
  which grants `execute-api:Invoke` and fixes the service name its source identity must
  start with. The only caller so far is the isolation runner. The API and workers are
  added in step 6, when they first use storage.
- **The Cognito admin Lambda serves `invite` only, and only the provisioning role may
  invoke it** (owner decision). Three things enforce that: no other role's policy allows
  it, the function has no resource-based permission, and the `lambda` endpoint's policy
  names one function and one role. `custom:tenant_id` comes from the tenant row the task
  reads back in the `invite_admin` step. Step 5 adds the API's operations, and must then
  extend the endpoint policy as well as the API's role.
- **The provisioning role** is scoped to resources named `<prefix>-t-*` and the state
  bucket, and its own policy denies tenant object data and decrypt on tenant keys. Two
  things it keeps, both accepted by the owner on 2026-11-23:
  - `s3:ListBucket`. The AWS provider reads a bucket with `HeadBucket`, which needs it, so
    the role can list key names but read nothing. This is why no key may contain a file
    name (see the owner decisions under "Tenant isolation").
  - `kms:GenerateDataKey` on tenant keys, **only through S3 Vectors**
    (`kms:ViaService`). S3 Vectors requires it of whoever creates a vector bucket with a
    customer-managed key. Never widen that condition; `test_tss_policies.py` checks it.
  And a third, an **accepted, deliberate exception** (owner, 2026-11-23, after the second
  provisioning run failed on it; also in BUILD_SPEC.md section 6):
  - `kms:Decrypt` on tenant keys, **only through S3 Vectors**. `CreateIndex` on a vector
    bucket encrypted with a customer-managed key is refused unless the caller has it
    ("not authorized to perform: kms:Decrypt"). The tenant key's policy grants it under
    the same `kms:ViaService` condition, and the role's own deny on decrypt exempts that
    one service. It cannot reach tenant data, for three reasons:
    1. The permission applies only through S3 Vectors: the role cannot call KMS itself,
       and cannot use the key through S3, where the documents are.
    2. Its policy allows no action that reads or writes vectors, and each tenant's vector
       bucket policy denies it every one.
    3. Its policy and each tenant's bucket policy deny it every action on an object.
    The alternatives were worse: an index not encrypted with the tenant key, or letting
    some other principal with tenant data access create it. Never widen the condition.
  - **The isolation tests try all of this as the provisioning role.**
    `scripts/run_isolation.py` runs the provisioning task with `PROVISION_COMMAND=probe`
    (`assay_tss/provision/probe.py`) for the two test tenants: reading an object that
    exists, writing an object, listing object versions, reading, querying and writing
    vectors, and using the key directly must each be refused. Three settings calls and
    listing keys must succeed, so that a run where everything fails cannot pass. It runs
    as a task because nothing else can be the provisioning role; the isolation Lambda is
    not allowed to assume it.
  For the same `HeadBucket` reason an administrator can list the state bucket's object
  names (tenant IDs), which the owner also accepted.
- **Each tenant's vector bucket has its own policy** (`tenant_vector_bucket_policy`),
  admitting only the tenant role tagged for that tenant and the provisioning role, which
  is denied the vectors themselves. The break-glass role is not admitted: vectors can be
  rebuilt from the documents. AWS documents vector bucket policies as "the standard AWS
  policy format" with Deny statements, but shows no example with `Principal: "*"` or a
  `Condition`, and this policy uses both.
- **A tenant key's policy has no `kms:*` for the account root**, so no IAM policy can
  grant decrypt on it; the account gets metadata actions only. Adding a new principal that
  must use tenant keys means changing `tenant_key_policy` and re-running every tenant's
  stack.
- **A vector index cannot be changed after creation**: 1024 dimensions (`vectorDimensions`),
  cosine, `float32`, and `text` as the one non-filterable metadata key. Step 6 must store
  chunk text under `text`, or the index has to be recreated.
- **A change to the tenant stack program reaches existing tenants only when their stack is
  run again.** There is no "update every tenant" command yet; step 6 needs one for bucket
  CORS.
- **API Gateway's account-level CloudWatch Logs role** was unset; this stack now sets it
  (one per Region, shared by every API in the account).
- **Write a resource the way AWS stores it, or every preview shows a difference.** Two
  cases found after the first `pulumi up` of step 4 (2026-11-23), both with tests:
  - **A REST API's resource policy.** API Gateway replaces the `execute-api:/...` shorthand
    with the API's own ARN and turns a one-item principal list into a string. So the policy
    is an `aws.apigateway.RestApiPolicy` resource built from the API's `execution_arn`
    (`assay_infra.tss.api_resource_policy`), never the API's `policy` argument, which
    cannot name its own ARN. The deployment's redeploy fingerprint uses the policy's
    portable form, so it does not depend on the API's ARN.
  - **A bucket's encryption rule.** S3 blocks SSE-C on every new bucket and reports
    `BlockedEncryptionTypes: ["SSE-C"]` in the rule, so every rule declares it
    (`BLOCKED_ENCRYPTION_TYPES`, in the platform stack and the tenant program).
- **A preview that shows a new bucket's encryption as `AES256` becoming `aws:kms` is
  usually Pulumi's state, not the bucket.** The provider reads a setting back right after
  writing it, and S3 can still answer with the default on a bucket a few seconds old; the
  state then records `AES256` although the bucket has the KMS rule. It happened to the
  state bucket. Check the bucket first
  (`aws s3api get-bucket-encryption --bucket <name> --profile pilar-dev`). If the bucket is
  right, the owner's next `pulumi up` (or a refresh) corrects the state. The encryption
  rule is now applied after the bucket's other settings, which narrows the window but
  cannot close it.
- **`tests/isolation` runs in a Lambda in the VPC** (`isolationTests: true`), because the
  TSS and the database are private. Its role is a probe: IAM allows it to assume the
  tenant role and to read, write and tag tenant buckets and keys, and the tests show the
  resources refusing it. It has no Rekognition permission, because a face collection has
  no resource policy to refuse it. The fixture tenants "Isolation Test A" and
  "Isolation Test B" are permanent (tenant deletion is out of scope) and show in the
  Tenants list.
- **Alarms notify one SNS topic** (`assay_infra.alerts`, `pulumi stack output
  alarm_topic_arn`), which emails `alarmEmail` from stack config. Every new alarm passes
  the topic as its `alarm_actions`; `test_infra_tenancy.py` fails if one in the stack does
  not. The topic is encrypted with its own customer-managed key, because CloudWatch cannot
  publish to a topic that uses the AWS-managed SNS key. An email subscription delivers
  nothing until the owner follows the confirmation link SNS sends at `pulumi up`; an
  unconfirmed one lapses after 48 hours and must be created again.
  - **Checking whether an alarm fired and was delivered** (all read-only):
    `aws cloudwatch describe-alarm-history --alarm-name <name>` shows each state change and
    "Successfully executed action" for the topic; the `AWS/SNS` metrics
    `NumberOfNotificationsDelivered` and `NumberOfNotificationsFailed` for the topic show
    what SNS did with it. On 2026-11-23 the provisioning-failure alarm fired for both
    failed executions (about a minute after each ended) and SNS reported both delivered,
    none failed; the owner had not seen the emails, so look at the mail side first.
  - An alarm notifies on a change of state only. A second failure while the alarm is
    still in `ALARM` sends nothing; it returns to `OK` about 15 minutes after a failure.
  - The provisioning-failure alarm sums three metrics, each wrapped in `FILL(..., 0)`: a
    math expression has no value for a period in which any metric has no data point, and
    the alarm would read that as not breaching.
- **Not in step 4:** `POST /admin/tenants` and the retry route (step 5), default models
  for a new tenant (step 5, with the catalog seed), bucket CORS for browser uploads and
  `DetectFaces`/`CompareFaces` in the tenant role (steps 6 and 8), the cross-tenant face
  search test (step 8).
- **For step 6 (uploads): no S3 key may contain a file name.** Register each uploaded file
  with a `file_id`, store it at `inbox/{batch_id}/{file_id}`
  (`StorageContext.inbox_key(batch_id, file_id)`) and keep `file_name` only in the
  database. Pair companion files and stipulations with their PDF by the names in the
  database, not by listing keys. BUILD_SPEC.md section 6 has the layout.
- **For step 6 (pipeline): ignore the isolation tests' fixture object.** Each test tenant's
  bucket holds one object the isolation tests wrote, kept by Object Lock:
  `derived/text/<reserved ID>.json` (since build step 6: `assay_tss.keys.RESERVED_DOCUMENT_ID`,
  `keys.isolation_fixture_text_key()`). It is outside `inbox/`, so no upload or scan event ever
  refers to it, but it sits under `derived/text/`, which the pipeline writes. That document
  ID is reserved: it has no `documents` row and must never get one. The pipeline must work
  only from database rows and scan events, never from a listing of `derived/` or any other
  prefix, so it never treats this object as a document's text. Owner's condition
  (2026-11-23): the fixture is never in the inbox, and the pipeline ignores it. The owner
  decided to keep it at this key. (2026-11-24: the owner asked for a separate inbox fixture
  for the inbox refusals; see "Reserved fixture objects" under "Step 6, first half". This
  text fixture stays where it is.) Step 6 must also do the following:
  - **Define the reserved document ID as a constant in one place.** Today it is defined
    only in `tests/isolation/conftest.py`, which the pipeline cannot import. Move it to
    code that both the pipeline and the isolation tests import (`assay_tss.keys` is the
    natural home, next to the key builders) and have the tests use that constant. It must
    not be written out a second time anywhere.
  - **Refuse the reserved ID.** The pipeline must refuse to create a document with that ID
    and refuse to process one: no `documents` row, no object written under it, no step run
    for it.
  - **Cover both refusals with a unit test.**
- **Tagging a KMS key in the request that creates it needs `kms:TagResource` in an IAM
  policy "that isn't restricted to particular KMS keys"** (AWS KMS guide, "Control access
  to tags"). The first provisioning run (2026-11-23, tenant `cfd08356-…`) failed at the
  `storage` step on exactly this: the grant was conditioned on `aws:RequestTag` and
  `aws:ResourceTag`, and KMS refused `kms:TagResource` during `CreateKey`. Now:
  - `kms:CreateKey` is allowed only with a `tenant_id` tag in the request;
  - `kms:TagResource` is allowed on every key in the account, conditioned only on the
    request's tag keys (`aws:TagKeys`: the default tags and `tenant_id`), which is the
    form AWS documents for tagging at creation;
  - an explicit deny names every KMS key the platform stack creates, so the role cannot
    tag or untag them. **A new platform KMS key must be added to `platform_key_arns` in
    `infra/__main__.py`.** A key in this account that the stack does not create, and whose
    policy trusts IAM, is not covered: the role could add one of those three tags to it,
    which gives it no access to anything.
  After creation a tenant key's own policy is what lets the role administer and tag it.
- **`tenant_id` on a KMS key is write-once** (owner decision, 2026-11-23). The tenant
  role's policy finds a key by this tag, so once set it never changes. The provisioning
  role's policy has two explicit denies, which override the tenant key's own policy:
  - `TenantIdTagNeverChanges`: `kms:TagResource` is denied when the key has a `tenant_id`,
    the request sets `tenant_id`, and the two values differ.
  - `TenantIdTagIsNeverRemoved`: `kms:UntagResource` is denied whenever the request names
    `tenant_id`.
  Setting `tenant_id` to the value it already has is allowed (it changes nothing), and
  the role may still change `app` and `env` on a tenant key, so a change to the stack's
  default tags can be rolled out. No other tag key can be added. Every other principal is
  refused all tagging by the tenant key's own policy.
  - **Why the tag deny compares values** instead of denying any request that touches
    `tenant_id` on a tagged key: it is not known whether KMS shows a new key's tags as
    resource tags while the key is being created (the failed run is consistent with it).
    If it does, the simpler rule refuses creation; the simulator shows that in two of the
    four possible contexts. Comparing values gives the same guarantee and cannot apply
    during creation. Do not "simplify" it.
  - `test_infra_tenancy.py` evaluates the policy for creation in all four contexts and for
    each change, and `aws iam simulate-custom-policy` agreed on 24 cases.
- **Check an IAM policy's logic with the policy simulator before a deploy:**
  `aws iam simulate-custom-policy` is read-only. It evaluates the policy in IAM's own
  engine, but it cannot say which condition keys a service supplies; only the service's
  documentation or a real run can.
- **`audit_log` cannot be read from outside the VPC before step 5** (the audit page). Until
  then a failed provisioning step is read from the task's log group
  (`/aws/ecs/assay-dev-provision`), which records the same step and reason.
- **A role's trust policy needs one statement per STS action.** One `AssumeRole` call
  with session tags and a source identity is authorized as three actions (`sts:AssumeRole`,
  `sts:TagSession`, `sts:SetSourceIdentity`), each against its own request context, and the
  tags are not in the context for `sts:SetSourceIdentity`. A single statement with every
  condition on all three refused that action, so every TSS `resolve` failed
  (2026-11-23: "not authorized to perform: sts:SetSourceIdentity"). The tenant role's trust
  policy now has three statements, as AWS's own examples do: `sts:AssumeRole` requires the
  tags and the source identity, `sts:TagSession` the tags, `sts:SetSourceIdentity` the
  source identity. Same principal and same three actions as before; nothing was added.
  A refused AssumeRole now reaches the caller as `tenant_role_refused`, with STS's message
  in the TSS log.
- **S3 answers 404, not 403, for a key that does not exist, to any caller that may list
  the bucket**, whatever its right to read objects. For an object that exists it answers
  403. So a refused read is proved only on an existing object. The provisioning role may
  list (HeadBucket), and its probe at first asked for random keys and got 404; IAM's
  evaluation of the deployed role was explicit deny all along
  (`aws iam simulate-principal-policy`, read-only). The probe now lists one key and reads
  that, and the isolation tests leave one fixture object in each test tenant's bucket for
  it (the only thing they write). A 404 or an empty bucket still fails the check.
- **Every provisioning assumption is now settled** by the runs of 2026-11-23. The two that
  were wrong are fixed (key tagging at creation; `kms:Decrypt` for `CreateIndex`). The rest
  held: GuardDuty accepts the scan role's source-account condition, the malware protection
  plan needs only its own action and `iam:PassRole`, and `CreateIndex` needs only the two
  key permissions through S3 Vectors. For the record, the assumptions were:
  1. GuardDuty accepts the scan role's trust policy with the `aws:SourceAccount` condition
     (AWS's documented trust policy has no condition).
  2. `guardduty:CreateMalwareProtectionPlan` needs nothing beyond that action and
     `iam:PassRole`.
  3. `CreateIndex` needs nothing of the tenant key beyond `kms:Decrypt` and
     `kms:GenerateDataKey` through S3 Vectors.
  Settled by the first two runs (2026-11-23):
  - `s3:CreateBucket` passes the S3 gateway endpoint's policy; the Rekognition collection
    is created; the task definition's container definition round-trips with no preview
    difference.
  - `kms:CreateKey` passes with `aws:RequestTag/tenant_id` as its condition, the tags sent
    with it are accepted, and the write-once deny does not block creation.
  - A tenant key's policy, alias, bucket settings and bucket policy are all accepted, and
    `kms:GenerateDataKey` through S3 Vectors is enough to create the vector bucket.
  - S3 Vectors accepts the vector bucket policy's `Principal: "*"` and `Condition`, and
    enforces it: the account administrator is refused `GetVectorBucket`.
  - **Wrong:** S3 Vectors needs only `kms:GenerateDataKey` to create the index. It needs
    `kms:Decrypt` as well (see the provisioning role's bullet above).
  `run_isolation.py` then checks the result: a principal whose IAM policy allows it is
  still refused when it lists a tenant's vectors.

### Step 5 (portal shell, sign-in, Tenants and Users, `scripts/seed.py`)

Owner decisions (2026-11-23), D1 to D11 of the step 5 plan:

- **Auth events (D1).** One trigger Lambda on both pools (post-authentication and
  post-confirmation, `api/assay_api/cognito_auth_trigger.py`) runs in the VPC and only sends
  `{event_id, user_pool_id, sub, action, at}` to SQS through the `sqs` endpoint. It never
  raises: a failed send is logged at ERROR, which a metric filter turns into an alarm. The
  consumer (`auth_event_consumer.py`, in the VPC, as `audit_writer`) calls
  `auth_events.record()` (migration 0004): SECURITY DEFINER owned by the NOLOGIN role
  `auth_event_recorder`, `search_path = pg_catalog, pg_temp`, actions `sign_in` and
  `password_reset` only. It finds the tenant, user, email and role in `users` (never from
  the event) and uses the event ID as the audit row's ID (a redelivery is recorded once).
  An unknown user, an unknown pool or a bad message fails the message: after 5 receives it
  is in the dead-letter queue, whose depth raises an alarm. Nothing is dropped.
- **Last sign-in (D2)**: `auth_events.record()` sets `last_sign_in_at` and moves `invited` to
  `active`.
- **Change password (D3)** goes from the browser straight to Cognito; the portal then calls
  `POST /api/auth/password-changed`. **MFA setup (D4)** is a portal screen
  (`AssociateSoftwareToken`, `VerifySoftwareToken`, `SetUserMFAPreference`): AWS documents
  that managed login "doesn't prompt users to set up MFA" when MFA is optional (the tenant
  pool), and prompts for it when MFA is required (the platform pool).
- **Sessions revoked (D5).** `users.sessions_revoked_at` is set by an MFA reset and by
  disabling a user; the account check refuses a token whose `auth_time` is not later
  (`session_revoked`). Global sign-out alone revokes only refresh tokens.
- **MFA reset** is `AdminDeleteSoftwareToken` plus `AdminUserGlobalSignOut`, not
  `AdminSetUserMFAPreference` (which "doesn't reset an existing TOTP MFA"). With the device
  gone, the platform pool returns `MFA_SETUP` at the next sign-in; a tenant-admin sees the
  portal's MFA screen. Check both in a real run.
- **D8.** `platform_view_owner`'s policy on `users` is `true`; it stays NOLOGIN with
  column-level SELECT only, and every view filters for itself. `test_db_step5.py` checks
  that no view in schema `platform` shows a business tenant's non-admin user.
- **D9.** The catalog is loaded as `migrator` through the migration Lambda
  (`seed_models`); no application role writes `ai_models`. New tenants get the defaults
  (stack config `defaultIngestModel`, `defaultQueryModel`) in `activate`, in the same
  transaction. **The `aiModels` prices in `Pulumi.dev.yaml` are blank on purpose: the owner
  fills them in; `seed.py` refuses to load the catalog until then.**

Security conditions (owner, 2026-11-23):

1. **Client write attributes.** Both portal app clients may write `name` only
   (`CLIENT_WRITE_ATTRIBUTES`); checked by `test_infra_auth_api.py` and, against Cognito, by
   `tests/isolation/cognito_clients.py` (run by `run_isolation.py` on the owner's machine,
   because Cognito cannot be reached from the VPC). The portal clients allow no
   programmatic sign-in and a sign-in on the real tenant pool would fire the audit trigger
   for an unknown user, so the attempt runs on a throwaway replica pool with the tenant
   pool's immutable `custom:tenant_id` and a client with the deployed client's attributes.
   It is created and deleted by the run.
2. **`cognito-admin-api` checks its target.** Every tenant-pool operation needs a
   `tenant_id`; the Lambda reads the user and refuses (`user_not_found`) unless its
   `custom:tenant_id` and `sub` match. Platform pool: `reset_mfa` of a `master-user` only.
   It is a second function on the step 4 image, with `OPERATIONS` limiting what each
   function serves (`invite` only on the provisioning role's).
3. **ALB ingress** comes from `CloudFront-VPCOrigins-Service-SG`, looked up after the VPC
   origin exists (so a preview shows the rule's source as unknown). If CloudFront has not
   created the group yet when `pulumi up` gets there, the lookup fails; run `pulumi up`
   again.
4. **SQS endpoint.** It existed since step 2 with the default allow-all policy. It now allows
   only the trigger's role to send to the auth-events queue. **Step 6's pipeline queues must
   be added to `sqs_endpoint_policy`.**
5. **Portal.** Tokens in sessionStorage only (oidc-client-ts `userStore` and `stateStore`;
   `auth.test.ts` checks localStorage stays empty). Exact versions, resolved with
   `npm install --before=<two weeks ago>` in the pinned Node container, `npm ci
   --ignore-scripts` everywhere. HSTS without preload.

Notes:

- **Yield dependencies need `scope="function"`.** FastAPI's default runs a yield
  dependency's exit after the response is sent, so a write could be reported and then fail to
  commit. Every dependency in `assay_api.security` sets it.
- **Endpoint policies are never moved or recreated**: deleting a `VpcEndpointPolicy`
  resets the endpoint to allow everything. The `lambda` endpoint's policy keeps its step 4
  name and parent (`Provisioning.lambda_endpoint_access`).
- **A security group's description cannot change** without replacing the group (the first
  step 5 preview replaced the ALB's). Keep descriptions as they are.
- **Portal build.** `scripts/build_portal.py` (a Pulumi command, keyed on a hash of
  `portal/` and `assets/`) builds `portal/Dockerfile` (type check, tests, `vite build`)
  and uploads the files; `config.json` is written by Pulumi. After changing a route, run
  `uv run scripts/export_openapi.py` and `npm run gen:api`; `test_openapi.py` and CI
  check both.
- **Rendered checks (portal/e2e).** Component tests run in jsdom, which has no layout. Three
  defects passed them and were seen only in a browser (owner, 2026-11-24, at 1800 px in
  Chrome): the banner's product name cut to "Assay Insi..." with a mark too small to read, a
  row-action menu clipped by its table's scrolling container, and a 280 px navigation for
  three short items. So the production build is now rendered in headless Chromium at 1280
  and 1800 px (`portal/e2e/layout.e2e.ts`, Playwright 1.64.1 in its digest-pinned image),
  for every screen of both hosts, signed in with a stored oidc-client-ts user and a stand-in
  API (`e2e/fixtures.ts`; nothing reaches AWS). On every screen it checks the banner (full
  name, no ellipsis; a 32 px logo whose drawing stands out from the banner), the navigation
  (no item cut off or under the collapse button, at most 90 px wider than its widest item),
  and every dropdown (opened, first and last items hit-tested where they are drawn, so a
  clipping container fails it). Each failed on the old build. CI job `portal-render` and
  `uv run scripts/check_portal_render.py` run it the same way; `test_portal_render.py`
  keeps the image and `@playwright/test` on one version. Not covered: the CloudFront CSP
  (`vite preview` sends none) and real tokens.
  - **The banner has its own mark**, `assets/assay-insights-mark-banner.svg`: the favicon's
    navy tile is the banner's colour (`#14213D`) and vanished into it, leaving a small glyph,
    so the banner version is the artwork alone, cropped to the logo height. It declares
    `width` and `height`; Cloudscape sizes the identity from the image's intrinsic width,
    and without one it cut the name. The favicon mark is unchanged, because Cognito's
    managed login branding uses it.
  - **Every table row-action menu is a `RowActions`** (`portal/src/components/RowActions.tsx`),
    a ButtonDropdown with `expandToViewport`, so the menu is drawn against the window and
    not inside the table's scrolling container. Never use a bare ButtonDropdown in a table.
  - **The navigation's width comes from its items** (`navigationWidth` in
    `portal/src/navigation.ts`: the longest title at 7.5 px a character plus 90 px, between
    150 and 280 px). Tenant portal 150 px, admin portal 188 px. New screens in steps 6 to 8
    widen it as needed; the rendered check catches a title that no longer fits.
  - **Every table fits its container** at 1280 and 1800 px (`checkTables`: no column starts
    or ends off screen, nothing scrolls sideways). Headless Chromium hides scrollbars, so a
    screenshot never shows the scrollbar a browser would; the check measures instead.
  - **Audit history fits at 1280 px** (owner, 2026-11-24): lines wrap; Time, Actor and Outcome
    stay on one line (checked); Target wraps anywhere, with a 120 px minimum; action names
    may break after an underscore (`<wbr>`, so a copy is still the exact name). The limit is
    the Actor address, kept on one line: about 245 px of text, roughly 33 characters, fits at
    1280 px. A longer address still shows in full, and the table then scrolls sideways at
    that width. The rendered check's longest actor is 33 characters (`e2e/fixtures.ts`).
    Outcome cannot go below about 104 px (its header and "success"); pinning it narrower made
    the word break into "succe / ss", which the one-line check now catches.
- **TypeScript stays on 5.9**: `typescript-eslint` and `openapi-typescript` do not support
  TypeScript 7 yet.
- **Dev-only npm advisory** (2026-11-23): a moderate ReDoS in `minimatch`, via ESLint and
  `typescript-eslint`. The fixed releases (ESLint 10.15.0, `typescript-eslint` 8.74.0) are
  younger than two weeks; bump them after 2026-12-07. CI audits the shipped dependencies
  (`npm audit --omit=dev`), which are clean.
- **API errors are short codes** (`detail`); `portal/src/errors.ts` turns them into text.
- **Not tested in AWS yet:** everything in this step. In particular CloudFront to the ALB over
  HTTPS (the certificate checked against the forwarded Host header), the Authorization
  header reaching the API, the triggers on both pools, the MFA reset, and whether CloudTrail
  shows `ChangePassword`.

Second half (Audit history, Models), owner decisions and conditions (2026-11-23):

- **`GET /admin/audit`** (both master roles) is strictly read-only: it reads in the account
  check's read-only `platform_reader` transaction. At most 50 rows per page, enforced by the
  route (`page_size` above 50 is refused with 422). Every filter is a bound parameter (the
  actor filter is a case-insensitive contains-match with `%` and `_` escaped), and the sort
  comes from a fixed allow-list (`assay_api.audit_read.SORTS`). Last 7 days unless a range is
  given; at most 400 days in one query.
- **CSV export** (`format=csv`): at most 10,000 rows; cells a spreadsheet would read as a
  formula are prefixed with `'`. Each export writes its own row, category `audit`, action
  `exported` (migration 0005 adds the category), with the actor, the filters and the row
  count, as `platform_writer` in a transaction of its own.
- **Before and after values are redacted** (`assay_api.audit_read.redact`): any key that
  names a password, token, secret, code, credential, authorization or session, in any case
  or separator style and at any depth, is shown as `[redacted]`. Nothing secret is written to
  `audit_log`; this makes sure nothing secret is shown either. `test_api_audit_models.py`
  plants secrets and checks neither the row panel nor the CSV carries them, and checks every
  row the tests wrote against the same rule.
- **Audit rows are stamped with `clock_timestamp()`**, not `now()`: two rows of one
  transaction (a model removed and the selection moved) would otherwise share a time and
  show in no fixed order.
- **Models.** `platform.ai_models_v` and `platform.tenant_models_v` (migration 0005) give
  `platform_reader` the catalog and each tenant's models; it still has no table but
  `tenants` and `audit_log`. A master admin's change runs as `platform_writer`, keeps at
  least one model per job, and moves a removed selection to the first remaining model in the
  same transaction (`models_available_changed`, then `model_selected`). A tenant admin's
  selection runs as `app_tenant`, so only their tenant's list exists for them
  (`model_not_available` otherwise).
- **The role matrix** (`test_api_audit_models.py::test_role_matrix`) covers every route of
  build step 5 for every role, and checks that a refused call writes nothing.

Audit-trail follow-ups (owner decisions and conditions, 2026-11-24):

- **What Cognito does, from CloudTrail and the trigger logs** (2026-11-24 UTC, read-only):
  - A tenant-pool first sign-in runs post-authentication **twice**: when the temporary
    password is accepted (with the one `login_POST`), and when the new password is set,
    0 to 3 seconds before the pre-token trigger's `TokenGeneration_NewPasswordChallenge`.
    Six of six first sign-ins did this; later sign-ins run it once.
  - The platform pool's first sign-in (one sample) ran it **once**, at the temporary
    password, and not after MFA setup finished in managed login.
  - A sign-in served from managed login's session cookie (no password, an authorization-code
    exchange) runs **no** post-authentication trigger, so it has no `sign_in` row. Seen at
    04:06:54: a tenant user who had not signed out since their password sign-in at 03:45:37.
  - Sign-out works on both pools. Every one of the 17 sign-ins after a portal sign-out on
    the same pool (14 tenant, 3 platform) asked for the password. The portal's sign-out
    revokes its own refresh token and sends the browser to Cognito's `/logout`, which ends
    the managed login session. Before the cross-tab sign-out below it did not end another
    open tab's session: at 04:45:07 such a tab refreshed its token (`Token_POST` with a
    refresh token) after a sign-out in another tab.
- **Sign-out reaches the portal's other tabs in the same browser** (owner decision,
  2026-11-24; not `GlobalSignOut`, which would end every device's sessions). The tab that
  signs out records `sign_out`, posts `{type: "signed-out"}` on the BroadcastChannel
  `assay-portal-sign-out` (`portal/src/signout.ts`), revokes its refresh token and goes to
  Cognito's `/logout`. Every other open tab of the same portal (same origin, so the tenant
  and admin portals never hear each other) revokes its own refresh token, clears its tokens
  and shows the sign-in screen ("You signed out in another tab."), with no second audit row.
  Other browsers and devices are unaffected. No storage-event fallback: that event crosses
  tabs only for localStorage, which the portal never uses, and BroadcastChannel is in every
  browser the build targets; without it a tab signs out alone. Tested in `Root.test.tsx`
  and `signout.test.ts`.
- **`temporary_password_accepted`** (item 2). The trigger sends it instead of `sign_in` when
  the event's `cognito:user_status` is `FORCE_CHANGE_PASSWORD`, and logs the status it saw
  (`auth event queued`, `user_status`), never queues it. Any other status, or none, stays
  `sign_in`. Migration `0006` replaces the body of `auth_events.record()` to accept it.
  Migration `0007` limits its effect on `users` to the platform pool (whose first sign-in
  sends nothing else); in the tenant pool only `sign_in` makes a user Active (see "Known
  limits"). Both switch to `auth_event_recorder` for the `CREATE OR REPLACE`, because
  `migrator` holds SET but not INHERIT on that role and so cannot replace a function it does
  not own; owner, grants and search_path are unchanged, and
  `test_migration_0007_changes_no_owner_grant_or_policy` compares the function's owner and
  ACL, every policy, every table and column grant and every role membership at 0006, at 0007
  and after the downgrade.
  - **Deploy order:** run `migrate.py` straight after `pulumi up`. Until `0006` is in, a
    first sign-in's `temporary_password_accepted` is refused by the old function, retried,
    and after 5 receives dead-lettered (and alarmed).
  - **Not yet verified:** that the first run carries `FORCE_CHANGE_PASSWORD` and the second
    `CONFIRMED`. Check after deploy with a new test user's first sign-in on the tenant pool
    (owner's condition e): the trigger's two `auth event queued` lines and the trail.
- **`session_started`** (item 1), `POST /api/auth/session-started`, called by the portal once,
  right after a sign-in redirect (not on a reload or a token refresh). IP from
  `CloudFront-Viewer-Address`, browser from `User-Agent`, `detail.reported_by: "portal"`.
  - **One row per sign-in** (condition c): the row's ID is a UUIDv5 of the user and the
    token's `auth_time`, and its time is `auth_time`. A repeat conflicts on the primary key
    `(id, at)` and `ON CONFLICT DO NOTHING` drops it, which needs INSERT only
    (`audit.write(row_id=..., at=...)`). Tested as `app_tenant` and `platform_writer`.
  - **The window is `auth_time`, not `iat`** (condition b): refused (`session_not_new`, 409)
    unless `auth_time` is within 5 minutes before now (1 minute of clock leeway after). A
    token refresh changes `iat` but not `auth_time`, so `iat` would let a session report its
    start long after the sign-in, from wherever the user is by then.
  - **Not yet known:** what `auth_time` a sign-in from the session cookie carries. AWS
    documents it only as when the user "completed authentication". If it is the cookie
    sign-in's own time, that sign-in gets its own row; if it is the original password
    sign-in's, it is the same sign-in and adds nothing (and is refused after 5 minutes).
    Either way the row's time is the authentication it describes. Check after deploy: sign
    in, close the tab without signing out, sign in again within the hour, and read the two
    rows in Audit history.
  - **The IP cannot be supplied by a viewer** (condition d). The `/api/*` behaviour uses
    `Managed-AllViewerAndCloudFrontHeaders-2022-06` (`allViewerAndWhitelistCloudFront`, with
    `CloudFront-Viewer-Address`), and AWS documents that when a policy adds such a header,
    "CloudFront overwrites the header values that were in the viewer request" (CloudFront
    developer guide, Lambda@Edge restrictions). The load balancer is internal, and its only
    inbound rule is 443 from `CloudFront-VPCOrigins-Service-SG` (checked 2026-11-24). The
    browser string is the viewer's own claim, as on every row.
- **`mfa_enabled`** (item 3, variant b), `POST /api/auth/mfa-enabled`, tenant pool only
  (`tenant_pool_only` otherwise). The MFA screen calls it after Cognito accepts the code. The
  API asks `cognito-admin-api` for `mfa_status` (new, read-only: `confirm_target`'s
  `AdminGetUser`, which also checks tenant and `sub`), and writes the row only if TOTP is on
  (`mfa_not_enabled`, 409, otherwise). No IAM change: the function's role already had
  `AdminGetUser`; the API may now ask it for one more operation, which changes nothing in
  Cognito.

### Step 6, first half (batches, uploads, scan routing, fingerprint, ingestion history)

Owner decisions on the plan (2026-11-24), by number:

- **1, 2.** Titan Text Embeddings V2 by model ID in us-east-1, a named exception to section 2
  (it has no `us.` inference profile: `aws bedrock list-inference-profiles`, 2026-11-24). It
  goes into `ai_models` with job `embed`, price left blank for the owner (second half).
  **The three Claude profiles were test-called by `scripts/seed.py` on 2026-11-23** (section
  7b's "verify each profile with a test call before seeding"), successfully.
- **3.** A GuardDuty scan event is a pipeline job message for rule 4: the tenant comes from
  the database (`pipeline.tenant_for_upload`, a registered upload's key), and the event's
  bucket must be that tenant's bucket (`naming.bucket_name`), or the event is refused.
- **4 (changed on the owner's later answer), e.** The API never reads `inbox/`. The scan
  Lambda (a PIPELINE caller) validates each companion file once its scan is clean: size
  checked first, at most 64 KB read, strict JSON with a depth limit, never logged, errors by
  key only. Close reads only the database and waits for every file's scan result.
- **5.** A batch open 24 hours is closed by the sweeper anyway: files never uploaded and
  companions with no PDF are dropped (`removed`), a PDF whose companion is invalid fails
  (`companion_invalid`), files still waiting for a scan wait on in the closed batch.
- **6, 7.** New table `batch_files`; extra routes `GET /batches` (status and mine filters; any
  tenant member, for the history's batch filter) and `DELETE /batches/{id}/files/{file_id}`.
- **8.** Deferred: the worker is not built (see "Carried forward to step 6").
- **9.** Each tenant's malware protection plan covers `inbox/` only (`object_prefixes`).
- **10, 11.** Known limits: inbox objects stay; the CSP admits every S3 bucket in the Region.
- **12, a.** The upload link signs `Content-Length`, `Content-Type` and
  `x-amz-checksum-sha256` (botocore puts all three in `X-Amz-SignedHeaders`; checked offline).
  The scan step checks the stored version's size and SHA-256 against the registration again.
- **13.** The fingerprint step holds a password-protected PDF (`on_hold`, `pdf_encrypted`;
  PDFium's `FPDF_ERR_PASSWORD`), whatever GuardDuty said.
- **14, g.** A `failed` document is not an original; a duplicate is within one tenant only
  (advisory lock on tenant and hash, an explicit tenant condition, row-level security, and a
  partial unique index per tenant).
- **15.** Status `discarded` (a discarded batch's documents, a removed file's), left out of
  both history views unless the status filter asks for it. A quarantined document keeps its
  status when its batch is discarded.
- **16.** Each TSS caller gets only its accesses (`TSS_CALLERS`: role name -> service and
  accesses; `api`: read and upload; `scan`, `pipeline`, `sweeper`: pipeline). No session may
  list a bucket.
- **17.** No database CHECK for the reserved ID (the literal would be written a second time).
- **18.** Textract and Bedrock will be called with the worker's own role and bytes (second
  half); Rekognition only with tenant credentials (step 8).
- **19.** A suspended tenant's documents get no new execution: the starter drops the job and
  the document stays eligible; the sweeper queues it again once the tenant is active (within
  about 20 minutes). Executions already running finish.

Conditions, and where each is met:

- **a.** Limits: 100 MB a PDF, 64 KB a companion file, 200 PDFs and 200 companion files a
  batch, 50 files a registration request, names up to 255 characters, `.pdf` and `.json`
  only (`assay_db.batches`). Signed headers as in 12. A stored size or hash that differs fails
  the file (`mismatch`, `size_mismatch` or `checksum_mismatch`) and its PDF.
- **b.** The first decisive scan result pins `batch_files.version_id`; fingerprint downloads,
  hashes and copies that version only. A later version's event is logged and ignored
  (`later_version_ignored`). Tested: `test_only_the_version_that_scanned_clean_is_ever_used`.
- **c.** READ sessions read `originals/`, `derived/` and `chat/` only; UPLOAD writes `inbox/`
  only; only PIPELINE reads `inbox/` and `quarantine/`. Isolation tests: a quarantine fixture
  (`keys.isolation_fixture_quarantine_key()`) read current and by version; the inbox by S3's
  AccessDenied reason (see the notes). `pdf-url` refuses anything whose key is not the
  context's `original_key`.
- **d.** A registered file with no scan result 30 minutes after registration: `scan_timeout`
  and `on_hold` if it is in the bucket (HEAD by key), with an ERROR line (alarm); otherwise
  `not_uploaded`. An event for an unregistered inbox key: ERROR line (alarm), ignored.
- **f.** `GET /documents/{id}/pdf-url`: a `viewed` event with the user's ID and email, the IP
  address (`CloudFront-Viewer-Address`) and the browser; five minutes; `response-content-type`
  `application/pdf`, `inline`. `platform.ingestion_events_v` leaves `viewed` events out.
- **h.** `api_task_role()` creates the role outside `ApiService` with an alias to its old
  parent. The preview (2026-11-24) shows no change to it at all.
- **i.** Titan's price stays blank (second half).

How it works (first half):

- `POST /batches/{id}/files` -> a `batch_files` row per file, a `documents` row per PDF (its ID
  is the file's ID), and a presigned PUT to `inbox/{batch_id}/{file_id}`, signed with an
  UPLOAD context. GuardDuty scans it -> EventBridge -> the scan Lambda records the result.
  Close (or a scan result for a closed batch) makes clean PDFs `processing` and sends one job
  each to the ingest queue after the commit. The starter starts one execution per document,
  named after it. Fingerprint (step 4) hashes, checks duplicates, holds encrypted PDFs and
  copies the PDF and its companion to `originals/`. The execution then ends; documents stay
  `processing` until the second half adds steps 5 to 10.
- Statuses: `uploaded` (batch open), `scanning` (closed, a scan still due), `processing`
  (queued or running), `quarantined`, `on_hold`, `failed`, `duplicate`, `discarded`.
- Database: migration `0008` (see its docstring). The two `pipeline` functions belong to the
  new NOLOGIN role `pipeline_lookup`, which the bootstrap creates; they answer with tenant
  IDs only and read no file name and no `tenant_storage`.
- Shared logic is in `assay_db.batches` (the API and the pipeline both use it); the
  companion validator in `assay_schemas.companion`; the pipeline in `workers/assay_workers`
  (one Lambda image, `workers/lambda.Dockerfile`, four handlers); the infrastructure in
  `infra/assay_infra/pipeline.py`.

Deploy order (owner): `pulumi up`; `uv run scripts/migrate.py` (bootstrap creates
`pipeline_lookup`, then 0008); `uv run scripts/update_tenants.py` (CORS, malware plan
prefix: needed before any browser upload); `uv run scripts/run_isolation.py ...`; browser
checks. After the copy and error-code fixes: `pulumi up`, then `uv run scripts/migrate.py`
(0009) straight away. Until 0009 is in, the admin route's query names a column the view
does not have, so the master view's step panel fails (the tenant view is unaffected).

Notes:

- **Verified by the first browser upload** (Infinity Loans, 2026-11-24 21:32 UTC): the
  presigned PUT with the signed `x-amz-checksum-sha256` is accepted under Object Lock default
  retention, and the scan event maps to the registered upload.
- **Since verified in AWS** (2026-11-24): S3 cites `s3:GetObject` and `s3:GetObjectVersion`
  for a session-policy refusal of an object that exists (the inbox fixture, isolation run
  69 of 69), and a copy with `TaggingDirective` `REPLACE` and no tags passes with the tenant
  role's permissions (fingerprint, next bullet).
- **Not verified directly:** that the scan event's `versionId` names the version scanned
  (AWS's example has it; it does not say so in words). The scan Lambda does not log the
  version ID. For the EICAR upload, the GuardDuty finding's `VersionId` was the inbox
  version GuardDuty scanned, and its hash was EICAR's.
- **Pipeline copies take no tags** (owner-approved fix, 2026-11-24). The first browser
  upload failed at fingerprint: `CopyObject` from `inbox/` to `originals/` was refused
  (`AccessDenied`, generic message) on all three attempts. GuardDuty tags every object it
  scans (the plan's tagging is on), and `CopyObject` copies the source's tags by default.
  That needs `s3:PutObjectTagging` (a `CopyObject` dependent action in the Service
  Authorization Reference) and reading the source's tags. The policy simulator
  (2026-11-24) shows `s3:GetObjectVersionTagging` and `s3:PutObjectTagging` as
  `implicitDeny` in the deployed tenant role's policy and in the PIPELINE session policy, and
  `explicitDeny` in the tenant bucket policy (as the program writes it), while
  `GetObjectVersion`, `PutObject` and `GetObjectTagging` are allowed in all three. Every
  copy (`runtime.S3Objects.copy`: fingerprint's originals and the scan step's quarantine)
  now sends `TaggingDirective=REPLACE` with no tags. No permission changed. **If S3 still
  refuses, stop:** the alternative is a widening the owner must approve first (both actions
  for the tenant role, in its policy, the PIPELINE session policy and every tenant's bucket
  policy).
- **A failed step names itself** (owner decision, 2026-11-24). Each step's catch goes through
  a Pass state that sets `failed_step` to that step's name (fixed text in the definition);
  `Failed` receives only that and the error's name (`$.error.Error`), never its `Cause`. The
  document's code is `{step}_failed` (`steps.PIPELINE_STEPS`; anything else is
  `pipeline_error`), which is all a tenant user sees ("The fingerprint step failed."). The
  `failed` event also keeps `error_type`, the error's name, if it matches
  `^[A-Za-z0-9_.]{1,64}$`. Master admins see the code and the error type: migration `0009`
  adds `error_type` to `platform.ingestion_events_v` (a read widening for
  `platform_reader`), and the view shows it only if it matches the same pattern. The
  tenant's document detail reads only `detail.error`. **Second half: add every new step to
  `PIPELINE_STEPS` and give it its own Pass state** (`test_infra_pipeline.py` checks every
  catch goes through one).
- **Who opened a PDF is shown to tenant admins only** (owner, 2026-11-24): `GET
  /documents/{id}` returns each `viewed` event's `viewer` only when the caller is a tenant
  admin; a tenant user sees "Opened" with no name. The steps panel shows plain words for
  every result (`RESULT_TEXT`: `continue` is "Passed", `pdf_url` is "Opened").
- **Every ingestion CSV export is audited** (owner decision, 2026-11-24), like an Audit
  history export: category `audit`, action `exported`, target type `ingestion_history`
  (tenant view, written as `app_tenant` in the request's transaction, which commits before
  the file is sent) or `ingestion_history_all_tenants` (master view, as `platform_writer` in
  a transaction of its own). Each row has the actor, role, tenant, filters, sort, row count,
  IP address and browser, and no file name. No permission changed: `app_tenant` and
  `platform_writer` already had INSERT on `audit_log`.
- **On-hold wording** (owner, 2026-11-24): every on-hold code says why and what to do
  (`ERROR_TEXT` in `portal/src/pages/ingestionFormat.ts`; the Upload screen shows it under the
  file's status). GuardDuty reports a password-protected PDF only as `UNSUPPORTED`, with no
  reason, so `scan_unsupported` says "most likely because it is password-protected".
  `pdf_encrypted`, from PDFium at fingerprint, says it for certain.
- **Master admins always see the stored code**, also on rows from before step-level codes:
  `pipeline_error` with no error type reads "pipeline_error · the failed step was not
  recorded".
- **A failed document cannot be re-run; upload the file again.** Nothing restarts a `failed`
  document (the sweeper requeues only `processing` documents with no execution), and Step
  Functions does not reuse a Standard execution's name, which is the document ID, for 90
  days. A re-upload is a new document and an original: the duplicate check and the
  per-tenant unique index both skip `failed`. Reprocessing (for extraction changes) is to be
  proposed in the second-half plan.
- **S3 refuses a missing key for `s3:ListBucket`** (first isolation run after the step 6
  deploy, 2026-11-24). Its AccessDenied message to a READ or UPLOAD session reading a key
  that was never written was "not authorized to perform: s3:ListBucket ... because no
  session policy allows the s3:ListBucket action", not the read. So the reason shows whether
  the object was there. The first version of `test_only_pipeline_sessions_read_the_inbox`
  read a random, missing key, proved nothing, and failed; it now reads the inbox fixture.
- **Reserved fixture objects** (keys in `assay_tss.keys`, all under `RESERVED_DOCUMENT_ID`,
  written once by `tests/isolation` with a PIPELINE session and kept by Object Lock):
  - `derived/text/{id}.json` (build step 4), in both test tenants' buckets;
  - `quarantine/{id}.pdf` (build step 6), Isolation Test A;
  - `inbox/{id}/{id}` (owner's request, 2026-11-24), Isolation Test A, **two versions**. The
    current one's body names the noncurrent one's version ID, because no tenant session may
    list versions. It shows that READ and UPLOAD sessions are refused both versions, with
    S3 citing `s3:GetObject` and `s3:GetObjectVersion`.
  - **The inbox fixture raises no alarm, and real uploads lose none.** GuardDuty scans its
    two writes (first run only). The scan Lambda ignores results for exactly
    `keys.isolation_fixture_inbox_key()` at INFO. No upload can have that key: `inbox_key`
    refuses the reserved ID, and no `batch_files` row has it. Any other inbox key that is
    not a registered upload still logs at ERROR. That now includes keys under `inbox/` that
    are not upload keys at all (the reserved ID in another position, a file name), which
    were INFO "outside the inbox" before. Tested in
    `test_only_the_isolation_inbox_fixture_is_ignored_without_an_alarm`.
  - None of them ever gets a `documents` or `batch_files` row, and the pipeline works only
    from rows and scan events, so none is ever processed.
- **GuardDuty's event** (user guide, read 2026-11-24): `detail.s3ObjectDetails.bucketName`,
  `objectKey`, `versionId`; `detail.scanResultDetails.scanResultStatus` is
  `NO_THREATS_FOUND`, `THREATS_FOUND`, `UNSUPPORTED`, `ACCESS_DENIED` or `FAILED`.
  `ACCESS_DENIED` with `statusReasons` `OBJECT_E_TAG_CHANGED` means a newer version will be
  scanned, so the scan Lambda waits for that one.
- **A missing key answers 403** to every tenant session now (none may list), so `head()` in
  the pipeline treats 403 and 404 alike as "not there".
- **Portal:** Upload, Ingestion history (tenant), Ingestion history (all tenants). The
  navigation is 218 px on both portals. At 1280 px the ten sortable columns did not fit
  legibly, so the histories use Cloudscape's full-page table, compact times (no year when it
  is this year's; full time in the row's panel), short IDs in the master view (first 8
  characters; full in the panel), and column preferences: Size and Duration are hidden by
  default on a window narrower than 1600 px, kept in the row's panel, and every column can be
  shown (kept per browser in localStorage). Audit history's Time column drops the year when
  it is this year's, so the table still fits at 1280 px beside the wider navigation.
- **Tenant ID in the admin portal** (owner, 2026-11-24). A tenant's detail shows its full
  tenant ID with a copy button. The Tenants table has an optional Tenant ID column, hidden by
  default (Preferences): it shows the first 8 characters, as the master ingestion view does,
  and its copy button copies the full ID. The full ID plus the button made the table 175 px
  too wide at 1280 px. Master roles already saw tenant IDs; nothing new is exposed.
- **Deployed names are not Pulumi resource names.** Functions are `assay-dev-pipeline-scan`,
  `-starter`, `-steps` and `-sweeper`, each logging to `/aws/lambda/<function>`. The Pulumi
  resource is `<function>-fn`, and that name exists nowhere in AWS. The state machine is
  `assay-dev-pipeline` (log group `/aws/vendedlogs/states/assay-dev-pipeline`). The queues
  are `assay-dev-pipeline-ingest`, `-ingest-dlq` and `-scan-dlq`. List them with
  `aws lambda list-functions` before writing a command (the owner caught a `-fn` name in a
  throttle command, 2026-11-24).
- **Which pipeline alarm fires when.** Each function has two:
  - `<function>-errors` (`assay-dev-pipeline-<function>-errors`): any `ERROR` log line
    (metric filter).
  - `<function>-failed`: the `AWS/Lambda` `Errors` metric, an invocation that ran and raised.
  - The state machine adds `assay-dev-pipeline-executionsfailed` and
    `-executionstimedout`.
  - **Every failed document fires `steps-errors` and `executionsfailed`**: the `failed` step
    logs "document failed" at `ERROR`, and the execution ends in `FailedEnd`.
  - **`steps-failed` fires only if the function itself raised.** In the first upload's
    `CopyObject` refusal it did (16:33 Eastern, 2026-11-24). In the throttle test
    (22:06 Eastern) it did not: with reserved concurrency 0, Lambda refuses the invoke
    before the function runs, which counts in `Throttles`, not `Errors`. That alarm stayed
    `OK`.
  - A handled outcome (`not_a_pdf`, duplicate, `pdf_encrypted`) fires nothing.
  - A quarantine fires nothing in this stack; see the second-half quarantine notice.
- **Step failure check** (owner decision D, run 2026-11-24). On a quiet system:
  1. Throttle the steps function:
     `aws lambda put-function-concurrency --function-name assay-dev-pipeline-steps --reserved-concurrent-executions 0 --profile pilar-dev`
  2. Upload a new PDF and close its batch.
  3. Wait for the `FingerprintFailed` Pass state (about 75 seconds of Lambda retries) in
     `aws stepfunctions get-execution-history --execution-arn arn:aws:states:us-east-1:<account>:execution:assay-dev-pipeline:<document id> --profile pilar-dev --query "events[?type=='PassStateEntered'].stateEnteredEventDetails.name" --output text`.
  4. Remove the throttle:
     `aws lambda delete-function-concurrency --function-name assay-dev-pipeline-steps --profile pilar-dev`.
  5. Confirm it is gone: `aws lambda get-function-concurrency --function-name assay-dev-pipeline-steps --profile pilar-dev`
     prints nothing.
  - The `Failed` state's own invoke retries until the throttle is gone. So remove the throttle
    within about 75 seconds of `FingerprintFailed`, or that invoke fails too and the document
    stays `processing` with no `failed` event.
- **Quarantine evidence** (EICAR document `21b2e1ee-…`, read-only, 2026-11-25 UTC):
  - **Infected version:** the GuardDuty finding names
    `inbox/0c490d9d-…/21b2e1ee-…`, version `02cnIxi9XNq7ognqYcV4PgFNzFGeEHFe`, with EICAR's
    SHA-256.
  - **Scan result:** the scan Lambda logged `scan result recorded`, `THREATS_FOUND`,
    `infected`, `threats_found` at 02:52:21, two seconds after the finding. It logged no
    `ERROR` line; `scan-errors` and `scan-failed` never left `OK`; the scan dead-letter
    queue is empty.
  - **Copy and delete marker:** `scan.quarantine` copies the scanned version to
    `quarantine/{document id}.pdf` (`ctx.quarantine_key`) and then deletes the inbox key
    without a version (a delete marker; the role cannot delete a version). It does both
    before the database write that line reports, and either one failing raises. So the line
    shows both returned success. Nothing records the object, its version or the marker
    directly:
    - the organization trail has no S3 data events;
    - the tenant bucket's policy refuses the administrator even `GetBucketPolicy`;
    - the scan Lambda does not log the key or version.
    Only the break-glass role could list the versions, and that raises its alarm.
  - **View link:** `pdf-url` refuses a `quarantined` document (`NOT_VIEWABLE`;
    `pdf_not_available`, 409) before it asks for a storage context. The API log shows two
    `GET /api/documents/21b2e1ee-…` requests and no `pdf-url` request for it, so the refusal
    was never exercised in AWS. `ingest_check.py eicar` now asks for the link and fails
    unless it is refused.
- **Browser-check files:** `uv run scripts/make_test_files.py` writes synthetic ones to
  `.test-files/` (git-ignored), including `06-not-a-pdf.pdf` (plain text: `failed`,
  `not_a_pdf`). **The EICAR test file is never written to disk or kept in the repository.**
  The owner's machine runs SentinelOne (Defender is off), which removes it on write, and the
  owner adds no exclusions. `scripts/ingest_check.py eicar` assembles it in memory from a
  masked form (each byte plus one, as hex) and uploads it through the normal API and
  presigned link. `test_ingest_check.py` checks the masked form hashes to EICAR's published
  SHA-256 one byte at a time, and that no tracked or untracked file holds the string. Run the
  `eicar` case in AWS CloudShell, where SentinelOne is not involved. A quarantine raises
  GuardDuty's own `Object:S3/MaliciousFile` finding (organization GuardDuty and Security
  Hub); the pipeline raises no alarm for it.
- **`scripts/ingest_check.py`** (owner decisions A3 and F, 2026-11-24) signs in with the
  access token of the owner's portal tab, pasted at a hidden prompt (never echoed, logged,
  stored or on a command line), refuses any tenant but `--tenant`, prints the tenant's name
  and asks before creating anything. It calls only the portal's routes, through CloudFront,
  so production code has no special case for it. Standard library only and Python 3.9+, so
  it runs in CloudShell as well.
- **Companion files are never left out for their content** (owner decisions A1 and A2,
  2026-11-24). The portal uploads every companion of the right size and name. What it can see
  is shown at once, and the scan Lambda's full list replaces that after the scan.
  `companion.validate` checks every key and reports every problem (`CompanionError.errors`,
  at most 50). A problem with the file as a whole still stops the check: too large, not
  UTF-8, nested, not JSON, not one object, or a key twice. The PDF shows "Companion
  rejected". The API still refuses to close while a companion is invalid. The portal's Close
  asks, for each such PDF: fix the companion (the rejected one is removed and a corrected one
  can be added under the same name), remove the PDF, or close without the companion. Each is
  an existing file removal; no route or permission was added. Attaching or replacing a
  companion after ingestion (A4) belongs to the second half's reprocessing design.
- **Idle timeout** (owner decision, 2026-11-24): stack config `portalIdleTimeoutMinutes`
  (5 to 480) reaches the portal in `config.json` as `idleTimeoutMinutes`. The decision was 30;
  the owner set the dev stack to **60 for now** (2026-11-24). The stack value is required; the
  portal's fallback, used only if `config.json` lacks it, stays 30.
  - `portal/src/idle.ts` watches clicks, keys, wheel, touch and scroll in every tab. Tabs
    share their last activity on the BroadcastChannel `assay-portal-activity`, at most every
    15 seconds; pointer movement alone does not count.
  - A warning shows 2 minutes before. Then exactly one tab, the first to take the Web Lock
    `assay-portal-idle-sign-out`, signs out as the Sign out button does, other tabs included.
    It records `sign_out` with `detail.reason: "idle_timeout"` and `idle_minutes`; a manual
    sign-out records `reason: "user"`. The other tabs show why.
  - A tab that timed out but did not get the lock waits 10 seconds for the other tab's
    message, then ends its own session (with no second audit row).
  - Hidden tabs have slowed timers, so they can sign out up to about a minute late.
  - Refresh tokens still last 8 hours.
- **Cognito refresh-token rotation (assessed, not built, 2026-11-24).** It is available on
  the Essentials plan these pools use. With it on, every refresh returns a new refresh token
  and invalidates the old one (a grace period of 0 to 60 seconds for retries). The new token
  is valid only for the remainder of the original's lifetime, so sessions still end 8 hours
  after the sign-in. It works through the `/oauth2/token` endpoint the portal uses, and
  oidc-client-ts keeps the new token. It requires removing `ALLOW_REFRESH_TOKEN_AUTH` from the
  clients' explicit auth flows ("You can't authenticate with REFRESH_TOKEN_AUTH in app clients
  with refresh token rotation enabled"). It adds `jti` and `origin_jti` claims to access and ID
  tokens. A stolen refresh token stops working once the portal has refreshed, or the portal's
  own refresh fails if the thief refreshed first.
- **`update_tenants.py` is guarded** (owner's request, 2026-11-24): each tenant is previewed
  first and updated only if every planned change is in `update.ALLOWED_CHANGES` (build step
  6: create `bucket-cors`; update `malware-protection`'s `protectedResource`), then with
  `--target` set to exactly those resources. Anything else refuses that tenant. `--preview`
  applies nothing; `--tenant <id>` limits it to one tenant. Change `ALLOWED_CHANGES` with
  the tenant program.
  - **The plan's `status` in the diff** (first `--preview` on Isolation Test A, 2026-11-24,
    refused correctly). `status` is an output only: Computed in the provider, with no
    UseStateForUnknown, so any update marks it unknown; it is not among
    `MalwareProtectionPlanArgs`, and the program never sets it. AWS showed the plan `ACTIVE`
    with no prefixes. `update.ALLOWED_OUTPUTS` admits `status` for `malware-protection`
    only, and only while it is not an input the program sends and not an input difference
    in the provider's detailed diff. Every planned change now logs each output's value
    before and after.
  - **Scanning during the update.** It is an in-place `UpdateMalwareProtectionPlan` (only
    the bucket name forces a replacement), and the provider reads the plan once afterwards
    with no waiter. AWS does not document whether scanning pauses during an update. So after
    applying, the script reads each updated tenant's plan with the owner's credentials and
    requires `ACTIVE` and exactly `inbox/`. It waits up to two minutes for the status to
    settle, and otherwise prints a "MALWARE PROTECTION IS NOT ACTIVE" banner and exits 1.
    A file that is never scanned is still caught: after 30 minutes the sweeper puts it
    `on_hold` (`scan_timeout`) and raises an alarm.
- **Every Dockerfile that runs `uv export --locked`** copies `schemas/` and `workers/`
  manifests, and `.dockerignore` admits both packages; every image rebuilds on the first
  `pulumi up` of step 6.

### Step 6, second half (pipeline steps 5 to 10)

Plan approved by the owner on 2026-11-24. Every later stage follows these.

Stages, each one `pulumi up`, then `uv run scripts/migrate.py` if it has a migration, then the
owner's checks:

| Stage | Builds | Content to AI services | Status |
| --- | --- | --- | --- |
| 1, part 1 | The worker task, the attempt grant, the PDF sandbox, `app_worker`, migration `0010` | None | Complete (2026-11-25) |
| 1, part 2 | Attempts and "Process again", step 9 (companion), companion attach and replace, the quarantine notice, refresh-token rotation, migration `0011` | None | Complete, verified 2026-11-27 |
| 2 | Step 5: text layer, Textract for pages that fail the quality score (migration `0012`) | Textract | Complete, verified in dev 2026-11-27. See "Start here" |
| 3 | Steps 6, 7, 8: split, signer details, `pyHanko`, field extraction | Bedrock (Claude) | Not started |
| 4 | Step 10: chunk, embed, index; final statuses; Titan catalog row | Bedrock (Titan) | Not started |

Owner decisions (2026-11-24):

- **1. One ECS task per document**, started by Step Functions (`ecs:runTask.sync`), at most
  `workerMaxPerTenant` (5) at once per tenant. A document never fails for lack of capacity:
  it waits and tries again.
- **2. Old results on reprocessing:** attempt columns and pointers; `app_tenant` still has no
  DELETE anywhere.
- **3. Two tenant-admin routes** (`POST /documents/{id}/reprocess`,
  `POST /documents/{id}/companion`); no reprocessing by master roles. Multi-select "Process
  again" in the tenant history. Each writes an audit row; a companion replace names both
  versions. Confirm before reprocessing a `ready` document (it costs money).
- **4. A second SNS topic for security notices**, apart from the alarm topic, with its own
  endpoint policy statement. The address is stack config `securityNoticeEmail` (blank until
  the owner sets it; SNS then sends a confirmation link to follow within 48 hours).
- **5. Widenings approved:** the `sns` endpoint and the scan role's publish and
  KMS-through-SNS permissions; the state machine's ECS permissions with `iam:PassRole` for
  the worker's two roles only; the worker's Textract and Bedrock permissions, each only in
  the stage that needs it; the database column grants.
- **6. The tenant document panel shows each form's extracted fields** with page and
  confidence. Master views never show extracted values; add a test.
- **7. Only `ready` documents are retrieved.** A "Mark as reviewed" action for tenant admins
  moves `needs_review` to `ready`, with an audit row naming the user. `needs_review`
  documents are still indexed.
- **8. No `pdfplumber`.**
- **A. The worker is bound to one tenant, and to one document** (below).
- **B. PDF parsing is isolated** in a sandbox child, and the worker's database session is
  bound to its grant (below).
- **C.** Every AI call writes an `ai_usage` row (tenant, model, tokens, document). No prompt
  or response content is logged anywhere. Bedrock and Textract throttling is retried with
  backoff.
- **D.** Document text is untrusted input to the model: forced tool choice and schema
  validation stay, and a test uses a document that contains instructions.
- **E.** Before stage 2's deploy: re-run the opt-out and invocation-logging checks and show
  the owner the output (done on 2026-11-27; the same again before stage 3's deploy). (Run on 2026-11-25 03:58 UTC for the plan: `optOut` for every
  service, last updated 2026-11-24 04:37:58 UTC; no invocation logging configuration.)
- **Conditions on part 1** (a to f): the token's visibility recorded as a known limit; the
  isolation tests below, run in AWS; the sandbox image tests below; the parent distrusts the
  child's output; stage 1 split in two; every new grant and permission listed in the pull
  request's permission changes.

#### Stage 1, part 1: the worker task, the attempt grant, the sandbox, `app_worker`

Deployed on 2026-11-25 from commit 4518d32 (owner). Stack update 31 failed after creating 15
resources, on `assay-dev-tss-api-policy`: "Invalid policy document ... ensure that
Principals are valid", straight after `assay-dev-pipeline-worker-task-role` was created in
the same run (see "Policies that name a new role" below); update 32 made the rest. A
preview straight after showed 355 unchanged. `uv run scripts/migrate.py` moved Aurora to
revision `0010`; `--command verify` (read-only, 2026-11-25) reported `0010`,
`rls_verified: true`, `roles_verified: true`. All 20 alarms `OK` (the new one is
`assay-dev-pipeline-worker-errors`).

Deployed names: cluster `assay-dev-pipeline-worker`, task definition family
`assay-dev-pipeline-worker`, log group `/aws/ecs/assay-dev-pipeline-worker` (streams
`worker/worker/<task id>`).

How it works:

- **Fingerprint no longer opens a PDF** (owner decision B4). A Lambda has one user and its
  credentials in its environment, so no Lambda may run a PDF parser, and the Lambda image
  ships none (`pypdfium2` is the `worker` extra of `assay-workers`). Fingerprint keeps the
  hash, the duplicate check and a comparison of the first bytes with `%PDF-`. Opening the
  PDF (password-protected, not a PDF that opens, page count) and the copy to `originals/`
  moved to the worker task's `inspect` step. So a password-protected PDF now waits for a
  task before it shows On hold.
- **State machine:** `Fingerprint` (Lambda), `Claim` (Lambda), `Worker`
  (`ecs:runTask.sync`), `Release` (Lambda).
  - `claim` takes one of the tenant's worker slots and creates the attempt grant
    (`assay_db.grants`, table `worker_grants`): a random token of which only the SHA-256 is
    stored. With no slot free it answers `wait`, and the execution waits 30 seconds and asks
    again. A live grant is the slot.
  - The task gets `DOCUMENT_ID`, `ATTEMPT` and `WORKER_TOKEN` as overrides and nothing else
    about the document. Its result is discarded.
  - `release` ends the grant. After a failed task it also says whether the task ever started
    (the task writes a `worker`/`running` event when it has bound its session). If it never
    did (no Fargate capacity, an image that could not be pulled), the execution waits 60
    seconds and claims again; `release` logs that at WARNING, and at ERROR every tenth try.
  - The execution's timeout (12 hours, inside a Standard execution's 25,000 history events)
    is the only limit on waiting. It leaves the document `processing`, never `failed`, and
    raises `executionstimedout`.
- **Failed steps still name themselves.** The task writes a `running` mark before each of
  its steps and an `error` mark, with the exception's type, when it can
  (`assay_workers.progress`). For a failed task the `failed` handler reads the step from
  those marks in the database (`inspect_failed`), never from the error; with no mark it is
  `worker_failed`. The history views leave out a `running` mark once its step has a later
  row (`assay_schemas.pipeline.visible_events`). **Later stages: add each new step to
  `progress.WORKER_STEPS`, to the portal's `STEP_TEXT` and `RESULT_TEXT`, and to
  `PIPELINE_STEPS`.**
- **A sandbox child that does not end well fails the document** (`pdf_unreadable`) and
  logs at ERROR, which raises `worker-errors`. How it ended is named: see "Every ending of a
  sandbox child has its own name" below.

Binding the worker (owner decisions A1, A2, B5):

- **Storage.** The worker calls the TSS at `POST /resolve-worker` with the document ID, the
  attempt and the token. It names no tenant. The TSS takes the tenant from the grant's row
  (`pipeline.worker_grant`) and only while the grant is live: not ended, not expired, the
  document's current attempt, the document `processing`. Access `worker`: a session policy
  with that document's exact keys and no wildcard (its inbox files, read; its original,
  companion, text and fields objects, read and write). No `quarantine/`, no other document,
  no vectors (they arrive in stage 4, tenant-wide: S3 Vectors has no per-key permission), no
  face collection. `/resolve` refuses the worker; `/resolve-worker` refuses every other
  caller; each caller's IAM policy and the API's resource policy name one route per caller.
- **Database.** The worker signs in as `app_worker`, not `app_tenant`. `app.tenant_id` means
  nothing to it. It calls `pipeline.bind_worker(document, attempt, token)`, and its policies
  read `pipeline.bound_tenant()` and `pipeline.bound_document()`, which check the grant
  again on every use. An unbound session, or one whose grant ended, sees nothing. A bound
  one sees one document. Column-level grants on `documents`, `batch_files` and
  `document_events` only: no file name, no `users`, no chat tables, no `audit_log`. **Later
  stages add `app_worker`'s grants and policies for each table their steps use, in that
  stage's migration.**
- The binding is a session setting that holds the token, checked against the stored hash.
  A session cannot forge it without the token. It is set in its own committed transaction,
  because a session setting made in a transaction that rolls back is undone.

The sandbox (owner decisions B3, B4; `assay_workers.sandbox`):

- The task's main process runs as root with CAP_SETUID, CAP_SETGID and CAP_KILL only, on a
  read-only root filesystem. Fargate cannot add capabilities, so the task definition drops
  the other 34 by name. It holds the credentials and the database session and never opens a
  PDF.
- Each job runs in a child: user `sandbox` (uid 10002), no groups, an empty environment,
  `python -I -B`, its own session, limits (60 s wall clock, 45 s CPU, 2 GiB address space,
  no file writes, 16 processes, 64 KB of output), `no_new_privs`, and a seccomp filter set
  between fork and exec that refuses `socket` and the calls that reach other processes. The
  child then forbids itself `exec`.
- CPython sets `LC_CTYPE` for itself under the "C" locale, so a child's environment is not
  literally empty; that one name is allowed (`runner.SET_BY_THE_INTERPRETER`).
- The parent distrusts the child: one JSON object, at most 64 KB, validated against a model
  with only numbers and codes from fixed sets. No path or key comes from the child. If the
  child cannot be started as the sandbox user, the job does not run.
- **Every ending of a sandbox child has its own name** (owner, 2026-11-25), so a limit that
  did its job never reads the same as a child that died some other way
  (`sandbox.runner.ending`):

  | Ending | Name | Kind |
  | --- | --- | --- |
  | The kernel, at the CPU limit | `cpu_limit` | limit |
  | The parent, at the wall clock | `wall_clock_timeout` | limit |
  | The child reported that the address-space limit stopped it | `memory_limit` | limit |
  | More output than the parent accepts | `output_limit` | limit |
  | It tried to write a file | `file_size_limit` | limit |
  | Any other signal (a crash, a kill from outside the sandbox) | `signal_SIGSEGV`, `signal_SIGABRT`, `signal_SIGKILL`, ... | unexpected |
  | It exited with a failure status, or printed no valid result | `exit_<status>`, `bad_output` | unexpected |
  | It could not be started as the sandbox user | `not_started` | the task's fault |

  - The CPU limit warns before it kills: SIGXCPU at the soft limit, SIGKILL one second later
    at the hard limit. SIGXCPU is the CPU limit by definition. A SIGKILL is named
    `cpu_limit` only if the child had used its CPU time (it ignored the warning); any other
    SIGKILL is `signal_SIGKILL`, which is what the task running out of memory would look
    like.
  - **Not distinguishable:** a native library that cannot get memory usually aborts, so the
    address-space limit can also end as `signal_SIGABRT` or `signal_SIGSEGV`. Only a
    Python-level failure to allocate is named `memory_limit`.
  - **What a real document shows.** For every ending but `not_started`: status Failed, code
    `pdf_unreadable`. A tenant user sees "The PDF could not be opened. Check the file and
    upload it again." and nothing more. Master admins see the code and the ending on the
    Inspection step: `pdf_unreadable · sandbox.cpu_limit`. It is kept in the event's
    `error_type`, which `platform.ingestion_events_v` already shows them; the tenant's
    document detail reads `detail.error` only. No migration and no grant changed.
  - **Logs and the alarm.** One ERROR line per ending, with `ending` and `sandbox_limit`
    (true or false), and two different messages: "the PDF check was stopped at a sandbox
    limit" and "the PDF check ended unexpectedly". Both raise `worker-errors`: either is
    what a broken file looks like, and what an attack on the parser looks like. The
    execution succeeds (the task handled it), so `executionsfailed` stays quiet.
  - `not_started` is not the file's fault: the step fails (`inspect_failed`, error type
    `SandboxNotStarted`) and the document is not blamed. That raises `worker-errors`,
    `steps-errors` and `executionsfailed`.
- Not available on Fargate: a cgroup, network namespace or firewall rule for one process; a
  task-level seccomp or AppArmor profile; a second container with another role.
- Residual risk (accepted by the owner, 2026-11-24): a parser exploit runs as the sandbox
  user for at most the timeout, with the one PDF it was given; it can return wrong values
  for that document or make it fail. To reach credentials it needs a second exploit, against
  the kernel in the task's microVM, and would then hold one document's worker credentials.

Known limit (owner condition a, 2026-11-24):

- **The grant's token is visible** in the execution's history (Step Functions keeps the
  state), in the task's description (the container overrides) and in CloudTrail (the
  `RunTask` request parameters), to principals that can read those. Accepted, because the
  token is useless without the worker task's role (only that role may call
  `/resolve-worker` or sign in as `app_worker`), and dead when the attempt ends (`release`)
  or the grant expires (40 minutes at most). The state machine's log still records no
  execution data (`includeExecutionData: false`, level `ERROR`; tested). Credentials issued
  just before an attempt ends still last their 15 minutes, the STS minimum.

New grants and permissions (all approved 2026-11-24; also in the pull request):

- **Database (migration `0010`):** role `app_worker` (login, IAM) with SELECT on ten columns
  of `documents`, nine of `batch_files`, five of `document_events`; UPDATE on six columns of
  `documents`; INSERT on `document_events`; EXECUTE on `bind_worker`, `bound_tenant`,
  `bound_document`. `tss`: USAGE on schema `pipeline`, EXECUTE on `worker_grant`.
  `pipeline_lookup`: SELECT on `documents.attempt`, `batch_files.document_id` and six
  columns of `worker_grants`. `app_tenant`: SELECT, INSERT, UPDATE on the new
  `worker_grants`.
- **IAM:** the worker task role (`rds-db:connect` as `app_worker`; `execute-api:Invoke` on
  `/resolve-worker`); its execution role (pull its image, write its logs); the state
  machine role gains `ecs:RunTask` (the worker task definition, its cluster),
  `ecs:StopTask` and `ecs:DescribeTasks` (that cluster's tasks), `iam:PassRole` (those two
  roles, to ECS tasks) and the three `events` actions on Step Functions' managed rule.
- **Resource policies:** the TSS API admits the worker role on `/resolve-worker`; API
  Gateway may invoke the TSS Lambda for that route.
- No change to the tenant role, the bucket, key or vector bucket policies, any endpoint
  policy or any queue. No new KMS key. No `update_tenants.py` run.

Deploy order (owner): `pulumi up`; `uv run scripts/migrate.py` straight away (the bootstrap
creates `app_worker`, then `0010`); `uv run scripts/run_isolation.py ...`; browser checks;
`uv run scripts/check_deployment.py` (since 2026-11-25).
Every image rebuilds (the lock file changed). Until `0010` is in, a document that reaches
`claim` fails there, so upload nothing in between.

Tests:

- `tests/unit/test_db_worker.py`: the grant, the TSS's lookup and `app_worker`, against a
  real PostgreSQL, signed in as each role.
- `tests/unit/test_worker_image.py` (Docker; condition c): the real child, run as the task
  runs it. No socket, no parent environment or memory, no AWS or database library, stopped
  at the timeout and the limits, a malformed PDF, memory exhaustion. 16 of 16 pass locally.
- `tests/unit/test_worker_probe.py`: the isolation probe passes every check on a correct
  system and fails the right one when what it guards is broken. That includes its four
  `inspection:` checks (see "A password-protected upload may never reach the worker").
- **In AWS** (condition b): `scripts/run_isolation.py` also runs the worker task with
  `WORKER_COMMAND=probe`. The isolation Lambda prepares three fixture documents with fixed
  IDs in the two test tenants (`tests/isolation/worker_fixture.py`), each with a small
  object at its `originals/` key (written once, kept by Object Lock), and ends one grant
  while the probe watches. The fixture documents are `discarded` between runs and have a
  `start` event, so the pipeline never queues them. They do have `documents` rows, unlike
  the reserved ID. In Isolation Test A they are `955f3e3d-f39a-5962-bc37-4b37ae1a44c6` (the
  probe's own) and its sibling.

First isolation run after the deploy (owner, 2026-11-25, 11:33 UTC):

- 71 of 71 in the isolation Lambda (the two new ones: `/resolve-worker` refuses the runner;
  worker access cannot be asked for by name), 28 of 28 in the provisioning role's probe, 7
  of 7 Cognito client checks.
- **Worker probe: 64 of 65.** Every TSS, storage, database, confinement, inspection and
  capability check held. The one failure was the check, not the sandbox: "a child that
  tries to spin is stopped" expected the parent's wall-clock `timeout` and got
  `signal_SIGKILL`. Its limits were 5 seconds of wall clock and 4 of CPU, so the kernel's CPU
  limit ended the spinning child first, as it should (with the soft and hard limits equal,
  the kernel sends SIGKILL, not SIGXCPU). The image test had accepted either ending, and
  the unit test's stand-in answered `timeout`, so neither caught the wrong expectation. It
  also meant the wall-clock timeout itself was never shown on Fargate.
- **Fix (same day):** one check per ending, each with limits only one of which can be
  reached, and each expecting that ending's own name: a child that spins, with the wall
  clock far off (`limit:cpu_limit`); one that ignores the kernel's warning
  (`limit:cpu_limit`); one that only waits, using no CPU time (`limit:wall_clock_timeout`);
  one that crashes (`unexpected:signal_SIGABRT`). The image test requires exactly those.
  **Lesson: a check that accepts several endings hides which mechanism worked; give each
  mechanism a case only it can end.**
- **Second isolation run** (owner, 2026-11-25 12:27 UTC, after stack update 34 deployed
  commit 714ec8c): 71 of 71 in the isolation Lambda, 28 of 28 in the provisioning role's
  probe, **68 of 68 in the worker probe**, 7 of 7 app client checks. So each ending is
  reported by its own name on Fargate too: the CPU limit for a child that spins and for one
  that ignores the warning, the wall-clock timeout for one that only waits, and an
  unexpected signal for one that crashes.
- **Policies that name a new role** (owner, 2026-11-25). IAM is eventually consistent: for
  some seconds after a role is created, another service may refuse a resource policy that
  names it as a principal. That is what failed update 31. A policy's dependency on the role
  is not enough (Pulumi waits for the role, not for IAM to tell every service), so each
  such policy also depends on `iam_propagation` (`infra/assay_infra/propagation.py`): a
  local command that waits 30 seconds once the roles exist. It runs when first created and
  again only when the set of roles changes, never on an ordinary `pulumi up` or a preview,
  and changes nothing in AWS.
  - The three policies with a role of this program in a `Principal` element: the TSS API's
    resource policy (API Gateway; the provider does not retry it, and it is the one that
    failed), the tenant role's trust policy and the tenant-state key's policy (IAM and KMS;
    the provider retries both, and each was created with its role in build step 4 without
    failing; they wait all the same).
  - Policies that name a role only in a condition (`aws:PrincipalArn`) are not checked
    against IAM and need nothing: the ingest queue's, the state bucket's and every VPC
    endpoint's.
  - **A new policy that names a role of this program as a principal must depend on the
    wait.** Part 2's security-notice topic will not: the scan role publishes through its own
    IAM policy, and the topic's policy names only services.
  - Not shown to work: it can only be shown by a run that creates a role and such a policy
    together, and the next one that does is a new stack.
  - It had happened once before: CloudTrail shows the same refusal of the TSS API's policy
    at 2026-11-24 19:36:07 UTC, in the first half's deploy, when the pipeline roles were new.

Settled by that run and the deploy:

- The switch of user and the seccomp filter work on Fargate as under Docker: the child ran
  as uid 10002 with no groups, an empty environment and `Seccomp: 2`, and was refused a
  socket, the parent's environment and memory, `exec` and every write.
- The task's main process has exactly CAP_KILL, CAP_SETGID and CAP_SETUID: `CapEff`,
  `CapPrm` and `CapBnd` all `00000000000000e0`, on a read-only root filesystem.
- The task definition's container definition round-trips: 34 names in `drop`, and the
  preview after the deploy showed no difference.
- The real inspection job, on Fargate: the plain PDF opened with 3 pages; the 40-bit and
  128-bit protected PDFs were both `pdf_encrypted`; the broken file was `not_a_pdf`.
- A session whose grant ended while it was connected saw its document, then nothing.
- **A worker task starts in about 18 seconds** (the probe task: created 11:33:03, image
  pulled in 6 seconds, started 11:33:21 UTC), not the 30 to 90 estimated.

Browser check (owner, Infinity Loans, `--fresh` set, batch received 2026-11-25 11:51 UTC,
on stack update 33; update 34 deployed commit 714ec8c afterwards, at 12:21 UTC, and the
isolation run has not been repeated on it yet):

- 02 `processing`, Inspection: Passed (fingerprint 11:56:11, inspection 11:56:37: 26 seconds
  from fingerprint to the inspection's result, the task's start included); 01 `duplicate`
  (which of the two is the original depends on which is fingerprinted first).
- 04 `processing` with its companion; 05 `processing` after "Close without the companion
  file"; 06 `failed` at Fingerprint, "The file is not a PDF."
- **03 and 07 `on_hold` at the scan: GuardDuty held both the 40-bit and the 128-bit
  protected PDF** (`scan_unsupported`). So neither reached the worker; its own check was
  shown by the probe on Fargate (both `pdf_encrypted`).

**One scan result failed once, and Lambda's retry recovered it** (same batch):

- `assay-dev-pipeline-scan-errors` (11:52 UTC) and `-scan-failed` (11:53 UTC) fired. One
  invocation, request ID `61eaf513-114b-40cd-831b-0834a5b6bad0`, raised
  `TssRequestError: TSS refused the request: 500 Internal server error` at its call for a
  storage context (`scan.py`, `deps.storage.pipeline`). It was the result for 02's file
  (`86845ddc-…`). Lambda retried it 59 seconds later with the same request ID and event,
  and it was recorded clean at 11:52:24. Until then the file showed "Waiting for its scan".
- **Cause: Lambda concurrency, not the files and not Aurora.** The account's quota is **10
  concurrent executions** (`aws lambda get-account-settings`; Service Quotas L-B99A9384,
  adjustable, never raised). Nine scan results arrived together, so nine scan functions ran
  at once, each needing the TSS's function too. The account reached 10 concurrent
  executions that minute and Lambda throttled one invocation of `assay-dev-tss`
  (`Throttles` 1, `Errors` 0). API Gateway answered that call itself: 500 "Internal server
  error" (its own `message`, not the TSS's `error`). The TSS's log has a "storage context
  issued" line for each of the other eight files at 11:51:24 to 11:51:26 and none for this
  one until 11:52:23: its function never ran for the failed call.
  - Not the identical files: the failure came before anything about the file's content was
    read, and 01's result, with the same bytes, was recorded with the others.
  - Not Aurora: the invocation had already made its database lookup, and the other eight
    were recorded within two seconds.
- **What saved it:** only Lambda's retry of the asynchronous invocation (2 retries, events
  kept an hour). Had all three attempts failed, the event would have gone to the scan
  dead-letter queue (alarm), the file would have stayed `registered`, and after 30 minutes
  the sweeper would have put its PDF `on_hold` (`scan_timeout`, alarm). Nothing is lost
  silently, but a good document would have ended on hold.
- **Fix in code** (no permission change): `TssClient.send` tries again, up to five times
  over at most 20 seconds with jitter, when the answer is API Gateway's own (429, 502, 503,
  504, or a 500 without the TSS's `error`). The TSS's own answers are never retried. It
  logs a WARNING per retry. This covers every caller: the scan, step and sweeper functions,
  the worker task and the API.
- **That does not cure the limit.** With 10 concurrent executions, a batch of 200 PDFs
  cannot work: scan functions would hold every slot while each waits for a TSS call that
  cannot get one. Reserved concurrency cannot be set either, with a quota this low. The
  same limit can throttle the Cognito triggers (a sign-in), the API's calls to the Cognito
  admin function and the state machine's steps. **The owner asked AWS to raise the
  quota to 1,000** on 2026-11-25 12:36 UTC (request `7902077dd06644889e37c4c5dde4b612UGimylpJ`,
  `PENDING` when made; the usual default):
  `aws service-quotas request-service-quota-increase --service-code lambda --quota-code L-B99A9384 --desired-value 1000 --profile pilar-dev --region us-east-1`.
  **Granted: the account allows 1,000** (owner, `get-account-settings`, 2026-11-25).
  Staging and Production need the same request before their first deploy (see "Before
  deploying to Staging or Production").
  - **Reserved concurrency: 50 for the scan function, 300 for the TSS** (owner decision,
    2026-11-25; built, in the next deploy). Stack config `scanReservedConcurrency` and
    `tssReservedConcurrency`, both or neither, the TSS's at least the scan function's
    (`config.validate_pipeline`). Reserved concurrency is both a cap and a guarantee, at
    no charge; 650 of the account's 1,000 stay unreserved.
    - **Why the scan function is capped.** It is the only function driven by outside
      arrivals (every uploaded file's scan result), each run holds a database connection
      and makes a TSS call, and it is the only one whose overflow is safe. At the measured
      3.9 seconds a result, 50 at once is about 13 results a second: a full batch of 400
      files is about 30 seconds of work.
    - **Why the TSS has a reservation.** Every pipeline function needs it while it runs. On
      2026-11-25 scan functions held every slot the account had, each waiting for a TSS
      call that could not get one. A reservation is capacity nothing else can take. 300 and
      not 200: a cold TSS call can take 14 seconds, so one full batch's 200 fingerprint
      steps starting together can hold 200 TSS instances at once, beside the scan
      function's 50. The TSS client's retry (five tries over 20 seconds) absorbs a brief
      overflow.
    - **What happens over the scan function's cap.** The result waits in Lambda's
      asynchronous queue and is retried with backoff, from 1 second up to 5 minutes, for the
      function's event age (1 hour here), then goes to the scan dead-letter queue, whose
      depth raises an alarm (AWS's page on asynchronous invocation, read 2026-11-25; it
      does not say whether those retries count against the function's two attempts). The
      30-minute `scan_timeout` is the limit that matters: a result that waits longer finds
      its file already on hold, and the document stays on hold.
    - **No other function is capped, on purpose.** Their callers are synchronous: a
      throttled pipeline step fails its document (`fingerprint_failed ·
      Lambda.TooManyRequestsException`, the throttle test), and a Cognito trigger has 5
      seconds. `test_only_the_scan_function_is_capped_and_only_when_the_stack_says_so`.
  - **Measured** (CloudWatch, three days to 2026-11-25; small numbers, mostly cold starts):
    the scan function ran 43 times, 3.9 seconds on average and 8.7 at the 99th percentile,
    9 at once at most; the TSS 265 times, 0.9 seconds on average on the last day, 14
    seconds at the 99th percentile (a cold start, or Aurora resuming from 0 ACU), 7 at
    once at most. The account's peak was 20, during the 13 documents processed again
    together. The steps function's 8 throttles are all from the deliberate throttle test.
    **Measure again once real batches have run**, and change the two numbers in stack
    config if the peaks say so.
  - **`check_deployment.py` confirms both settings.** It reads each function's reserved
    concurrency from Lambda and fails if it differs from the stack's config (also if one
    was set by hand that the stack does not set). A stack that sets neither passes with a
    warning. The account's limit must cover `concurrency_needed`: the two reservations
    and 400 unreserved (a full batch's 200 pipeline steps and 200 for everything else);
    750 for Development. **Staging and Production: raise the quota first** (see "Before
    deploying to Staging or Production"); until then leave both keys unset.
- **The TSS API's access log received nothing until its role's trust policy was changed**
  (found on the way; investigated read-only on 2026-11-25). **Cause proven the same day:
  API Gateway sends no source account when it assumes the logging role to write a log
  entry.** The fix is permanent; see "The experiment" and "Outcome" below.
  - It is configured as AWS documents. Stage `v1` has `accessLogSettings` (the log group
    `/aws/apigateway/assay-dev-tss`, a format with `$context.requestId`). The account's
    `cloudwatchRoleArn` is the stack's `assay-dev-tss-api-logs-role`, with
    `AmazonAPIGatewayPushToCloudWatchLogs`. The log group exists, with no KMS key and no
    stream. The API counted 176 requests in three days, and one 5XX at the time of the
    throttled call.
  - CloudTrail: API Gateway assumed the role only when the account setting or the stage was
    updated (2026-11-23 21:35, 11-24 19:49, 11-25 11:25 UTC), each time to call
    `DescribeLogGroups` on the group, and once to write its own test message to
    `/aws/apigateway/welcome`, which worked. It never assumed the role at the time of a
    request, and no `CreateLogStream` was ever made in the group.
  - A refused `AssumeRole` by a service does not appear in this account's trail: the two
    `UpdateAccount` calls refused at 21:34 on 2026-11-23 left none. So a refusal at request
    time cannot be seen from here.
  - **The one difference from AWS's documented setup** is the role's trust policy: ours adds
    `aws:SourceAccount` (`_service_trust`, used for every service role). AWS's logging page
    asks only for `apigateway.amazonaws.com` as the trusted entity. The control plane sends
    the account (its assumes passed `StringEquals`); no AWS page says whether the part that
    writes logs does. This was the candidate cause, and the change below proved it.
  - **Ruled out:** a log delivery and a log group resource policy for
    `delivery.logs.amazonaws.com`. That is how HTTP APIs log; the owner's credentials would
    have created both when the stage was set up, and CloudTrail has no `CreateLogDelivery`.
  - **The experiment (approved by the owner, 2026-11-25): a deliberate, narrow trust
    widening.** On the API Gateway logging role only (`tss.api_logs_trust_policy`),
    `StringEquals` becomes `StringEqualsIfExists` for `aws:SourceAccount`. A call from
    `apigateway.amazonaws.com` that carries no source account is admitted; a call that
    carries another account's is still refused. Every other service role keeps
    `StringEquals` (`_service_trust`); `test_infra_tenancy.py` fails if `IfExists` appears
    anywhere else in `infra/`. Why the risk is low:
    - The role has only `AmazonAPIGatewayPushToCloudWatchLogs`: create log groups and
      streams, write log events, and describe and read log events (`GetLogEvents`,
      `FilterLogEvents`), all on `*` (read from IAM, 2026-11-25). So it is CloudWatch Logs
      only, but not write-only. It reaches no bucket, key, queue, database or tenant data.
    - To misuse it, another account would have to make this role its own API Gateway
      logging role. Setting that needs `iam:PassRole` on the role (CloudFormation's schema
      for `AWS::ApiGateway::Account` lists `apigateway:PATCH`, `iam:GetRole` and
      `iam:PassRole`; `aws cloudformation describe-type`, 2026-11-25), and IAM lets a
      principal pass only a role of its own account. The `UpdateAccount` API page does not
      state the requirement itself.
    - API Gateway checks the role when it is set, and that call carries the source account
      (ours passed `StringEquals`), so another account's would still be refused.
  - **What decides it** (owner): deploy, run the isolation suite, then
    `uv run scripts/check_deployment.py`.
    - Entries arrive: the cause is proven and the change stays.
    - None arrive: put the condition back exactly as it was (`_service_trust`), say whether
      a stage redeploy could matter, and prepare the evidence for an AWS Support case.
    - **Outcome: proven, and the change is permanent** (owner, 2026-11-25). Stack update 35
      deployed commit e3ccbfd; the isolation run after it passed (71, 28, 68, 7) and
      `check_deployment.py` showed the access log receiving (last 13:55 UTC; 24 receiving,
      the break-glass log not proven). CloudTrail shows the difference: from 13:22 UTC API
      Gateway assumes the role at the time of each request and creates a log stream in the
      group (`CreateLogStream`, 13:55:24), which it never did in the two days before.
      Nothing else changed in that deploy that touches API Gateway.
    - **Never change this role back to `StringEquals`**, and never "tidy" it to match the
      other service roles: the access log would go silent again, with no error anywhere.
      Two tests hold the form: `test_the_access_logs_role_keeps_the_trust_that_lets_api_gateway_write`
      (the role in the program, and that no other role has the wider form) and
      `test_only_the_api_gateway_logging_role_admits_a_call_with_no_source_account`.
    - **Staging and Production need the same form.** It is in the program
      (`tss.api_logs_trust_policy`), so a new stack gets it; `check_deployment.py` after the
      first deploy's checks confirms the log is receiving.
  - Until it is fixed, API Gateway's own record of a call (caller, integration status) does
    not exist, and `uv run scripts/check_deployment.py` fails on this log.
- **`uv run scripts/check_deployment.py`** (owner's request, 2026-11-25; read-only). For
  every resource with the stack's tags it reads where the logs are configured to go, from
  the resource itself, and fails if a destination has received nothing. It also warns if the
  account's Lambda concurrency limit is below what the stack needs, and (since the caps
  were decided, 2026-11-25) fails if the scan function's or the TSS's reserved concurrency
  is not what the stack's config says: see "Reserved concurrency" above. With the caps the
  need is 750 for Development (50, 300 and 400 unreserved), where it was 1,000 without.
  - Run before the deploy that sets the reservations (2026-11-25), it failed on both, as
    it should: each function reserved nothing and the stack says 50 and 300.
  - First run (2026-11-25): 25 log destinations; 23 receiving; the TSS API's access log
    `EMPTY` (fails); the break-glass log `not proven`.
  - **The break-glass log is empty because the role has never been assumed** (the rule's
    `MatchedEvents` is 0; CloudTrail has only the role's creation). So the rule, its log
    and the `break-glass-assumed` alarm have never been seen to work in AWS (now on the
    before-production list). An empty log whose source was idle is reported and fails only
    with `--strict`; the owner keeps non-strict as the default (2026-11-25).
  - A log whose source has no activity metric (an ECS task's, the flow log, WAF, Aurora) is
    `EMPTY` until it has received something, so run the check after the isolation run.
  - **A new kind of log destination in the stack needs a function in the script.** It
    covers API Gateway access logs, Lambda, ECS, Step Functions, flow logs, WAF, Aurora's
    exports, EventBridge rules that write to a log group, load balancer logs and CloudWatch
    Logs deliveries (CloudFront). Not covered: API Gateway execution logs (none configured).

Still not known:

- How a RunTask refused for the vCPU quota reaches Step Functions. The design does not
  depend on it: any failed task with no `worker`/`running` event is waited out.
- Whether GuardDuty holds every kind of password-protected PDF at the scan. It held both
  kinds tried (40-bit and 128-bit RC4); AES-encrypted PDFs were not tried.

Notes:

- **GuardDuty's scan result event carries the threat name**:
  `detail.scanResultDetails.threats[0].name` (user guide, read 2026-11-25: "contains only
  one threat ... the first detected threat"). The note under "Carried forward to step 6" says
  the event lacks it; `scan.parse` just does not read it. Part 2 reads it there, with no
  GuardDuty permission. Not yet seen in a real event here: the part 2 EICAR run confirms it.
  The same page shows `scanStatus: SKIPPED` with `statusReasons` such as
  `PASSWORD_PROTECTED`, which could make `scan_unsupported`'s wording certain.
- **Linux-only code and mypy.** The sandbox uses `resource`, `os.killpg` and `signal.SIGKILL`,
  which do not exist on the Windows development machine. Each function that uses them starts
  with `if sys.platform != "linux": raise OSError(...)`, which mypy understands: it checks
  the rest on Linux (CI) and skips it on Windows.
- **`preexec_fn` runs after the switch of user** in CPython's `subprocess`, so the limits and
  the filter are set by the sandbox user itself, before the child's interpreter starts.
- **Bash heredocs halve backslashes** on this machine: write a script with the Write tool
  and run it, or use Edit.
- **Test files that are never duplicates** (owner's request, 2026-11-25).
  `uv run scripts/make_test_files.py --fresh` adds a new run ID to every page of every PDF,
  so a tenant that already holds the plain set (Infinity Loans does) gets new documents.
  Within one run 02 is still byte for byte 01, so the duplicate check still shows.
- **A password-protected upload may never reach the worker.** GuardDuty held
  `03-password-protected.pdf` (40-bit RC4) at the scan on 2026-11-24 (`scan_unsupported`).
  AWS documents that password-protected files are not scanned, and not how it recognises
  one, so it is not known that it catches every kind. The worker's own check
  (`pdf_encrypted`, owner decision 13: "whatever GuardDuty said") is the backstop for a kind
  it misses. It is exercised three ways, none of which depends on GuardDuty missing one:
  - `tests/unit/test_worker_step6.py`: the real job on the real protected file, with the
    document ending `on_hold` in a real PostgreSQL;
  - `tests/unit/test_worker_image.py`: the real sandbox child, in the task's image, on both
    protected files;
  - in AWS, the worker probe's four `inspection:` checks (`run_isolation.py`): the real job,
    in the real sandbox, on Fargate, on a plain PDF, both protected PDFs and a broken one
    (`assay_workers/worker/probe_files.py`, the test-file script's own output).
  `07-password-protected-128.pdf` (128-bit RC4) is a second kind to try through the browser.
  **Tried on 2026-11-25: GuardDuty held it too** (`scan_unsupported`, "Could not be
  scanned"), as it holds 03. So no upload has yet reached the worker's own check.

#### Stage 1, part 2: attempts and "Process again", step 9, companion replacement, the quarantine notice, "Mark as reviewed"

Built on 2026-11-25 (owner's go-ahead once the access log was settled). **Deployed the same
day** from commit ae46223: stack update 37 failed after creating 6 resources, on the worker
image's build: "`<registry>/assay-dev-pipeline-worker@sha256:24069468...`: not found" (the
owner kept the message; it was the build's own check, not a missing image: see "not found"
under "Step 2"). Update 38 finished the rest.
`uv run scripts/migrate.py --command verify` (read-only): revision `0011`,
`rls_verified: true`, `roles_verified: true`. The security-notice address is confirmed.
The owner's isolation run after the deploy: 71 of 71, 28 of 28, 68 of 68, 7 of 7.
No content goes to any AI service in this part.

**The rotation probe's result** (same run; replica pool, rotation on with a 10-second grace
period). It settles decision 4 below; the real clients are unchanged until the owner decides:

| Candidate | What Cognito stored | Programmatic sign-in |
| --- | --- | --- |
| No explicit flows | `[]`, rotation `ENABLED` | **`USER_SRP_AUTH` allowed** (it answered with the `PASSWORD_VERIFIER` challenge). Password, custom and `USER_AUTH` refused |
| `ALLOW_CUSTOM_AUTH` only | `['ALLOW_CUSTOM_AUTH']`, rotation `ENABLED` | **All four refused.** Custom auth: "Custom auth lambda trigger is not configured for the user pool" |
| The refresh flow kept | Refused: "ALLOW_REFRESH_TOKEN_AUTH is not a permitted ExplicitAuthFlow when refresh token rotation is enabled" | - |

- **An empty flow list is not "no flows".** Cognito stores `[]` and still lets anyone start
  an SRP sign-in against the client, outside managed login. Never give a portal client an
  empty list.
- **`ALLOW_CUSTOM_AUTH` alone leaves no programmatic sign-in**, but only while the pool has
  no custom-auth triggers (`DefineAuthChallenge`, `CreateAuthChallenge`,
  `VerifyAuthChallengeResponse`). Neither pool has one (read 2026-11-25: the tenant pool
  has post-confirmation, post-authentication and pre-token-generation; the platform pool
  the first two). A rule that goes with this setting: those three triggers are never added
  to a pool whose client lists `ALLOW_CUSTOM_AUTH`.
- Both real clients at the time of the probe: `['ALLOW_REFRESH_TOKEN_AUTH']`, no rotation.
  (Since 2026-11-27 both have `['ALLOW_CUSTOM_AUTH']` with rotation on.)
- **Not shown by the probe** (the replica has no managed login domain): that managed login
  still signs a user in when the client's only flow is `ALLOW_CUSTOM_AUTH`, and that the
  portal's refresh through `/oauth2/token` returns a new refresh token. Today's clients
  list only the refresh flow and managed login works, which suggests it does not depend on
  the list; the first real client to change shows it.

**Rotation: the owner's decision (2026-11-25) and the rollout.** `ALLOW_CUSTOM_AUTH` as a
client's only explicit flow, rotation on, a 10-second grace period. An empty list is
rejected because it allows `USER_SRP_AUTH`. Two steps:

1. **The tenant app client only** (stack config `tenantRefreshTokenRotation: "true"`).
   **Deployed and verified by the owner on 2026-11-27**: tenant sign-in works in a browser
   under rotation, and a forced refresh returned 200 from `/oauth2/token` with the session
   continuing. So managed login does sign a user in on a client whose only explicit flow is
   `ALLOW_CUSTOM_AUTH`. The same deploy set the reserved concurrency (`check_deployment.py`:
   50 and 300, 650 unreserved) and the two-layer images; the isolation run passed 71, 28
   and 68, and 24 of 26 Cognito checks (the two below).
2. **The platform client** (`platformRefreshTokenRotation: "true"`). **Deployed and
   verified by the owner on 2026-11-27** (stack update 40): a forced refresh returns 200 on
   both portals, and the Cognito section of the isolation run passes 28 of 28. To go back
   on either client: set its key to `"false"` and run `pulumi up`.

**What Cognito answers on a client with rotation on** (isolation run of 2026-11-27, and
AWS's pages read the same day). The run's two failures were refusals the checks did not
recognise: they had been written for a guess (`InvalidParameterException`,
`NotAuthorizedException`). **Lesson: a check that expects "refused" in general hides what
was refused and why; require the service's own answer by name.**

- **`REFRESH_TOKEN_AUTH` answers `UnsupportedOperationException`.** AWS: "You can't
  authenticate with REFRESH_TOKEN_AUTH in app clients with refresh token rotation enabled",
  and of the exception, "thrown when you attempt to perform an operation that isn't enabled
  for the user pool client." So it is about the client, not the token. **AWS does not say
  whether the token is looked at first.** The check now asks twice, with the user's current
  refresh token and with one that is not valid (one character changed), and requires the
  same answer: if the token were judged first, the second would answer differently.
  **Settled by the owner's run of 2026-11-27 (28 of 28): Cognito gives the same
  `UnsupportedOperationException` for a token that is not valid.** So the flow is refused
  for the client, whatever the token: nothing about the token is judged.
- **A rotated-out refresh token, used after its grace period, answers
  `RefreshTokenReuseException`.** AWS: "thrown when your application requests token refresh
  with a refresh token that has been invalidated by refresh-token rotation." **AWS's pages
  say nothing of revoking anything else** (no token family, no session). The run showed it
  on the replica: the check that the new refresh token works ran straight after the reuse
  and passed. The checks now say so in words and add one: after the reuse, the new refresh
  token still refreshes and the access token issued before it is still accepted.
  - **Two tabs.** Tabs that signed in separately have their own tokens and never meet
    this. Tabs that share one refresh token (a duplicated or restored tab): whichever
    refreshes second, more than 10 seconds after the first, is sent to sign-in; the first
    stays signed in. Inside the 10 seconds the second tab's refresh is accepted.
  - Not checked: the same through `/oauth2/token`, which the portal uses (the checks call
    `GetTokensFromRefreshToken`); AWS's token endpoint page does not say which error a
    rotated-out token gets there. The portal does not depend on which: any answer from
    Cognito that refuses the refresh ends the session on the sign-in screen (see "Two tabs
    that share one refresh token" under "Known limits").
  - **Before 2026-11-27 the portal did not handle a refused refresh in an open tab.** The
    library raises `silentRenewError` and does not try again; nothing listened. The app
    stayed open, and a minute later, with the access token expired, every request went out
    without a token and each page showed its own error. A reload showed the sign-in screen
    with no reason. Both now end on the sign-in screen with the message.
- The old label "reached authentication" for an answer a check did not recognise was wrong:
  nothing shows that anything was authenticated. It now reads "not refused as a flow
  (`<code>`)", and still fails.

How it is built:

- **A switch per client in stack config**, read into `AuthConfig`: `"true"` (rotation on,
  `ALLOW_CUSTOM_AUTH` only), `"false"` (rotation sent as `DISABLED` and the refresh flow
  restored: the way back), or unset (the client is not mentioned, exactly as before
  rotation existed here). Both keys are `"true"` in the dev stack since 2026-11-27.
  `auth.explicit_auth_flows` never returns an empty list.
- **Rule: no pool may ever have a custom-auth trigger** (`DefineAuthChallenge`,
  `CreateAuthChallenge`, `VerifyAuthChallengeResponse`). That is what keeps
  `ALLOW_CUSTOM_AUTH` from being a sign-in. Held by `test_no_pool_has_a_custom_auth_trigger`,
  by a test that the three arguments appear nowhere in `auth.py` but the forbidden list,
  and by the isolation run on the deployed pools.
- **The isolation run** (`tests/isolation/cognito_clients.py`, which replaced the probe)
  now checks, against Cognito: each deployed client's flows and rotation are what the stack
  says (stack outputs `tenant_refresh_token_rotation`, `platform_refresh_token_rotation`);
  neither pool has a custom-auth trigger; each deployed client refuses all four programmatic
  sign-ins as "flow not enabled", asked for a user that does not exist (nobody is signed
  in, no trigger runs); on the replica, a refresh returns a new refresh token, the old one
  stops working after the grace period and `REFRESH_TOKEN_AUTH` is refused; and the way
  back is rehearsed on the replica (rotation on, then in one update rotation off with the
  refresh flow restored). `test_cognito_client_checks.py` shows these checks fail when what
  they guard is broken.
- **Preview for step 1 (2026-11-25):** 2 to update (the tenant app client: its flow
  `ALLOW_REFRESH_TOKEN_AUTH` becomes `ALLOW_CUSTOM_AUTH`, and `refreshTokenRotation`
  `ENABLED`, 10 seconds; the isolation function, for its rebuilt image), 1 to replace (that
  image's build), 360 unchanged. The platform client is not touched.
- **Rolling back** (if sign-in breaks): set `tenantRefreshTokenRotation: "false"` in
  `infra/Pulumi.dev.yaml` and run `pulumi up`. Only the app client changes, so it takes
  about a minute. Sessions already open keep their tokens.
- **Known effects of rotation:** a duplicated or restored tab shares its original's refresh
  token, and whichever refreshes second, more than 10 seconds after the first, is sent to
  sign-in with a message; the first stays signed in (recorded under "Known limits": "Two
  tabs that share one refresh token"). Sessions still end 8 hours after the sign-in. The portal's own refresh goes
  through `/oauth2/token`, which works with rotation on; nothing in the portal changes.
- **Checking the refresh in a browser** (owner, after step 1's deploy). The portal refreshes
  by itself about a minute before the access token's 60 minutes are up. To see one at
  once: sign in to the tenant portal, open the developer tools, and in Application,
  Session Storage, edit the `oidc.user:...` entry's `expires_at` to `1`; then reload the
  tab with the Network panel open. The portal finds the token expired and refreshes
  (`Root.tsx`). What shows it works: a `POST .../oauth2/token` with
  `grant_type=refresh_token` answers 200 and its response has a `refresh_token` (without
  rotation it has none); the entry's `refresh_token` is now a different value; the portal
  stays signed in. Do it twice: the second refresh uses the new token. Then sign out and
  check that the next sign-in asks for the password.

What it does:

- **Attempts.** `documents.attempt` (part 1) is now used everywhere. Every event carries the
  attempt it belongs to (`document_events.attempt`, stamped from the document's row when the
  event is written). The starter names an execution after the document and its attempt
  (`{document_id}` for attempt 1, `{document_id}-{n}` after) and puts the attempt in the
  execution's input; every Lambda step and the worker task do nothing unless it is still the
  document's current attempt. **A superseded attempt writes nothing** (tested for fingerprint,
  claim, companion, failed, and a late release).
- **"Process again"** (`POST /documents/{id}/reprocess`, tenant admins only; multi-select in
  the tenant's ingestion history, and a button in the document's panel).
  - Allowed for `failed`, `ready`, `needs_review`, and `processing` whose attempt has ended
    (no live worker grant and nothing recorded for the current attempt in the last 5
    minutes; `batches.RUNNING_GRACE`). Refused otherwise: `not_reprocessable` (quarantined,
    on hold, duplicate, discarded, or a document whose PDF never scanned clean),
    `attempt_running`. A suspended tenant is refused by the account check, as everywhere.
  - Every attempt restarts at fingerprint, from the upload at the version that scanned
    clean (inbox versions stay; known limit 10). The PDF is copied to `originals/` only by
    the first attempt that gets there; a later one finds it and writes nothing.
  - A `failed` document gives up its hash when it is processed again, so fingerprint
    checks it afresh: if the same file became an original in the meantime it ends
    `duplicate`.
  - The portal asks before processing again, and says how many of the selected documents
    are Ready ("runs the AI steps again, which costs money"; owner's condition).
- **Step 9, the companion file** (`assay_workers.companion_data`): plain code, never the
  model. The values are stored by version: `signers` (source `companion`),
  `companion_loan_terms` and `document_labels` rows carry `attempt`, and
  `documents.companion_attempt` points at the current version. The pointer moves in the
  transaction that writes the rows, so readers see the previous values until then. Nothing
  is deleted (owner decision 2). `documents.companion_file_id` is the upload the values
  came from. The tenant's document panel shows the values (`GET /documents/{id}`,
  `companion` and `companion_files`); **no master view shows any of them** (tested).
- **Companion attach or replace** (`POST /documents/{id}/companion`, tenant admins only):
  the file is registered against its document (`batch_files.purpose` =
  `companion_replacement`), uploaded with a presigned link to the inbox under an opaque
  file ID, and scanned like any upload. Clean and valid, it takes effect at once; invalid
  or infected, the document is unchanged. One at a time (`companion_pending`).
- **The quarantine notice.** A second SNS topic, "Assay Insights security notices"
  (`alerts.SecurityNotices`), with the alarm topic's key (no new KMS key, so
  `platform_key_arns` is unchanged). The scan function reads GuardDuty's threat name from
  the event (`detail.scanResultDetails.threats[0].name`; kept only if it is a plain
  printable name of at most 200 characters), writes an audit row (category `security`,
  action `file_quarantined`) in the scan result's transaction, and publishes the notice
  after the commit. Both name the tenant, the uploading user, the time, the batch and
  document IDs and the threat; **never a file name or content** (tested). The quarantine
  key and the source version ID are in the audit row and in an INFO log line, so the copy
  can now be confirmed from logs. Nothing is logged at ERROR, so `scan-errors` stays quiet.
  A publish that fails raises: Lambda retries the event, the retry finds the file infected
  with `notice_sent_at` unset and sends it; after its retries the event is in the scan
  dead-letter queue (alarm). While `securityNoticeEmail` is blank the topic has no
  subscription.
- **"Mark as reviewed"** (`POST /documents/{id}/reviewed`, tenant admins only):
  `needs_review` becomes `ready`. No document can be `needs_review` before stage 3, so the
  button cannot appear until then; the route and its tests are in place.
- **Audit rows** (category `document`, tenant admin as the actor, IP address and browser,
  no file name): `reprocess_requested`, `marked_reviewed`, `companion_replace_requested`
  (the request), and `companion_replaced` or `companion_attached` (written by the pipeline
  when the file takes effect, with the uploading user as the actor): `before` and `after`
  name both files by ID and inbox version, and `after.stored_version_id` is the new
  version of `originals/{document}.companion.json`.

Decisions made while building. The owner confirmed 1, 2 and 3 on 2026-11-25, and decided
that rotation (4) stays off on the real clients until they have read the probe's output:

1. **Step 9 runs in the steps Lambda, after the worker task, not inside it.** Section 8
   lists it among the worker's steps. It is plain JSON (the scan function already reads and
   validates the same file), it needs no sandbox, and running it as a Lambda step needs no
   new grant for `app_worker`. The worker task no longer copies the companion file; the
   `companion` step does. Stage 4 must decide where the final status is set, since step 9
   now follows the task.
2. **A replacement companion takes effect in the scan function**, in the transaction that
   records its clean result, not through a "companion-only" execution as the plan said. Same
   outcome (no task, no AI call), with no extra queue message, execution name or sweeper
   rule; a failure is retried with the scan event and then alarmed by its dead-letter queue.
3. **Audit category `document`** is new (the plan named only `security`). Audit history's
   category filter has both.
4. **Refresh-token rotation was not switched on with part 2** (superseded: it is on for
   both clients since 2026-11-27; see "Rotation: the owner's decision" above). AWS's pages
   (read 2026-11-25) say rotation "isn't compatible with" `REFRESH_TOKEN_AUTH` and that a
   client with no explicit flows gets `ALLOW_REFRESH_TOKEN_AUTH`, `ALLOW_USER_SRP_AUTH` and
   `ALLOW_CUSTOM_AUTH` by default; they do not say what a client with rotation on and an
   empty flow list ends up with. Every flow other than refresh is a programmatic sign-in,
   which the portal clients must not allow. So, as the plan said, the replica goes first:
   `run_isolation.py` now prints a **rotation probe** (replica pool; not pass or fail):
   for three candidate settings, what Cognito stored and how it answered each kind of
   programmatic sign-in. The owner's decision on the real clients follows that output.
5. **Later-stage columns are not all here.** `documents.content_attempt` exists (NULL). The
   `attempt` column for `pages`, `document_parts`, `signature_check`, `signing_events` and
   `extracted_fields`, and the master view's count of current parts
   (`platform_view_owner` reading `content_attempt` and `document_parts.attempt`), arrive
   with the stages that first write those tables.

Migration `0011` (see its docstring):

- `document_events.attempt`; `attempt` on `signers`, `companion_loan_terms`,
  `document_labels`, with their unique rules now per attempt; `documents.companion_attempt`,
  `content_attempt`, `companion_file_id`; `batch_files.purpose`, `applied_at`,
  `notice_sent_at`, and the one-live-file-per-name index limited to batch uploads; audit
  categories `security` and `document`; `pipeline.tenants_with_work` replaced (a document
  is started only if its current attempt has a `start` event).
- **Grants: two, each one integer column** (approved with the plan, decision 5):
  `pipeline_lookup` and `app_worker` may read `document_events.attempt`. No policy, role or
  table changes. `app_tenant` already had table-level access to every table touched.
- A downgrade deletes nothing (`migrator` sees no rows). If replacement files or a second
  version of a document's companion data exist, the database refuses it.

New permissions (all approved with the plan, decisions 4 and 5; also in the pull request):

- **`sns` interface endpoint** in the VPC, with a policy that allows one role (the scan
  function's), one action (`sns:Publish`) and one topic.
- **Scan function's role:** `sns:Publish` on the security-notice topic; `kms:GenerateDataKey*`
  and `kms:Decrypt` on the alarm topic's key, only through SNS (`kms:ViaService`).
- **The topic's policy** has no Allow: it denies `sns:Publish` to every principal but the
  scan function's role, and any call without TLS. The role is named in a condition, so the
  policy needs no IAM propagation wait.
- No change to the tenant role, any bucket, key or vector bucket policy, the TSS, any
  queue, or any other endpoint policy. No `update_tenants.py` run.

Deploy order (owner): deploy when no document is in progress (an execution started before
the deploy keeps the old definition, without the attempt). `pulumi up`; then
`uv run scripts/migrate.py` **straight away** (until `0011` is in, every pipeline step that
writes an event fails, so upload nothing in between); `uv run scripts/run_isolation.py ...`
(read the rotation probe); browser checks; `uv run scripts/check_deployment.py`. If
`securityNoticeEmail` is set, follow SNS's confirmation link within 48 hours.

What the preview showed (2026-11-25): 6 to create (the `sns` endpoint, the topic, its
policy, its email subscription, the endpoint policy, the component), 16 to update, 12 to
replace (every image, the three task definitions and the portal build), 329 unchanged. The
steps, starter and sweeper role policies show as updated only because their text is built
from the topic's ARN, which is unknown until the topic exists; their content is unchanged
(tested: only the scan function's policy has the two new statements).

**Verified in AWS by the owner on 2026-11-25** (Infinity Loans):

- "Process again", one document and 13 selected at once (so a second execution of one
  document, `{document_id}-2`, works);
- a companion replacement taking effect;
- rejected replacements (a file that is not JSON, and one with a field error) leaving the
  document unchanged;
- an EICAR quarantine with its security notice: the threat name is in the real scan event,
  and the notice has no file name and no content;
- audit rows for all of them; idle-timeout sign-outs and audited ingestion exports too.

**Deployed by the owner on 2026-11-27 as stack update 39** (commit 3f2a073), in 1 minute 7
seconds, first time: the three fixes, rotation step 1, the reserved concurrency, the build
change and the two-layer images. Its preview: **13 to update, 12 to replace, 338
unchanged**, and two new outputs.

- Update: the tenant app client (`ALLOW_CUSTOM_AUTH`, rotation `ENABLED`, 10 seconds);
  `assay-dev-pipeline-scan` (`reservedConcurrentExecutions` -1 to 50, and its image);
  `assay-dev-tss` (-1 to 300, and its image); seven more functions for their images
  (`pipeline-steps`, `-starter`, `-sweeper`, `db-migrate`, `auth-events-consumer`, `seed`,
  `isolation-tests`); the API service; the pipeline and provisioning state machines (each
  definition names a task definition that is replaced).
- Replace: eight image builds (every image that installs dependencies: `api`, `ops`,
  `db-migrate`, `tss`, `provision`, `pipeline`, `pipeline-worker`, `isolation-tests`); the
  API, worker and provisioning task definitions; the portal build.
- No role, policy, endpoint, queue, topic or database change, and no migration. The
  platform app client and the three Cognito trigger images are untouched.
- Deploy order: `pulumi up`; `uv run scripts/run_isolation.py ...`; browser checks (the
  tenant client's refresh among them); `uv run scripts/check_deployment.py`, which must
  now show both reservations as the stack says.

Three things the owner found, fixed the same day (deployed 2026-11-27; no migration, no
grant, no permission change):

1. **A replacement that did not take effect left only its request in the audit trail.** Now
   every `companion_replace_requested` row is followed by exactly one row that says how it
   ended (`companion_data.audit_not_applied` and `ending_action`; category `document`, the
   uploading user as the actor, `reported_by: pipeline`), written in the transaction that
   records the ending:
   - `companion_replaced` or `companion_attached`: it took effect;
   - **`companion_replace_rejected`: only when the file was checked and refused** (owner,
     2026-11-25). File status `invalid` (`companion_invalid`), `infected`
     (`threats_found`; it also has its `file_quarantined` row, category `security`) or
     `mismatch` (what was stored is not what was registered: `size_mismatch`,
     `checksum_mismatch`, `version_missing`);
   - **`companion_replace_expired`: the file never arrived or was never scanned**, with the
     reason for which: `not_uploaded`, `scan_timeout` (both from the sweeper), or
     GuardDuty's own "could not scan" results (`scan_unsupported`, `scan_failed`,
     `scan_access_denied`; from the scan function).
   - Two placements the owner confirmed on 2026-11-27: `mismatch` is rejected (it was
     checked against its registration and refused), and GuardDuty's "could not scan" is
     expired (it was never scanned).
   - **The row holds codes and IDs only**: `reason`, `problems` (each problem's
     `error_code`, and its `key` only if it is one of the format's reserved keys, in lower
     case), `file` and `kept` (file IDs and inbox versions). **A custom key is never
     named**: it is the tenant's own wording, and master roles read audit rows
     (`assay_schemas.companion.audit_problems`). No file name and no value.
   - **Never name a detail key `code`**: Audit history redacts any key ending in `code`
     except `error_code`, `status_code` and `country_code` (`audit_read.is_secret_key`). The
     test checks that `redact()` leaves this row as written.
   - A new file status must be given one of the two actions:
     `test_only_a_file_that_was_checked_and_refused_is_rejected` fails until it is.
2. **"Companion file uploads" showed stored codes** (`not_json`,
   `LoanInterestRate: must_be_a_number`). It now uses the Upload screen's wording
   (`companionErrorLines`, moved to `ingestionFormat.ts` so both screens share it; a code
   with no wording has its underscores replaced, never shown raw). The rendered check has
   a screen for it (`tenant-admin-ingestion-companion-uploads`: the panel scrolled to the
   table, with two rejected uploads in the fixtures), which fails if a stored code shows.
3. **Duration was measured from the upload, also for a document processed again**
   (`06-not-a-pdf.pdf`: 55,492 seconds). **One definition now, everywhere** (owner decision,
   2026-11-25, the one that needs no grant): from the document's latest `queued` event to
   `completed_at` (`ingestion_read._started`).
   - That event is written when a document becomes eligible for the pipeline (its batch
     closed, its files scanned clean) and again by every "Process again", so the latest
     one is where the current attempt began.
   - **A first attempt's duration therefore leaves out the upload, the scan and the time
     its batch stayed open.** Durations of documents already in the history get shorter by
     that much.
   - A document that was never queued (held at its scan, quarantined, failed before the
     pipeline) is timed from `received_at`.
   - The tenant view, the master view, both CSV exports and both Duration sorts use it.
     The master view reads the events through `platform.ingestion_events_v`, which
     `platform_reader` could already read: no migration, no grant.
   - `test_every_view_and_both_csv_exports_show_the_same_duration` reads the number in
     seven places (the tenant table, panel and CSV; the master table and CSV, for both
     master roles) and fails, naming them, if any two differ.
   - The tenant panel's label says which attempt ("Duration (attempt 2)").

Portal (owner, 2026-11-25, before the deploy):

- **The File name column of the tenant's ingestion history** has a minimum width (200 px),
  shows each name on one line, and cuts a long one with an ellipsis; the full name is the
  cell's tooltip and the panel's title. With the selection checkboxes added, names had
  wrapped into many short lines at 1280 px. The rendered check now has `checkFileNames`
  (both tenant ingestion screens, both widths): one line, at least 180 px, the tooltip is
  the full name, the fixtures' long name is cut and a short one is whole.
- **To make the room, the Companion file column is hidden by default** at every width (it
  is in the row's panel, and Preferences shows it), as Size and Duration already are on
  narrower windows. The page's content stops at about 1280 px however wide the window is,
  so a wide window has only about 270 px more than a 1280 px one, and it also shows Size
  and Duration. A browser that already saved its column preferences keeps them.
- No new screen, so `SCREENS` has no new entry; the document panel's fixture gained the
  companion values and uploads (long values, to check they wrap).

For later stages:

- Add each new step to `PIPELINE_STEPS`, `progress.WORKER_STEPS` (worker steps), the
  portal's `STEP_TEXT` and `RESULT_TEXT`, and give it a Pass state. `companion` is there.
- A step that asks "did I already run?" must ask about its own attempt's events.
- Derived rows carry an attempt and are read through the document's pointer; nothing is
  deleted.
- The worker's session policy still names the companion key for writing; the task no
  longer writes it. Narrowing it is a change to the TSS (no tenant stack run) for a later
  stage, or part of stage 2's worker changes.
