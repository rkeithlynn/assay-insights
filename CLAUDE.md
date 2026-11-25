# CLAUDE.md

`BUILD_SPEC.md` is the contract for this build. Read the relevant sections before any
change. Where this file and the spec differ, the spec wins.

## Working rules

- **AWS:** profile `pilar-dev`, Region `us-east-1`.
- **Pulumi:** organization `pilartech`, project `assay-platform`, stack `dev`
  (`pilartech/assay-platform/dev`). The program lives in `infra/`.
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
| 6. Batches and upload, malware scan routing, pipeline steps 4 to 10, ingestion history | **First half complete** (2026-11-24), deployed and verified, on `build-step-6`. **Second half:** plan approved (2026-11-24); stage 1 part 1 (worker task, attempt grant, sandbox, `app_worker`) **deployed** 2026-11-25; isolation run and browser checks passed (2026-11-25), with one throttled TSS call that Lambda's retry recovered (the account allowed 10 concurrent Lambda executions; increase to 1,000 requested). Part 2 not started: the TSS API's empty access log is to be settled first. See "Step 6, second half" |
| 7–9 | Not started |

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

- **Rule 6, before the second half:** re-check the AI services opt-out policy and that Bedrock
  invocation logging is off before any content first goes to Bedrock or Textract (checked on
  2026-11-24 for the plan: `optOut` for every service, logging off). The first half sends no
  content to any AI service.
- **The second-half plan** (owner, 2026-11-24) is approved; its decisions are under "Step 6,
  second half". What it had to include, for the record:
  - **The worker (owner decision 8, deferred):** compare the always-on worker with Step
    Functions starting one ECS task per document (`runTask.sync`): cost, start-up delay,
    complexity and every permission each needs. Do not build it before the owner chooses.
  - **Reprocessing:** how a document is processed again (for extraction changes, and for a
    `failed` document, which today can only be uploaded again; execution names are document
    IDs and Step Functions keeps them 90 days), with attempt numbers.
  - **Attaching or replacing a companion file after ingestion** (A4), as part of
    reprocessing.
  - **Quarantine notice.** When a file is quarantined, send a security notice to the
    platform alert topic and write an audit row. The notice names the tenant, the uploading
    user, the time, the batch and document IDs and GuardDuty's threat name. It never carries
    the file name or any content. Word it as a security notice, not a system failure, and
    keep it apart from the pipeline failure alarms (its own sender, subject and wording; no
    `ERROR` line, so the `-errors` alarms stay quiet). The threat name is not in the scan
    result event the scan Lambda receives today; the plan must say where it comes from (the
    GuardDuty finding, for example) and what permission that needs. Also log, and keep in
    the audit row, the quarantine key and the source version ID, so the copy can be
    confirmed from logs (today it cannot; see "Quarantine evidence").
  - **Cognito refresh-token rotation** on both app clients, as assessed (see "Cognito
    refresh-token rotation" in the step 6 notes). It removes `ALLOW_REFRESH_TOKEN_AUTH` from
    both clients (`explicit_auth_flows` in `infra/assay_infra/auth.py`). The replica client in
    `tests/isolation/cognito_clients.py` sets `ExplicitAuthFlows` itself and must follow.
- **Titan Text Embeddings V2 (owner decisions 1 and 2):** called by model ID in us-east-1 (it
  has no `us.` profile; a named exception to section 2), with an `ai_models` row (job
  `embed`, migration `0010`; `0009` is the master view's `error_type`) and its price left blank for the owner. Arrives with the second
  half, when the catalog changes; `seed.py` refuses a catalog with a blank price.
- **Remove the "later steps are not available yet" note** (`laterStepsPending` in
  `portal/src/pages/ingestionFormat.ts` and its Alert in `Steps`, both marked
  `TODO(build step 6, second half)`) once steps 5 to 10 exist. Until then a document that
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
- **Before deploying to Staging or Production (owner, 2026-11-25): request the Lambda
  concurrent-executions quota increase in that account first.** A new account allows 10
  concurrent executions (Service Quotas L-B99A9384), which is too few for the pipeline: see
  "One scan result failed once" in the step 6 notes. Request 1,000, as for Development:
  `aws service-quotas request-service-quota-increase --service-code lambda --quota-code L-B99A9384 --desired-value 1000 --profile <profile> --region us-east-1`,
  and wait until it is granted before the first `pulumi up`.

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

| Stage | Builds | Content to AI services |
| --- | --- | --- |
| 1, part 1 | The worker task, the attempt grant, the PDF sandbox, `app_worker`, migration `0010` | None |
| 1, part 2 | Attempts and "Process again", step 9 (companion), companion attach and replace, the quarantine notice, refresh-token rotation, migration `0011` | None |
| 2 | Step 5: text layer, Textract for pages that fail the quality score | Textract |
| 3 | Steps 6, 7, 8: split, signer details, `pyHanko`, field extraction | Bedrock (Claude) |
| 4 | Step 10: chunk, embed, index; final statuses; Titan catalog row (migration `0012`) | Bedrock (Titan) |

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
  the owner the output. (Run on 2026-11-25 03:58 UTC for the plan: `optOut` for every
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
creates `app_worker`, then `0010`); `uv run scripts/run_isolation.py ...`; browser checks.
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
  Once it is granted, propose the scan function's concurrency cap and the TSS's reserved
  concurrency to the owner, with the reasoning for the numbers. **Set neither before then**
  (owner, 2026-11-25). Staging and Production need the same request before their first
  deploy (see "Before deploying to Staging or Production").
- **Found on the way: the TSS API's access log has never received an entry.** The stage has
  access logging set to `/aws/apigateway/assay-dev-tss`, and that log group has no stream
  and 0 bytes since build step 4. So API Gateway's own record of the 500 (caller,
  integration status) does not exist. Not yet investigated.

Still not known:

- How a RunTask refused for the vCPU quota reaches Step Functions. The design does not
  depend on it: any failed task with no `worker`/`running` event is waited out.
- Whether GuardDuty holds every kind of password-protected PDF at the scan. It held both
  kinds tried (40-bit and 128-bit RC4); AES-encrypted PDFs were not tried.
- Why the TSS API's access log is empty.

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

