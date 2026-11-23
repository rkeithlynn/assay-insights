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
| 4. Tenant Storage Service, tenant provisioning, `tests/isolation` | **Built, not yet deployed** (2026-11-23, branch `build-step-4`) |
| 5–9 | Not started |

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

Step 4 was built on 2026-11-23 and passes its unit tests. It is complete only when the owner
has done all of this, in order, and each has succeeded:

1. `pulumi up`, then `uv run scripts/migrate.py` (revision `0003`). The owner follows the
   confirmation link SNS emails to `alarmEmail`, or no alarm is delivered.
2. `uv run scripts/add_tenant.py` twice, for the fixture tenants "Isolation Test A" and
   "Isolation Test B". Each must end `SUCCEEDED`, and each admin must get an invitation.
3. `uv run scripts/run_isolation.py` with the two tenant IDs: every test passes.
4. `pulumi preview` shows no changes.

The first real provisioning run is also the first test of several things no unit test can
reach; they are listed under "Step 4" in the notes below.

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
- **Rule 6.** On 2026-11-23 the organization had not enabled the AI services opt-out policy
  type, and Bedrock model invocation logging was off. The owner applies the opt-out policy
  from the management account before build step 6. Check it before any content goes to
  Bedrock, Textract or Rekognition:
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
- **Not in step 4:** `POST /admin/tenants` and the retry route (step 5), default models
  for a new tenant (step 5, with the catalog seed), bucket CORS for browser uploads and
  `DetectFaces`/`CompareFaces` in the tenant role (steps 6 and 8), the cross-tenant face
  search test (step 8).
- **For step 6 (uploads): no S3 key may contain a file name.** Register each uploaded file
  with a `file_id`, store it at `inbox/{batch_id}/{file_id}`
  (`StorageContext.inbox_key(batch_id, file_id)`) and keep `file_name` only in the
  database. Pair companion files and stipulations with their PDF by the names in the
  database, not by listing keys. BUILD_SPEC.md section 6 has the layout.
- **Unverified until the first real provisioning run.** Each of these is an assumption the
  first `add_tenant.py` run tests. If one is wrong, provisioning fails at the `storage`
  step with the reason in `audit_log`:
  1. GuardDuty accepts the scan role's trust policy with the `aws:SourceAccount` condition
     (AWS's documented trust policy has no condition).
  2. S3 Vectors needs only `kms:GenerateDataKey` of the provisioning role to create the
     vector bucket and index (documented for the bucket, not stated for the index).
  3. `s3:CreateBucket` passes the S3 gateway endpoint's `aws:ResourceAccount` condition.
  4. `kms:CreateKey` with tags passes the `CreateTenantKeys` conditions
     (`aws:RequestTag/tenant_id` present, `aws:ResourceTag/app` absent).
  5. `guardduty:CreateMalwareProtectionPlan` needs nothing beyond that action and
     `iam:PassRole`.
  6. The task definition's container definition round-trips unchanged, so the next
     `pulumi preview` shows no diff.
  7. S3 Vectors accepts the vector bucket policy's `Principal: "*"` and `Condition`
     elements. If it rejects them, tell the owner before changing the policy: admitting
     only two roles cannot be written without one of them.
  `run_isolation.py` then checks the result: a principal whose IAM policy allows it is
  still refused when it lists a tenant's vectors.
