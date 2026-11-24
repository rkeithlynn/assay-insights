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
| 5. Portal shell, sign-in, Tenants and Users screens, `scripts/seed.py` | **First half deployed and verified** (2026-11-23). **Second half (Audit history, Models) deployed**, migration `0005` applied, checked in a browser by the owner (2026-11-24). **Audit-trail follow-ups** (session start, first sign-in label, MFA enrolment, migration `0006`) built on `build-step-5`; waiting for the owner's `pulumi up` and `migrate.py`, then the checks under "Audit-trail follow-ups" below |
| 6–9 | Not started |

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
  it commits before answering 204.

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

- **A sign-out from a session already ended by an MFA reset or a disable is not recorded**:
  the account check refuses the token (`session_revoked`), so `POST /auth/signed-out` answers
  403. The reset or disable that ended the session is recorded.

- **A suspended tenant's users can still complete managed login** (D10). Cognito is not
  changed when a tenant is suspended; every API request is refused with `tenant_suspended`
  and the portal says so. Reactivating needs no Cognito change either.
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
  attached to it. Check both again before any content first goes to Bedrock, Textract or
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
  `uv run mypy` and `uv run pytest`.
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
  `derived/text/00000000-0000-4000-8000-0000000000f1.json` (`FIXTURE_DOCUMENT` in
  `tests/isolation/conftest.py`). It is outside `inbox/`, so no upload or scan event ever
  refers to it, but it sits under `derived/text/`, which the pipeline writes. That document
  ID is reserved: it has no `documents` row and must never get one. The pipeline must work
  only from database rows and scan events, never from a listing of `derived/` or any other
  prefix, so it never treats this object as a document's text. Owner's condition
  (2026-11-23): the fixture is never in the inbox, and the pipeline ignores it. The owner
  decided to keep it at this key. Step 6 must also do the following:
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
    the managed login session. It does not end the session of another tab still open: at
    04:45:07 such a tab refreshed its token (`Token_POST` with a refresh token) after a
    sign-out in another tab.
- **`temporary_password_accepted`** (item 2). The trigger sends it instead of `sign_in` when
  the event's `cognito:user_status` is `FORCE_CHANGE_PASSWORD`, and logs the status it saw
  (`auth event queued`, `user_status`), never queues it. Any other status, or none, stays
  `sign_in`. Migration `0006` replaces the body of `auth_events.record()` to accept it, with
  the sign-in's effect on `users` (the platform pool's first sign-in sends nothing else). It
  switches to `auth_event_recorder` for the `CREATE OR REPLACE`, because `migrator` holds
  SET but not INHERIT on that role and so cannot replace a function it does not own; owner,
  grants and search_path are unchanged.
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
