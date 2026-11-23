# Assay Insights: first deployment build spec

Build spec for the first deployment of Assay Insights, a multi-tenant AWS SaaS that lets
lenders query archives of signed PDF loan documents through AI chat. This file is the
contract for the build. The full architecture document holds the reasoning and the later
phases; where the two differ, this file wins for the first deployment.

Status of decisions: all confirmed by the owner (Keith Lynn) on 2026-11-21.

## 1. Goal and scope

Deploy a working development environment at `dev.assayinsights.ai` that supports seven
use cases for three tenants, with tenant isolation enforced by AWS credentials and
database row-level security, not by application code alone.

In scope:

1. Any user can sign in, and reset or change their own password.
2. A tenant admin can invite and maintain users.
3. A master admin can add tenants.
4. A tenant admin can upload PDFs, each with an optional companion JSON file, inside a
   batch that is opened and closed.
5. Tenant admins and tenant users can ask AI questions and get answers with page citations.
6. Tenant admins and tenant users can view their tenant's ingestion history in a sortable,
   filterable table, including a date filter.
7. Master admins and master users can view ingestion history across tenants, filtered by
   date and tenant, and an audit history of sign-ins, sign-outs, user changes and
   tenant changes.
8. A tenant admin can upload stipulations (photos and files) tied to a primary PDF.
9. Underwriters (tenant admins and tenant users) can see an identity photo score and a
   stipulation authenticity score for each document, with reasons, and record a review.
   This is a first version, labelled experimental. Tuning and measuring accuracy is not
   part of this deployment.

Out of scope (do not build, but do not block): bulk import (SFTP, DataSync), pooled
storage for consumer tenants, tenant custom field definitions, standard reports, a review
queue screen, text-to-SQL, linking PDFs to applications, vendor-specific audit-trail
parsers, tenant deletion, billing, cross-Region recovery,
SES, IAM Identity Center federation for the portal, CI deploy pipeline.

Data: test documents only. The environment is outside SOC 2 scope for now, but build
every control as if it were in scope.

## 2. Fixed facts

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

### Development environment

Development happens on Intel (x86-64) Windows 11 Professional, in VS Code with
PowerShell, Docker Desktop on WSL 2, and `pulumi up` run by hand.

- Build every container image for `linux/amd64` and set Fargate tasks to `X86_64` and
  Lambda functions to `x86_64`. Do not use ARM (Graviton) anywhere.
- Never package Lambda code or native dependencies (`pypdfium2`, `psycopg`, `pyHanko`)
  from the Windows host. Build them inside a Linux container, or ship those functions
  as container images.
- Pin Python to the newest version that AWS Lambda supports and that every native
  dependency publishes Linux x86-64 builds for; prefer one release behind the latest.
  Use the same version in the container images, and manage it with `uv`. Do not rely
  on the Python installed on the machine.
- Give commands for the developer in PowerShell, one line each, not bash. Scripts the
  developer runs are Python (`uv run ...`), not shell scripts.
- Add a `.gitattributes` that forces LF line endings for Dockerfiles, shell scripts
  used inside containers, and Python files.
- Use `pathlib` and forward slashes; never hard-code Windows or POSIX paths.

Never put model IDs, account IDs, hostnames or email addresses in code. Read them from
Pulumi config or the `ai_models` table.

## 3. Repository layout

```
infra/      Pulumi program (Python): network, data, auth, compute, edge, pipeline
api/        FastAPI app (ECS Fargate)
workers/    Pipeline code: Lambda handlers and the Fargate worker
tss/        Tenant Storage Service (Lambda) and its client package
schemas/    Pydantic v2 models shared by api/ and workers/
portal/     React + TypeScript + Vite + Cloudscape single-page app
scripts/    seed.py and operational scripts
tests/      Unit tests, plus tests/isolation (runs against real AWS)
assets/     assay-insights-logo.svg, assay-insights-mark.svg
```

GitHub Actions runs lint, type checks, unit tests, `pip-audit` and Bandit on pull
requests. It does not deploy.

## 4. Infrastructure (Pulumi)

Build in this order. Tag every resource `app=assay-insights`, `env=dev`.

1. **DNS and certificates.** Hosted zone `dev.assayinsights.ai`; ACM certificate in
   `us-east-1` covering both hosts, DNS-validated. Export the zone's name servers.
2. **Network.** One VPC, no public subnets, private app and data subnets in two
   Availability Zones (the load balancer and Aurora need two; run workloads in one).
   An internet gateway must be attached for CloudFront VPC origins, with no routes to it.
   S3 gateway endpoint plus interface endpoints, in one zone, for every AWS API the code
   calls from inside the VPC: Bedrock runtime, Textract, KMS, STS, S3 Vectors, Rekognition, ECR (api
   and dkr), CloudWatch Logs, Secrets Manager, SQS. Add others only when a call needs
   them. Workloads have no route to the internet.
