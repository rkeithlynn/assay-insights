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
    `action.yml` at that SHA.
- Run the checks locally with `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `uv run pytest`.
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
