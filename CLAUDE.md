# CLAUDE.md

`BUILD_SPEC.md` is the contract for this build. Read the relevant sections before any
change. Where this file and the spec differ, the spec wins.

## Working rules

- **AWS:** profile `pilar-dev`, Region `us-east-1`.
- **Pulumi:** organization `pilartech`, project `assay-platform`, stack `dev`
  (`pilartech/assay-platform/dev`). The program lives in `infra/`.
- **One build step at a time.** Work on exactly one step from BUILD_SPEC.md section 12.
  Stop when it is done and report back. Do not start the next step unasked.
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
| 2. Network, database, migrations, database roles and RLS | **Built, awaiting `pulumi up`** (2026-11-23). Then `uv run scripts/migrate.py` and verification |
| 3–9 | Not started |

Step 1 was verified on 2026-11-23:

- The hosted zone `dev.assayinsights.ai` is delegated from Squarespace to its four
  Route 53 name servers (`pulumi stack output name_servers`).
- The ACM certificate for `dev.assayinsights.ai` and `admin.dev.assayinsights.ai` is
  `ISSUED`. It replaced the first certificate, which failed with `CAA_ERROR`.
- `pulumi preview` on the `dev` stack shows 6 unchanged.
- CI passes on `main`.

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
  as workspace members.
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
  The `cognito-idp` endpoint service is offered only in `use1-az2`, `use1-az4` and
  `use1-az6`; S3 Vectors is not offered in `use1-az3`. Check both before changing it.
- **Migrations run only in the migration Lambda**, started by `uv run scripts/migrate.py`
  after `pulumi up` (and monthly by EventBridge Scheduler for `audit_log` partitions).
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