3. **Database.** Aurora PostgreSQL Serverless v2, one writer, minimum 0 ACU with
   auto-pause, encrypted with a customer-managed KMS key, credentials in Secrets Manager.
   Database roles are listed in section 7.
4. **Auth.** Two Cognito user pools on the Essentials plan.
   - Tenant pool: groups `tenant-admin`, `tenant-user`; custom attribute
     `custom:tenant_id`, writable only by the API's role, never by the app client.
   - Platform pool: groups `master-admin`, `master-user`; TOTP MFA required.
   - Tenant pool MFA: TOTP, optional at pool level; the API refuses `tenant-admin`
     requests from sessions without MFA.
   - Managed login on both pools, branded with the logo; authorization code flow with
     PKCE; public app clients with no secret. Access token 60 minutes, refresh 8 hours.
   - Self-service password reset on both pools: account recovery by verified email
     only. "Forgot password?" on managed login emails a code; a new password plus the
     code completes it, and MFA is still required at the next sign-in. Users in
     `FORCE_CHANGE_PASSWORD` (invited, never signed in) cannot reset; their admin
     resends the invitation.
   - MFA reset is never self-service. A tenant admin resets MFA for tenant users in
     their tenant; the master admin resets it for tenant admins and the master user. A
     reset clears the user's TOTP device, signs them out everywhere and writes an
     `audit_log` row; the next sign-in sets up MFA again where required. The
     master admin's own MFA is reset in the Cognito console by an AWS administrator.
   - Self sign-up off. Temporary passwords expire in 7 days.
   - Email: both pools use Cognito's built-in sender (`COGNITO_DEFAULT`), which sends
     invitations and password-reset codes without SES. It is limited to 50 messages a
     day and sends from a generic AWS address. The application sends no email of its
     own in this deployment. Keep the email configuration in one Pulumi component so a
     later switch to SES is a config change.
5. **Tenant Storage Service (TSS).** Section 6.
6. **API.** FastAPI on ECS Fargate (one task, 0.5 vCPU, 1 GB) behind an internal
   Application Load Balancer.
7. **Edge.** One S3 bucket for portal files; one CloudFront distribution serving both
   hosts: default behaviour to the portal bucket (origin access control), `/api/*` to
   the load balancer through a VPC origin with caching disabled. AWS WAF web ACL on
   CloudFront with the AWS managed common and known-bad-inputs rule groups and a rate
   limit. No IP allow-list yet.
8. **Pipeline.** Section 8.
9. **Observability.** Structured JSON logs to CloudWatch with 30-day retention; alarms
   on dead-letter queue depth, Step Functions failures and API 5xx rate.

The API has no internet route, so it cannot fetch Cognito's signing keys at runtime
unless a Cognito endpoint is reachable. First check whether a VPC endpoint for Cognito
user pools is available in `us-east-1`. If not, bake the JWKS into the task at deploy
time and run Cognito admin calls (invite, disable, reset) in a Lambda outside the VPC
that the API invokes.

## 5. Tenants, roles and seed data

| Tenant | Kind | User | Role |
| --- | --- | --- | --- |
| Assay Insights, LLC | `platform` | keith.lynn@pilartech.com | `master-admin` |
| Assay Insights, LLC | `platform` | robert@pilartech.com | `master-user` |
| Global Finance, Inc | `business` | robert@ugafamily.com | `tenant-admin` |
| Global Finance, Inc | `business` | robertreg@ugafamily.com | `tenant-user` |
| Infinity Loans | `business` | me@keithlynn.com | `tenant-admin` |
| Infinity Loans | `business` | keith@keithlynn.com | `tenant-user` |

`scripts/seed.py` is idempotent. It creates the platform tenant and its two users
directly, then creates the two business tenants by calling the same add-tenant path the
portal uses. Emails come from stack config. Display names default to the email address.

| Capability | master-admin | master-user | tenant-admin | tenant-user |
| --- | --- | --- | --- | --- |
| List tenants | Yes | Yes | No | No |
| Add, suspend, reactivate tenants; retry provisioning | Yes | No | No | No |
| Ingestion history across tenants (metadata only) | Yes | Yes | No | No |
| Audit history across tenants | Yes | Yes | No | No |
| Set which models are available to a tenant | Yes | View | No | No |
| Select the tenant's current ingest and AI query models | No | No | Own tenant | No |
| Invite and maintain users | No | No | Own tenant | No |
| Reset MFA for tenant users | No | No | Own tenant | No |
| List a tenant's admins | Yes | Yes | No | No |
| Reset MFA for tenant admins and the master user | Yes | No | No | No |
| Open, fill, close and discard batches | No | No | Own tenant | No |
| Ingestion history, document detail, view PDF | No | No | Own tenant | Own tenant |
| Ask AI questions | No | No | Own tenant | Own tenant |
| See fraud scores and reasons; record a review | No | No | Own tenant | Own tenant |

Master roles never see tenant content: no file names, no page text, no answers, no PDFs,
no fraud scores.

## 6. Tenant isolation and the Tenant Storage Service

These rules are not negotiable. A change that weakens one must be rejected.

1. Each business tenant gets its own S3 bucket (`assay-dev-t-{8 hex}`; never the
   customer's name), its own customer-managed KMS key, and its own S3 Vectors bucket and
   index. Bucket settings: Block Public Access, SSE-KMS with the tenant key and Bucket
   Keys on, versioning, Object Lock in governance mode with 1-day default retention,
   GuardDuty Malware Protection for S3 enabled, bucket policy denying every principal
   except the tenant role and a break-glass role.
2. Only the TSS may assume the tenant role. It passes session tags `tenant_id`, `bucket`
   and `vector_bucket`, a source identity naming the calling service and job, and an
   inline session policy listing the exact bucket, key, index and Rekognition collection. Credentials last 15
   minutes. The role's policy is one template built from `${aws:PrincipalTag/...}`.
3. No other code builds an S3 key or picks a KMS key. Callers ask the TSS for a storage
   context and build object keys only through it.
4. The tenant ID comes only from the verified token or a pipeline job message, never
   from a request body, query string or model output.
5. Every tenant table has a `tenant_id` column and a forced row-level security policy.
   The app sets `SET LOCAL app.tenant_id` at the start of every transaction.
6. Model invocation logging stays off. Apply the AI services opt-out policy if it isn't
   already applied by the organization.

TSS: a Python Lambda behind a private API Gateway endpoint with IAM authorization.

```python
class TenantStorageService(Protocol):
    def resolve(self, tenant_id: UUID, access: StorageAccess, source_identity: str) -> StorageContext: ...
    def provision(self, tenant_id: UUID, mode: IsolationMode) -> TenantStorageProfile: ...
    def offboard(self, tenant_id: UUID) -> None: ...   # not called in the first deployment
```

Object layout inside a tenant bucket:

```
inbox/{batch_id}/{file_name}            uploads, before scanning
originals/{doc_id}.pdf                  clean PDFs, under Object Lock
originals/{doc_id}.companion.json       the companion file, if any
originals/{doc_id}/attachments/{attachment_id}.{ext}   stipulations, under Object Lock
derived/faces/{face_id}.png             face crops from ID documents and live photos
quarantine/{doc_id}.pdf                 infected files
derived/text/{doc_id}.json              per-page text, source, quality score, word positions
derived/fields/{doc_id}.json            extracted fields
chat/{conversation_id}/{message_id}.json  question, retrieved passages, answer
```

Tenant provisioning: `POST /admin/tenants` inserts the tenant with status `provisioning`
and starts a Step Functions execution that runs a Fargate task. The task uses Pulumi's
Automation API with one stack per tenant (`assay-tenant/{tenant_id}`) to create the
bucket, key, vector bucket and index, malware protection plan and Rekognition face
collection (`assay-dev-t-{8 hex}`), then writes the
`tenant_storage` row, sets status `active` and invites the first admin. On failure it
records the failed step, sets status `failed` and allows retry.

`tests/isolation` assumes tenant A's role and tries to read tenant B's bucket, key,
vector index and rows. Every attempt must fail. It runs against the real account.

## 7. Data model

PostgreSQL, SQLAlchemy 2.0, psycopg 3, Alembic. UUID primary keys, `timestamptz`
everywhere. Every table below except `ai_models` has `tenant_id` and forced RLS.

| Table | Columns beyond `id`, `tenant_id` |
| --- | --- |
| `tenants` | `name`, `kind` (platform, business), `status` (provisioning, active, suspended, failed), `extraction_model`, `answer_model`, `identity_scoring_enabled`, `created_at` (here `id` is the tenant ID) |
| `ai_models` | `name`, `profile_id`, `jobs` (ingest, query), `price_in`, `price_out`, `status`; platform-wide |
| `tenant_models` | `model_id`, `job` (ingest, query), `enabled_by`, `enabled_at`; the models available to a tenant |
| `tenant_storage` | `mode`, `bucket`, `prefix`, `kms_key_arn`, `role_arn`, `vector_bucket`, `vector_index`, `region`, `version`, `status`; readable only by the TSS role |
| `users` | `cognito_sub`, `email`, `name`, `role`, `status` (invited, active, disabled), `invited_by`, `last_sign_in_at` |
| `batches` | `name`, `status` (open, closed, discarded), `opened_by`, `opened_at`, `closed_at` |
| `documents` | `batch_id`, `uploaded_by`, `file_name`, `sha256`, `s3_key`, `size_bytes`, `page_count`, `has_companion`, `application_ref`, `loan_number`, `cosigner_name`, `package_created_on`, `package_completed_on`, `status`, `error_code`, `duplicate_of`, `received_at`, `completed_at`, `pipeline_version` |
| `document_events` | `document_id`, `step`, `status`, `at`, `detail` |
| `document_parts` | `document_id`, `start_page`, `end_page`, `form_type`, `method` (rule, model), `confidence` |
| `document_labels` | `document_id`, `key`, `value`, `source` (companion) |
| `pages` | `document_id`, `page_no`, `text_source` (native, ocr), `quality_score`, `ocr_confidence`, `char_count`, `search_tsv` |
| `signature_check` | `document_id`, `intact`, `envelope_id`, `checked_at` |
| `signers` | `document_id`, `name`, `email`, `phone`, `location`, `signed_at`, `source` (audit_trail, companion) |
| `companion_loan_terms` | `document_id`, `term_months`, `maturity_date`, `rate` |
| `signing_events` | `document_id`, `signer_id`, `event`, `occurred_at`, `ip_address`, `auth_method` |
| `extracted_fields` | `part_id`, `field`, `value`, `page_no`, `confidence`, `schema_version`, `model_version` |
| `chat_conversations` | `user_id`, `title`, `created_at` |
| `chat_audit` | `conversation_id`, `user_id`, `asked_at`, `retrieved_doc_ids`, `log_s3_key`, `model_id` |
| `ai_usage` | `job`, `model_id`, `document_id`, `chat_audit_id`, `input_tokens`, `output_tokens`, `cost`, `request_id`; cost frozen at call time |
| `audit_log` | `at`, `actor_user_id`, `actor_email`, `actor_role`, `action`, `target_type`, `target_id`, `target_label`, `detail` (JSON with before and after), `outcome`, `ip_address`, `user_agent`; monthly partitions |
| `attachments` | `document_id`, `kind` (paystub, bank_statement, tax_form, id_document, live_photo, other), `file_name`, `s3_key`, `sha256`, `size_bytes`, `content_type`, `page_count`, `status` |
| `faces` | `document_id`, `attachment_id`, `page_no`, `kind` (id_document, live_photo), `rekognition_face_id`, `s3_key`, `image_sha256` |
| `face_matches` | `face_id`, `matched_face_id`, `similarity`, `scope` (same_document, other_document) |
| `fraud_scores` | `document_id`, `attachment_id`, `kind` (identity_photo, stipulation), `score`, `reasons` (JSON list of code, text, points, page or attachment), `rules_version`, `model_version`, `scored_at`, `review_state` (open, confirmed, dismissed), `reviewed_by`, `review_note` |

Document statuses: `uploaded` (batch still open), `scanning`, `processing`, `ready`,
`needs_review`, `quarantined`, `on_hold`, `failed`, `duplicate`.

Database roles:

- `app_tenant`: used by tenant routes and workers. No `BYPASSRLS`.
- `platform_reader`: used by admin routes. Can read `tenants` and the view
  `platform_ingestion_v` (document ID, tenant ID and name, batch ID, status, error code,
  page count, size, has_companion, part count, received and completed times). No access
  to any other table. The view must not expose `file_name`.
- `platform_writer`: used only by tenant provisioning and admin tenant changes.
- `migrator`: Alembic only.

## 7a. Audit trail

Record every event below in `audit_log`. A request that changes something and its audit
row commit in the same transaction.

| Category | Actions | Written by |
| --- | --- | --- |
| `auth` | `sign_in` | Cognito post-authentication Lambda trigger on both pools |
| `auth` | `sign_out` | `POST /auth/signed-out` |
| `auth` | `password_reset`, `password_changed` (by the user) | Cognito post-confirmation trigger and the change-password path |
| `user` | `invited`, `invite_resent`, `role_changed`, `disabled`, `enabled`, `password_reset_sent`, `mfa_reset` | The API |
| `tenant` | `created`, `provisioning_succeeded`, `provisioning_failed`, `provisioning_retried`, `suspended`, `reactivated`, `models_available_changed`, `model_selected`, `settings_changed` (identity scoring) | The API and the provisioning task |

- `detail` holds the values before and after. Never store passwords, tokens or codes.
- Append-only: `app_tenant` and the trigger Lambdas' role (`audit_writer`) get `INSERT`
  only; no role used by the application has `UPDATE` or `DELETE` on `audit_log`.
  `platform_reader` gets `SELECT` on it for the audit page.
- Trigger Lambdas run in the VPC and must never block a sign-in: on failure they log an
  error, raise an alarm and let the sign-in proceed.
- Keep 13 months online in monthly partitions.
- Failed sign-in attempts are not recorded in this deployment.

## 7b. Models

- `ai_models` is the catalog. Seed it with Claude Haiku 4.5
  (`us.anthropic.claude-haiku-4-5-20251001-v1:0`), Claude Sonnet 4.6
  (`us.anthropic.claude-sonnet-4-6`) and Claude Opus 4.6 (`us.anthropic.claude-opus-4-6-v1`),
  each approved for both jobs. The API and worker IAM roles may invoke only these
  inference profiles. Verify each profile with a test call before seeding.
- The master admin sets, per tenant and per job, which catalog models are available
  (`tenant_models`). New tenants start with Haiku 4.5 for ingest and Sonnet 4.6 for query.
- The tenant admin selects `tenants.extraction_model` and `tenants.answer_model` from the
  tenant's available models. The API rejects a model that isn't available to that tenant
  for that job.
- Each job always has at least one available model and exactly one selected. If the
  master admin removes a tenant's selected model, set the selection to the tenant's
  first remaining available model in the same transaction and audit both changes.
- A new ingest model applies to documents ingested after the change. Question routing
  always uses Haiku 4.5. If the selected query model is throttled or unavailable, retry
  once, then answer with Sonnet 4.6 and record the swap in `chat_audit`.
- Audit `models_available_changed` (master admin) and `model_selected` (tenant admin)
  with before and after values.

## 8. Ingestion pipeline

Trigger: a PDF starts only when its batch is closed **and** its malware scan is clean.

1. **Intake.** The browser uploads to `inbox/{batch_id}/` with 15-minute presigned PUT
   links. A companion file is paired with its PDF by base name.
2. **Malware scan.** GuardDuty publishes a `GuardDuty Malware Protection Object Scan
   Result` event to EventBridge. A Lambda records the result. `NO_THREATS_FOUND` makes
   the file eligible; `THREATS_FOUND` moves it to `quarantine/` and sets `quarantined`;
   `UNSUPPORTED` and `FAILED` set `on_hold`.
3. **Start.** When a batch closes, or a scan result arrives for a closed batch, eligible
   PDFs are queued on SQS, one Step Functions execution per PDF. A dead-letter queue
   catches repeated failures.
4. **Fingerprint.** SHA-256. If the tenant already has a document with that hash, set
   `duplicate`, point `duplicate_of` at it and stop. Otherwise move the PDF to
   `originals/` and the companion file beside it. Never modify the PDF.
5. **Text.** Read the embedded text layer with `pypdfium2` (`pdfplumber` where layout
   matters). Score each page; pages that fail go to Textract `DetectDocumentText`. Write
   `derived/text/{doc}.json` and the `pages` rows. Do not use PyMuPDF (AGPL).
6. **Split into forms.** Assign every page to a part. Rules first (form titles, form
   numbers, "Page 1 of 3" markers), then the extraction model for the rest. The e-sign
   audit trail, typically the last pages, is its own part.
7. **Signer details.** The extraction model reads the audit-trail part into `signers`
   and `signing_events`. `pyHanko` checks any PDF signature or seal for changes after
   signing and fills `signature_check`.
8. **Extract fields.** For each part, call the extraction model with the part's schema
   as its only tool (Bedrock Converse, forced tool choice) and validate the result with
   the same Pydantic class. Ship four schemas: `LoanApplication`, `Paystub`, `IdDocument` and the fallback
   `GeneralForm` (title, parties, dates, amounts, reference numbers). Every field
   carries `value`, `page` and `confidence`. Validation failure: retry once, then
   `needs_review`. Any field with confidence below 0.7 also sets `needs_review`.
9. **Companion file.** Validate with plain code, never the model. Store reserved keys as
   set out below and every custom pair in `document_labels`.
10. **Embed and index.** Chunk by section with page numbers and word positions, embed,
    write to the tenant's vector index with document ID and page as metadata, write
    `search_tsv`, set `ready`.

Each step writes a `document_events` row and each model call an `ai_usage` row.
Compute: Lambda (with Powertools) for steps 2 to 4 and status updates; one Fargate
worker task for steps 5 to 10. Idempotency is keyed on tenant and hash.

Treat document text as untrusted input. The model gets no tools beyond the extraction
schema, and nothing it returns chooses a tenant, a bucket or a SQL statement.

### Companion file, version 1

A flat JSON object, one per PDF, named with the PDF's base name (`smith-package.pdf`,
`smith-package.json`). It holds reserved keys and any custom keys. Every key is optional.

```json
{
  "TenantID": "7f3a91c2-...",
  "ApplicationId": "APP-2026-00123",
  "LoanNumber": "",
  "CreateDate": "2026-10-28",
  "CompleteDate": "2026-11-02",
  "Signer1Name": "Jane Smith",
  "Signer1Email": "jane@example.com",
  "Signer1Phone": "+14045550100",
  "Signer1Location": "Atlanta, GA",
  "CosignerName": "John Smith",
  "LoanTerm": 360,
  "LoanMaturityDate": "2056-12-01",
  "LoanInterestRate": 6.125,
  "Branch": "Atlanta"
}
```

Reserved keys, matched case-insensitively:

| Key | Type | Handling |
| --- | --- | --- |
| `TenantID` | UUID | Check only: must equal the token's tenant, or reject the file. Never used to choose a tenant |
| `ApplicationId` | text | `documents.application_ref` |
| `LoanNumber` | text | `documents.loan_number` |
| `CreateDate`, `CompleteDate` | date, YYYY-MM-DD | `documents.package_created_on`, `documents.package_completed_on` |
| `Signer{1..3}Name`, `Signer{1..3}Email`, `Signer{1..3}Phone`, `Signer{1..3}Location` | text | One `signers` row per signer with `source = companion` and `location` |
| `CosignerName` | text | `documents.cosigner_name` |
| `LoanTerm` | integer, months | `companion_loan_terms.term_months` |
| `LoanMaturityDate` | date | `companion_loan_terms.maturity_date` |
| `LoanInterestRate` | number, annual percent | `companion_loan_terms.rate` |

Any other key is a custom field stored in `document_labels`. Custom keys match
`^[A-Za-z][A-Za-z0-9_]{0,63}$`. Values are strings (up to 500 characters), numbers or
booleans. No nesting, at most 50 custom keys, at most 64 KB.

Companion values never overwrite what the pipeline reads from the PDF. Both are stored
with their source.

## 8a. Stipulations and fraud scoring

First version. Build it end to end; do not spend time tuning weights or thresholds.
Show both scores in the portal with an "Experimental" label.

### Stipulation files

- Stipulations are separate files tied to one primary PDF. Accepted: PDF, JPEG, PNG, TIFF.
- Pairing: the primary's base name plus `.stip{n}`: `smith-package.pdf`,
  `smith-package.stip1.jpg`, `smith-package.stip2.pdf`. The portal also lets an admin
  drop a file onto a primary document in the batch list, which records the same link.
- A stipulation with no primary document blocks the batch from closing.
- Each attachment is scanned, hashed and stored under
  `originals/{doc_id}/attachments/`, then read (images go straight to Textract; convert
  TIFF pages to PNG where a service needs it), classified by the extraction model into
  an `attachments.kind`, extracted with the matching schema (`Paystub`, `IdDocument`,
  otherwise `GeneralForm`), embedded and indexed. Attachments finish before the primary
  document is set `ready`.

### Scores

Both scores are integers from 0 to 100; low means look closer. Each starts at 100 and
loses points per signal. Signals, points and thresholds live in one versioned rules
module (`rules_version`); every deduction is stored as a reason with its code, text,
points and the page or attachment it refers to. A document with nothing to score gets
no row, not a score of 100.

Stipulation authenticity score, one per attachment:

| Signal | Source |
| --- | --- |
| Editing software named in PDF or image metadata; PDF modified after creation | Code |
| Image identical (SHA-256) to an attachment on another document in the tenant | Code |
| Paystub arithmetic: gross less deductions equals net; year-to-date fits the pay period | Extraction model, returned as pass or fail per check |
| Employer, borrower name or income disagrees with the primary document's fields | Code over extracted fields |
| Same employer and figures under a different borrower in the tenant | SQL under RLS |

Identity photo score, one per primary document, only when
`tenants.identity_scoring_enabled` is true (true for the two seeded business tenants,
false by default for new tenants):

1. Find ID document and live photo images: pages of the primary PDF that the split step
   classifies as such (render at 200 dpi), plus attachments of those kinds.
2. Amazon Rekognition `DetectFaces`; keep the largest face; store the crop and a `faces`
   row. Send image bytes, not S3 references.
3. `CompareFaces` between the ID face and the live photo in the same document.
4. `IndexFaces` into the tenant's collection, then `SearchFaces` for the same face on
   other documents.
5. Deduct for: ID photo and live photo not matching; the same face on another document
   whose ID name, birth date or ID number differs; an identical image file reused.

Rules for faces:

- Never send a face image to Claude and never ask a model to identify or compare faces.
  Claude only reads the printed text on an ID through the `IdDocument` schema.
- One Rekognition collection per tenant, reached only with TSS-issued tenant
  credentials. Add a cross-tenant face search to `tests/isolation`.
- Rekognition does not document customer-managed keys for face vectors. Do not describe
  face data as encrypted with the tenant's key.
- Confirm the organization's AI services opt-out policy covers Rekognition before use.
- Use only synthetic or consented test photos in this environment.

### Portal and chat

- Fraud review screen: scored documents, lowest score first; filters for score kind,
  "below" threshold, date range and review state. A row opens the reasons, each with its
  points and a link to the page or image, and Confirm or Dismiss with a note.
- Ingestion history (tenant view) gains two columns: identity score and lowest
  stipulation score. The master view shows neither.
- Write a short text summary of each document's scores and reasons into the tenant's
  vector index, so a question about one application can cite them. Listing or counting
  across applications is done with the Fraud review filters, not chat.
- Scores never block, reject or change a document.

## 9. AI questions

1. Embed the question and search the tenant's vector index; also run a Postgres
   full-text search under RLS. Merge to the top passages with document and page.
2. Call the answer model with only those passages. It must cite each claim by document
   and page and reply that the documents don't answer the question when they don't.
3. Stream the answer to the browser as server-sent events, followed by the citations.
4. Write the question, passages and answer to `chat/` in the tenant's bucket, a
   `chat_audit` row and an `ai_usage` row.

Conversations are multi-turn and visible only to the user who started them. Retrieval
covers only documents with status `ready`. All storage and vector access goes through a
TSS context for the token's tenant.

## 10. API

All routes are under `/api`. Tenant routes accept tenant-pool tokens; admin routes
accept platform-pool tokens only. Enforce every permission in the API; the portal only
hides what a role can't do. Generate the portal's API client from the OpenAPI schema.

| Route | Roles | Does |
| --- | --- | --- |
| `GET /me` | All | User, role, tenant name |
| `GET /users`, `POST /users` | tenant-admin | List; invite (name, email, role) |
| `PATCH /users/{id}` | tenant-admin | Change role, disable, enable |
| `POST /users/{id}/resend-invite`, `POST /users/{id}/reset-password` | tenant-admin | As named |
| `POST /users/{id}/reset-mfa` | tenant-admin | Reset MFA; the target must be a tenant-user in the same tenant |
| `POST /batches`, `GET /batches/{id}` | tenant-admin | Open a batch; read it with its files |
| `POST /batches/{id}/files` | tenant-admin | Register files; return a presigned link for each |
| `POST /batches/{id}/close`, `DELETE /batches/{id}` | tenant-admin | Close and start processing; discard |
| `GET /documents` | tenant-admin, tenant-user | Ingestion history: `from`, `to`, `status`, `batch_id`, `sort`, `page`, `format=csv` |
| `GET /documents/{id}`, `GET /documents/{id}/pdf-url` | tenant-admin, tenant-user | Detail with events and parts; short-lived view link |
| `GET /chat/conversations`, `POST /chat/conversations` | tenant-admin, tenant-user | List own; start one |
| `POST /chat/conversations/{id}/messages` | tenant-admin, tenant-user | Ask; stream answer and citations |
| `GET /fraud-scores` | tenant-admin, tenant-user | Scored documents: `kind`, `below`, `from`, `to`, `review_state`, `sort`, `page` |
| `GET /documents/{id}/fraud` | tenant-admin, tenant-user | Scores and reasons for one document and its attachments |
| `POST /fraud-scores/{id}/review` | tenant-admin, tenant-user | Mark confirmed or dismissed, with a note |
| `GET /admin/tenants` | master-admin, master-user | List tenants |
| `POST /admin/tenants` | master-admin | Add a tenant (legal name, first admin's name and email) |
| `PATCH /admin/tenants/{id}`, `POST /admin/tenants/{id}/retry` | master-admin | Suspend, reactivate; retry provisioning |
| `GET /admin/tenants/{id}/admins` | master-admin, master-user | A tenant's admins: name, email, status |
| `POST /admin/users/{id}/reset-mfa` | master-admin | Reset MFA; the target must be a tenant-admin or the master-user |
| `GET /admin/tenants/{id}/models`, `PUT /admin/tenants/{id}/models` | master-admin (GET also master-user) | Read; set the models available to a tenant per job |
| `GET /settings/models`, `PUT /settings/models` | tenant-admin | Read available models and current selection; select the ingest and AI query models |
| `GET /admin/audit` | master-admin, master-user | Audit history: `from`, `to`, `tenant_id`, `category` (auth, user, tenant), `actor_email`, `sort`, `page`, `format=csv` |
| `POST /auth/signed-out` | All | Records a sign-out; the portal calls it before Cognito logout |
| `GET /admin/ingestion` | master-admin, master-user | Cross-tenant history: same filters plus `tenant_id` |

Rules: an admin can't change their own role or disable themselves; a tenant always keeps
one active admin; one open batch per user; a batch can't close while it holds an invalid
or unmatched companion file (including one whose `TenantID` doesn't match); an open batch closes automatically after 24 hours; a
suspended tenant's users can't sign in or upload. Limits: 100 MB per PDF, 25 MB per
attachment, 200 primary PDFs per batch, 20 attachments per PDF. Every user and tenant change writes an `audit_log` row.

## 11. Portal

One React app served on both hosts; the host decides which user pool to sign in against,
and `GET /me` decides the navigation.

| Role | Screens | Lands on |
| --- | --- | --- |
| tenant-admin | Ask, Upload, Ingestion history, Fraud review, Users, Settings | Ask |
| tenant-user | Ask, Ingestion history, Fraud review | Ask |
| master-admin | Tenants, Ingestion history (all tenants), Audit history | Tenants |
| master-user | Tenants (read-only), Ingestion history (all tenants), Audit history | Tenants |

- **Shell.** Cloudscape app layout. Top bar: logo, tenant name, user name, Sign out, and
  a "Development" tag. Navy `#14213D` top bar and headings, gold `#F2B632` accents,
  `#B07D12` for gold text on white. Use `assets/assay-insights-mark.svg` as the favicon.
- **User menu.** Under the user's name: Change password (for every role) and Sign out.
- **Users.** Table: name, email, role, status, last sign-in. Invite dialog; row actions
  for role, disable or enable, resend invite, reset password, and reset MFA (tenant
  users only).
- **Tenants.** Table: name, kind, status, users, documents ingested, created. Add tenant
  dialog; live status while provisioning; Retry on failure; Suspend and Reactivate. A tenant's detail lists
  its admins with Reset MFA (hidden for the master user).
- **Audit history.** Newest first, 50 per page, server-side sort and filter, CSV export.
  Columns: time, tenant, actor, role, action, target, outcome. Filters: date range,
  tenant, category, actor's email. A row opens the before and after values, IP address
  and browser.
- **Models.** Master: a Models panel on a tenant's page with two checklists (Ingest, AI
  query) of catalog models; read-only for the master user. Tenant admin: a Settings
  screen with two dropdowns listing only the tenant's available models, each with its
  price per million tokens.
- **Upload.** New batch (optional name), drop zone, file list with size, progress and
  "companion file matched" or "no companion file", inline validation errors, Close batch
  and Discard batch. Closing switches to that batch's ingestion history.
- **Ask.** Own conversations on the left, the conversation in the middle, a PDF.js
  viewer on the right. Answers stream; each citation chip shows file name and page and
  opens the PDF at that page with the passage highlighted. Empty state when no document
  is `ready`, with a link to Upload for admins.
- **Ingestion history.** One row per PDF, newest first, 50 per page, server-side sort
  and filter, CSV export, refresh every 10 seconds while any row is in progress.
  Filters: date range (today, 7 days, 30 days, custom), status, batch; the master view
  adds tenant. Tenant columns: received, batch, file name, pages, size, companion file,
  status, forms found, duration, uploaded by. Master columns: received, tenant, batch
  ID, document ID, pages, size, companion file, status, forms found, duration. A row
  opens a panel with each pipeline step, its time and any error; tenants also see the
  forms found and can open the PDF.
- **Every screen** has loading, empty and error states; destructive actions ask for
  confirmation; WCAG AA contrast and keyboard access.

## 12. Build order

1. Repo scaffold, CI checks, Pulumi project, DNS zone and certificate. Stop and give the
   owner the four name servers to enter at Squarespace.
2. Network, database, migrations, database roles and RLS.
3. Cognito pools, managed login branding, API skeleton with token verification, `GET /me`.
4. Tenant Storage Service, tenant provisioning, `tests/isolation`.
5. Portal shell, sign-in, Tenants and Users screens, `scripts/seed.py`.
6. Batches and upload, malware scan routing, pipeline steps 4 to 10, ingestion history
   for tenants and for the master tenant.
7. AI questions with citations and the PDF viewer.
8. Stipulation uploads, the two fraud scores and the Fraud review screen (section 8a).
9. Acceptance checks below.

## 13. Done when

- [ ] All six seeded users can sign in; both master users and both tenant admins are
      forced through MFA setup.
- [ ] A user in each role can reset a forgotten password by email without an admin, and
      can change it while signed in.
- [ ] A tenant admin can reset a tenant user's MFA and the master admin a tenant
      admin's; the user is signed out and sets up a new authenticator at next sign-in.
      No other role or path can reset MFA.
- [ ] A sign-in, a sign-out, a role change and a tenant suspension each appear in Audit
      history within a minute, with actor and before and after values; the page filters
      by date, tenant and category; no application role can update or delete a row.
- [ ] The master admin makes Opus 4.6 available to one tenant; that tenant's admin can
      select it for AI query and the other tenant's admin cannot; the next answer uses
      it; both changes appear in Audit history.
- [ ] A tenant admin invites a user, who gets the email, signs in and sees only Ask and
      Ingestion history.
- [ ] A tenant admin cannot remove the tenant's last admin.
- [ ] The master admin adds a fourth tenant, sees it go from `provisioning` to `active`,
      and its first admin can sign in. The master user cannot add or suspend a tenant.
- [ ] A tenant admin opens a batch, uploads PDFs with and without companion files,
      closes it and sees each PDF reach `ready`.
- [ ] An invalid companion file blocks the batch from closing with a clear message.
- [ ] The EICAR test file ends as `quarantined`; a password-protected PDF as `on_hold`;
      a repeated upload as `duplicate`.
- [ ] A question about an uploaded PDF returns a cited answer, and the citation opens
      the right page. A question the documents can't answer says so.
- [ ] Ingestion history sorts by every column and filters by date. The master view
      filters by tenant and shows no file names.
- [ ] Global Finance users cannot see Infinity Loans documents in history, in chat
      answers or by guessing a document ID, and `tests/isolation` passes.
- [ ] A batch with a primary PDF and two stipulations closes and all three are processed;
      a stipulation with no primary blocks the batch.
- [ ] Both scores appear with reasons in Fraud review and in ingestion history, and a
      review can be recorded. Master users see no scores.
- [ ] An edited test paystub scores lower than its original, and the same test face
      under two names is flagged.
- [ ] One tenant's face collection cannot be searched with another tenant's credentials.
- [ ] `pulumi preview` on an unchanged stack shows no changes.
