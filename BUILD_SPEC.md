# Assay Insights: first deployment build spec

Build spec for the first deployment of Assay Insights, a multi-tenant AWS SaaS that lets
lenders query archives of signed PDF loan documents through AI chat. This file is the
contract for the build. The full architecture document holds the reasoning and the later
phases; where the two differ, this file wins for the first deployment.

Status of decisions: all confirmed by the owner (Keith Lynn) on 2026-11-21.

Amended during the build, each time by an owner decision. Section 14 lists every amendment
with its date; where it differs from an earlier section, section 14 wins. Sections 2, 3, 7,
7a, 8, 10, 11 and 12 have been brought into line with it (last on 2026-11-27, after stage 2
of the second half of build step 6 was deployed and verified in dev). `CLAUDE.md` holds the reasoning, the evidence
and the working notes.

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
| Embedding model | Amazon Titan Text Embeddings V2, called by model ID in `us-east-1`. It has no `us.` inference profile, so it is a named exception to the Bedrock rule (owner, 2026-11-24) |
| Tenant portal host | `dev.assayinsights.ai` |
| Admin portal host | `admin.dev.assayinsights.ai` |
| DNS | Route 53 hosted zone `dev.assayinsights.ai` in the Development account. The registrar is Squarespace; the owner adds the NS records there by hand once the zone exists |
| IaC | Pulumi in Python, state in Pulumi Cloud (organization `pilartech`, on the Free plan since 2026-11-27; nothing in the code depends on the plan), project `assay-platform`, stack `dev`, run with `pulumi up` from VS Code |
| Already in place | Control Tower landing zone, organization CloudTrail, AWS Config, GuardDuty and Security Hub (managed by a separate `pilar-foundation` stack). Do not manage any of these here. Account Factory creates no VPC |
| Language | Python 3.12+ (pinned to 3.14 in `.python-version`), `uv` for locked dependencies. Portal in React and TypeScript |

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
  used inside containers, and Python files. A file it marks as LF must also be LF on disk:
  images are built from the bytes on disk. `uv run scripts/check_line_endings.py` checks it
  (as a unit test and in CI too), and `pulumi preview` refuses an image source with CRLF
  (owner, 2026-11-27).
- Use `pathlib` and forward slashes; never hard-code Windows or POSIX paths.

Never put model IDs, account IDs, hostnames or email addresses in code. Read them from
Pulumi config or the `ai_models` table.

## 3. Repository layout

```
infra/      Pulumi program (Python): network, data, auth, compute, edge, pipeline
db/         Database: SQLAlchemy models, Alembic migrations, database roles and RLS
            (uv workspace package `assay_db`, shared by api/ and workers/)
api/        FastAPI app (ECS Fargate)
workers/    Pipeline code: Lambda handlers and the Fargate worker
tss/        Tenant Storage Service (Lambda) and its client package
schemas/    Pydantic v2 models shared by api/ and workers/
portal/     React + TypeScript + Vite + Cloudscape single-page app
scripts/    seed.py and operational scripts
tests/      Unit tests, plus tests/isolation (runs against real AWS)
assets/     assay-insights-logo.svg, assay-insights-mark.svg
```

GitHub Actions runs the line-ending check, lint, type checks, unit tests (image tests
included), `pip-audit` and Bandit, and for the portal its type check, lint, unit tests,
build, dependency audit and rendered layout checks, on pull requests. It does not deploy.

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
   on dead-letter queue depth, Step Functions failures and API 5xx rate, and a spend guard:
   an alarm when Textract has read more than `textractDailyPagesAlarm` pages in 24 hours
   across the account.

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
inbox/{batch_id}/{file_id}              uploads, before scanning
originals/{doc_id}.pdf                  clean PDFs, under Object Lock
originals/{doc_id}.companion.json       the companion file, if any
originals/{doc_id}/attachments/{attachment_id}.{ext}   stipulations, under Object Lock
derived/faces/{face_id}.png             face crops from ID documents and live photos
quarantine/{doc_id}.pdf                 infected PDFs
quarantine/{file_id}.companion.json     infected companion files
derived/text/{doc_id}.json              per-page text, source, quality score, word positions
derived/fields/{doc_id}.json            extracted fields
chat/{conversation_id}/{message_id}.json  question, retrieved passages, answer
```

No object key contains a file name. An upload is stored under an opaque `file_id`, and its
`file_name` is kept only in the database.

Every version of every object is kept: Object Lock's default applies to the whole bucket, no
role may delete a version, and there is no lifecycle rule. "Process again" writes a new
version of `derived/text/{doc_id}.json`; the document's row names the version to read
(`documents.text_version_id`) and each attempt's `text` event names its own (section 14).

Tenant provisioning: `POST /admin/tenants` inserts the tenant with status `provisioning`
and starts a Step Functions execution that runs a Fargate task. The task uses Pulumi's
Automation API with one stack per tenant (`assay-tenant/{tenant_id}`) to create the
bucket, key, vector bucket and index, malware protection plan and Rekognition face
collection (`assay-dev-t-{8 hex}`), then writes the
`tenant_storage` row, sets status `active` and invites the first admin. On failure it
records the failed step, sets status `failed` and allows retry.

The provisioning task's role manages the settings, tags and policies of tenant resources
and never tenant data. It has one accepted, deliberate exception (owner, 2026-11-23): on a
tenant's KMS key it may use `kms:Decrypt` and `kms:GenerateDataKey`, but only for calls
that S3 Vectors makes on its behalf. S3 Vectors requires both of whoever creates a vector
bucket and index encrypted with that key. The role cannot reach tenant data with them, for
three reasons:

1. The permission applies only through S3 Vectors. The role cannot call KMS itself, and
   cannot use the key through S3, where the documents are.
2. The role's policy allows no action that reads or writes vectors, and each tenant's
   vector bucket policy denies it every one.
3. The role's policy and each tenant's bucket policy deny it every action on an object.

`tests/isolation` assumes tenant A's role and tries to read tenant B's bucket, key,
vector index and rows. Every attempt must fail. It also tries, as the provisioning role,
to read and write a tenant's objects and vectors and to use its key directly; those must
fail too. It runs against the real account.

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
`needs_review`, `quarantined`, `on_hold`, `failed`, `duplicate`, `discarded` (a discarded
batch's documents and a removed file's; left out of both history views unless the status
filter asks for it).

Added during the build (the migrations' docstrings are the authority for columns, policies
and grants; section 14 says why):

- Tables: `batch_files` (every registered upload: PDF or companion file, its opaque `file_id`
  and inbox key, size, SHA-256, scan result and the pinned `version_id`, `purpose`: a batch
  upload or a companion file attached later) and `worker_grants` (one worker task's right to
  one attempt of one document: only the SHA-256 of its token is stored).
- Attempts: `documents.attempt` and `document_events.attempt`; `attempt` on `signers`,
  `companion_loan_terms` and `document_labels`, with `documents.companion_attempt` pointing
  at the current version of the companion data and `documents.companion_file_id` at the
  upload it came from; `documents.content_attempt` is the same pointer for text and
  extraction (unused until a stage writes them). Nothing is deleted on reprocessing.
- Text (stage 2): `pages.attempt`, one row per page per attempt, read through
  `documents.content_attempt`; `documents.text_version_id`, the S3 version of the text object
  that pointer stands for. No page text is stored in the database: `pages` holds numbers and
  each page's source.
- `ai_usage` also records a service billed by the page: `service` (`bedrock` or `textract`),
  `operation`, `model_version`, `attempt`, `page_no`, `units`, `unit` and `unit_price`. A
  Textract row has no `model_id` and no tokens.
- Also: `documents.queued_at`, `users.sessions_revoked_at`, and views in schema `platform`
  for the admin routes (among them `platform_users_v`, `ai_models_v`, `tenant_models_v`
  and `ingestion_events_v`).

Database roles:

- `app_tenant`: used by tenant routes and workers. No `BYPASSRLS`.
- `platform_reader`: used by admin routes. Can read `tenants` and the view
  `platform_ingestion_v` (document ID, tenant ID and name, batch ID, status, error code,
  page count, size, has_companion, part count, received and completed times). No access
  to any other table. The view must not expose `file_name`.
- `platform_writer`: used only by tenant provisioning and admin tenant changes.
- `migrator`: Alembic only.
- `audit_writer`: used by the Cognito trigger Lambdas. `INSERT` on `audit_log` only
  (section 7a).
- `tss`: used by the Tenant Storage Service. The only role that can read `tenant_storage`.
- `app_worker`: used by the pipeline's worker task, never `app_tenant`. Its session is bound
  to one attempt grant (`pipeline.bind_worker`), and its policies show it that one document
  and nothing else. Column-level grants only; each stage's migration adds the grants and
  policies for the tables its steps use.
- `pipeline_lookup` and `auth_event_recorder`: cannot sign in. They own the `pipeline` and
  `auth_events` functions (tenant for an upload, tenants with work, the worker's grant; the
  Cognito triggers' audit rows).
- `platform_view_owner`: cannot sign in. Owns the views that `platform_reader` reads
  (`platform_ingestion_v` and any later admin views, in schema `platform`) and can read
  only the columns those views need, so `platform_reader` needs no access to tenant tables.
  Admin routes that need more than `tenants`, `audit_log` and `platform_ingestion_v` get
  a narrow view of this kind (counts, admin names); never a direct table grant.

No database role has `BYPASSRLS`. Login roles sign in with IAM database authentication;
only the RDS master user has a password, which RDS manages in Secrets Manager.

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
| `auth` | `session_started` (the sign-in's IP address and browser), `mfa_enabled` (tenant pool), `temporary_password_accepted` | `POST /auth/session-started`, `POST /auth/mfa-enabled`, the post-authentication trigger |
| `audit` | `exported` (an Audit history or ingestion history CSV: who, the filters, the row count) | The API |
| `security` | `file_quarantined` (tenant, uploader, batch and document IDs, GuardDuty's threat name, the quarantine key and source version; never a file name or content) | The scan Lambda, which also publishes a security notice |
| `document` | `reprocess_requested`, `marked_reviewed`, `companion_replace_requested` | The API (tenant admins) |
| `document` | `companion_replaced`, `companion_attached`, `companion_replace_rejected` (checked and refused: invalid, infected, or not what was registered), `companion_replace_expired` (never arrived, or never scanned) | The scan Lambda and the sweeper; every request ends in exactly one of these |

- `detail` holds the values before and after. Never store passwords, tokens or codes.
- Append-only: `app_tenant` and the trigger Lambdas' role (`audit_writer`) get `INSERT`
  only; no role used by the application has `UPDATE` or `DELETE` on `audit_log`.
  `platform_reader` gets `SELECT` on it for the audit page.
- Trigger Lambdas run in the VPC and must never block a sign-in: on failure they log an
  error, raise an alarm and let the sign-in proceed. The trigger only queues the event on
  SQS; a consumer Lambda writes the row as `audit_writer` (Aurora at 0 ACU takes longer to
  resume than Cognito waits for a trigger).
- A row's `detail` never holds tenant content: master roles read audit rows. Codes, IDs and
  counts only; a companion file's custom keys are never named.
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
   `UNSUPPORTED`, `FAILED` and `ACCESS_DENIED` set `on_hold`. The same Lambda checks the
   stored size and SHA-256 against the registration, pins the version that scanned clean
   (only that version is ever used), and validates a companion file. A quarantine writes a
   `security` audit row and publishes a security notice to its own SNS topic. A file with
   no scan result after 30 minutes is put on hold by the sweeper, with an alarm.
3. **Start.** When a batch closes, or a scan result arrives for a closed batch, eligible
   PDFs are queued on SQS, one Step Functions execution per PDF. A dead-letter queue
   catches repeated failures.
4. **Fingerprint.** SHA-256. If the tenant already has a document with that hash, set
   `duplicate`, point `duplicate_of` at it and stop (a `failed` document is not an
   original). A file that does not begin with `%PDF-` fails (`not_a_pdf`). The Lambda never
   opens the PDF: no Lambda runs a PDF parser. Then the worker task's first step, **inspect**,
   opens it in the sandbox: a password-protected PDF is put `on_hold` (`pdf_encrypted`), one
   that cannot be opened fails (`pdf_unreadable`), and otherwise the page count is stored and
   the PDF is copied to `originals/`. Never modify the PDF.
5. **Text.** Read the embedded text layer with `pypdfium2`, in the sandbox. Score each page;
   pages that fail go to Textract `DetectDocumentText`. Write `derived/text/{doc}.json` and
   the `pages` rows. Do not use PyMuPDF (AGPL), and do not use `pdfplumber` (owner,
   2026-11-24). As built (owner, 2026-11-27; section 14 has each decision):
   - A page's score is 1.0 if it is blank, 0.0 if annotations draw on it (form fields,
     stamps) or it has content and no text, and otherwise its text's legibility times the
     share of the page that no image covers. A page below `textQualityThreshold` (0.90) goes
     to Textract, so a page with an image over about a tenth of it does, whatever its text
     layer says.
   - Such a page is drawn in the sandbox as a greyscale PNG and sent to Textract as bytes,
     one page a call, by the worker task with its own role, through the VPC's Textract
     endpoint. Textract never reads a bucket.
   - A page is `native` or `ocr`. An `ocr` page whose text layer reads well keeps that text
     beside Textract's, marked as not its source. Only a page's source text feeds extraction,
     search and embedding.
   - A document with more than `textMaxPages` (1,000) pages fails (`too_many_pages`). A page
     Textract will not take is recorded as unread and the document goes on.
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
   set out below and every custom pair in `document_labels`. It runs as a Lambda step after
   the worker task, not inside it (it is plain JSON and needs no sandbox), and copies the
   companion file beside the PDF in `originals/`. The values are stored by version; nothing
   is deleted.
10. **Embed and index.** Chunk by section with page numbers and word positions, embed,
    write to the tenant's vector index with document ID and page as metadata, write
    `search_tsv`, set `ready`.

Each step writes a `document_events` row and each model call an `ai_usage` row.
Compute: Lambda (with Powertools) for steps 2 to 4, step 9 and status updates; a Fargate
worker task for the inspection and steps 5 to 8 and 10. Idempotency is keyed on tenant and
hash.

The worker (owner decisions, 2026-11-24; `CLAUDE.md` has the detail):

- **One task per document attempt**, started by Step Functions (`ecs:runTask.sync`), at most
  `workerMaxPerTenant` (5) at once per tenant. A document never fails for lack of a slot or
  of capacity: it waits and tries again.
- **Bound to one document.** The state machine's `claim` step creates an attempt grant; the
  task gets the document ID, the attempt and the grant's token, and nothing else. It asks
  the TSS at `/resolve-worker`, naming no tenant, and gets credentials for that document's
  exact keys only. It signs in to the database as `app_worker`, bound to the same grant.
- **PDFs are opened only in a sandbox child**: another user, no network, no environment, CPU,
  memory, time and output limits, a seccomp filter. The parent holds the credentials, never
  opens a PDF, and trusts nothing the child returns beyond a small validated result.
- **Attempts.** "Process again" (tenant admins) starts a new attempt of a document from the
  upload at the version that scanned clean. Every event and derived row carries its attempt;
  a superseded attempt writes nothing; old results are kept and read through pointers.
- **A failed step names itself**: the document's code is `{step}_failed`. Tenants see plain
  words; master admins also see the error's type, never its message.
- Every AI call writes an `ai_usage` row. No prompt or response content is logged anywhere.
  Bedrock and Textract throttling is retried with backoff. Textract and Bedrock are called
  with the worker's own role and bytes; Rekognition only with tenant credentials.
- **Page text and pictures leave the sandbox by the page channel**: a pipe the parent opened
  and reads one record at a time. A child's standard output is still one small object of
  numbers and codes, and the child still writes no file. The parent never opens a picture.
- **Extracted text is content chosen by whoever made the PDF**: it is data, never
  instructions, when it is shown to a model.
- Before content first goes to Textract or Bedrock, re-check the AI services opt-out policy
  and that model invocation logging is off, and show the owner the output.
- Only `ready` documents are retrieved. "Mark as reviewed" (tenant admins) moves
  `needs_review` to `ready`; `needs_review` documents are still indexed.

Stages of steps 5 to 10 (each a deploy and the owner's checks):

| Stage | Builds | Content to AI services | Status |
| --- | --- | --- | --- |
| 1 | The worker task, the attempt grant, the sandbox, `app_worker`; attempts and "Process again"; step 9; companion attach and replace; the quarantine notice; "Mark as reviewed" (migrations `0010`, `0011`) | None | Complete, verified 2026-11-27 |
| 2 | Step 5: text layer, Textract for pages that fail the quality score (migration `0012`) | Textract | Complete, verified in dev 2026-11-27 |
| 3 | Steps 6, 7, 8: split, signer details, `pyHanko`, field extraction | Bedrock (Claude) | Not started |
| 4 | Step 10: chunk, embed, index; final statuses; the Titan catalog row | Bedrock (Titan) | Not started |

Until stage 4, a document that passes its steps stays `processing`. In stage 4 the final
status is set inside the worker task, at its last write, and the companion step runs before
the task or beside it: Fargate takes about 30 seconds to stop a task, and nothing should wait
for that (owner, 2026-11-27).

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
| `GET /batches` | tenant-admin, tenant-user | List batches (`status`, `mine`), for the history's batch filter |
| `POST /batches/{id}/files` | tenant-admin | Register files; return a presigned link for each |
| `DELETE /batches/{id}/files/{file_id}` | tenant-admin | Remove a file from an open batch |
| `POST /batches/{id}/close`, `DELETE /batches/{id}` | tenant-admin | Close and start processing; discard |
| `GET /documents` | tenant-admin, tenant-user | Ingestion history: `from`, `to`, `status`, `batch_id`, `sort`, `page`, `format=csv` |
| `GET /documents/{id}`, `GET /documents/{id}/pdf-url` | tenant-admin, tenant-user | Detail with events and parts; short-lived view link |
| `POST /documents/{id}/reprocess` | tenant-admin | "Process again": a new attempt. Refused while an attempt is under way, and for a quarantined, on-hold, duplicate or discarded document |
| `POST /documents/{id}/reviewed` | tenant-admin | "Mark as reviewed": `needs_review` becomes `ready` |
| `POST /documents/{id}/companion` | tenant-admin | Attach or replace the companion file; returns a presigned link. It is scanned and validated like any upload and takes effect only if clean and valid |
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
| `POST /auth/session-started`, `POST /auth/password-changed` | All | Record the start of a session (once per sign-in) and a password change |
| `POST /auth/mfa-enabled` | tenant-admin, tenant-user | Records MFA enrolment, after checking with Cognito that TOTP is on |
| `GET /admin/ingestion` | master-admin, master-user | Cross-tenant history: same filters plus `tenant_id` |
| `GET /admin/ingestion/{id}` | master-admin, master-user | One document's pipeline steps, times and error codes; no file name |

Rules: an admin can't change their own role or disable themselves; a tenant always keeps
one active admin; one open batch per user; a batch can't close while it holds an invalid
or unmatched companion file (including one whose `TenantID` doesn't match); an open batch closes automatically after 24 hours; a
suspended tenant's users can't sign in or upload. Limits: 100 MB per PDF, 1,000 pages per
PDF (`textMaxPages`; checked by the pipeline, not at upload), 25 MB per
attachment, 200 primary PDFs per batch, 20 attachments per PDF, 64 KB per companion file,
50 files per registration request, file names up to 255 characters, `.pdf` and `.json`
only. Every user and tenant change writes an `audit_log` row, and so does every CSV export
and every document action (section 7a).

Duration, in both history views and both CSV exports (owner, 2026-11-25): from the
document's latest `queued` event (its batch closing, or its latest "Process again") to its
completion; from when it was received if it was never queued.

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
  and Discard batch. Closing switches to that batch's ingestion history. No file goes
  missing without a word: every addition shows how many files were chosen and how many were
  added, until the next addition or until it is dismissed; files left out stay listed under
  "Not added" until dismissed; files cannot be chosen while an addition runs; and the reason
  an addition or a close was refused stays on the screen.
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
  forms found and can open the PDF. The Text step's line says how many pages were read from
  the text layer, how many by OCR and how many could not be read, in both views.
- **Document actions** (tenant admins, in the tenant's ingestion history; owner,
  2026-11-24): "Process again" for one document or several selected, with a confirmation
  that says how many are Ready (running the AI steps again costs money); "Mark as reviewed";
  attach or replace the companion file. The document's panel shows the companion file's
  values and every companion upload with its problems in plain words; from stage 3 it also
  shows each form's extracted fields with page and confidence. Master views never show
  extracted or companion values.
- **Sessions.** The portal signs a user out after `portalIdleTimeoutMinutes` without
  activity in any of its tabs, with a warning 2 minutes before. Signing out in one tab signs
  out the portal's other tabs in that browser. A session that cannot be renewed ends on the
  sign-in screen with "Your session has ended. Sign in again to continue."
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
   for tenants and for the master tenant. Built in two halves: the first (batches, uploads,
   scan routing, fingerprint, both histories) is complete; the second (steps 5 to 10) is in
   the four stages of section 8, of which stage 1 is complete (2026-11-27).
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

## 14. Amendments (owner decisions during the build)

Every item is an owner decision, with its date. Where one differs from a section above, this
section wins. `CLAUDE.md` records the reasoning, the evidence from AWS and the limits the
owner accepted.

**Tenant isolation (section 6)**

- A tenant bucket's policy names two more principals besides the tenant role and the
  break-glass role, each for one job: the GuardDuty scan role (read and tag objects) and the
  provisioning role (bucket settings, never object data) (2026-11-23).
- The GuardDuty scan role reads and tags objects under `inbox/` only, the prefix each
  tenant's protection plan scans, when stack config `malwareScanInboxOnly` is `"true"` (on
  in Development). `"false"` is the role as AWS's page writes it, for the whole bucket, and
  is the way back. Listing the bucket and writing GuardDuty's validation object are the
  same either way (2026-11-27).
- Tags are part of the lock: only the provisioning role may tag a tenant KMS key, and
  `tenant_id` on a key is write-once (2026-11-23).
- The break-glass role is assumable only by the owner's administrator role, and every
  assumption raises an alarm. Production needs a separate, approval-gated role (2026-11-23).
- Each TSS caller gets only its accesses: the API reads and signs uploads; the scan, step and
  sweeper functions get the pipeline access; the worker task gets one document's keys through
  `/resolve-worker`. No session may list a bucket (2026-11-24).
- A GuardDuty scan result counts as a pipeline job message for rule 4: the tenant comes from
  the registered upload's row, and the event's bucket must be that tenant's (2026-11-24).
- Each tenant's malware protection plan covers `inbox/` only. Inbox objects are never
  expired; they are never read again (2026-11-24).
- One document ID is reserved for the isolation tests' fixture objects. It never has a
  `documents` row, and the API and the pipeline refuse it (2026-11-23).
- Quarantined files: `quarantine/{doc_id}.pdf` for a PDF, and
  `quarantine/{file_id}.companion.json` for a companion file (2026-11-25).

**Sign-in and sessions (sections 4 and 11)**

- No Cognito VPC endpoint: the JWKS is read at `pulumi up` and Cognito admin calls run in a
  Lambda outside the VPC (2026-11-23).
- Refresh-token rotation on both app clients: `ALLOW_CUSTOM_AUTH` as the only explicit flow,
  rotation on, a 10-second grace period. A client never has an empty flow list (it would
  allow an SRP sign-in), and no pool may ever have a custom-auth trigger. Known effect: two
  tabs that share one refresh token cannot both refresh; the second is sent to sign-in
  (2026-11-25, both clients on by 2026-11-27).
- An MFA reset or a disable ends the user's sessions at once (`sessions_revoked_at`), not
  only their refresh tokens (2026-11-23).
- Idle timeout from stack config; cross-tab sign-out (2026-11-24).
- No custom Cognito message templates while email uses `COGNITO_DEFAULT`.

**Pipeline (section 8)**

- The API never reads `inbox/`. The scan Lambda validates companion files and reports every
  problem at once; a batch may be closed without a rejected companion file (2026-11-24).
- A batch open 24 hours is closed by the sweeper (2026-11-24).
- A password-protected PDF is put on hold whatever GuardDuty said (2026-11-24).
- A suspended tenant's documents get no new execution; they are queued again once the tenant
  is active (2026-11-24).
- The worker, attempts, the sandbox, step 9 as a Lambda step, the quarantine notice and
  "Mark as reviewed": section 8 (2026-11-24, 2026-11-25).
- A companion file attached or replaced after ingestion takes effect in the scan step, with
  the result that made it usable; no task runs and no model is called (2026-11-25).
- Lambda reserved concurrency: 50 for the scan function (a cap; results over it wait in
  Lambda's queue for up to an hour, then go to the scan dead-letter queue) and 300 for the
  TSS (a guarantee). Stack config, both or neither. An account must allow at least 750
  concurrent executions before they can be set; a new account allows 10 (2026-11-25).

**Stage 2 of the second half of step 6: text and Textract (section 8, step 5; all 2026-11-27)**

1. Page text and page pictures leave the sandbox by the page channel: one more inherited
   descriptor for the two page jobs, a pipe the parent reads one record at a time. Caps: 1 MB
   a page record, 8 MB a picture record, 5,000,000 bytes a picture, 128 MB for a document's
   text object.
2. A Textract call's `ai_usage` row: `service` `textract`, `operation` `DetectDocumentText`,
   no catalog model, no tokens, one unit of `page` at the configured price. One row per
   successful call.
3. The quality threshold is 0.90 (`textQualityThreshold`), and the score keeps its vowel
   factor (English only).
4. New permissions: `textract:DetectDocumentText` for the worker task's role, only through
   the VPC's Textract endpoint (`aws:SourceVpce`); the same statement for the isolation test
   role; the endpoint's policy narrowed from allow-everything to that action for the worker
   task's role alone; `app_worker` may insert its bound attempt's `pages` and `ai_usage` rows
   and set its bound attempt's content pointer, and can read neither table.
5. A document may have at most 1,000 pages (`textMaxPages`).
6. A page with images over about a tenth of it, or with form fields or stamps, goes to
   Textract even when its text layer is good. The text layer's text is then kept beside
   Textract's, marked as not the page's source. Only a page's source text feeds extraction,
   search and embedding.
7. A document with pages Textract could not read goes on; stage 4 makes it `needs_review`.
   The unread count shows in the step panel's Text line in the tenant view and the master
   view (the master step view gains the text step's four page counts, and nothing else).
8. What a Textract page costs is stack config (`textractPricePerPage`), never code.
9. The worker's session policy no longer names the stored companion file.
10. **Every version of the text object is kept.** No lifecycle rule and no delete permission.
    Each attempt's S3 version ID is in its `text` event, so an old version can be found
    without listing. Deleting a tenant's data (offboarding, a retention period, a deletion
    request) has no design yet and needs one before production, covering originals, derived
    objects and noncurrent versions together.
11. A spend guard: an alarm on Textract pages in 24 hours across the account
    (`textractDailyPagesAlarm`, 2,000), sent to the alarms topic.
12. The AI services check (the opt-out policy in effect, and Bedrock invocation logging off)
    is part of `scripts/check_deployment.py` in every mode, and must pass before any content
    goes to an AI service.

**After stage 2 (all 2026-11-27)**

1. Stage 2 is deployed and verified in dev: migration `0012`; the isolation suite passes (73,
   28, 101 and 28 checks); the step panel's Text line is right for the five synthetic files;
   1,000 scanned pages were read in 7 minutes 48 seconds, with a peak of 102 MB of the task's
   4,096 MB and no throttling.
2. The Upload screen states, after every addition, how many files were chosen and how many
   were added, until the next addition or until dismissed. "Not added" entries stay until
   dismissed. Nothing can be chosen while an addition runs, and a drop made then is listed by
   name. Each file goes to the upload link registered under its own name. A refused
   addition's or close's reason stays on the screen (section 11).
3. Stage 4 sets the final status inside the worker task and runs the companion step before
   the task or beside it (section 8). A task-token callback is rejected: it would give the
   worker task's role a Step Functions permission. Nothing changes before stage 4.
4. The worker task is right-sized only after real scanned documents have been measured: the
   102 MB peak is from synthetic one-bit scans.
5. `ai_usage` gets no read path for now. When the usage and cost view is built, it must show
   exactly 1,000 Textract rows for `11-scanned-1000-pages.pdf` (Infinity Loans, 2026-11-27).
6. Every isolation run makes one real Textract call, on a page the probe draws, and records
   it in `ai_usage`.
7. The scan role's limit to `inbox/` (section 6 above) is deployed and verified in dev: the
   deployed policy ends in `/inbox/*`; on Isolation Test A a clean file was scanned and
   processed and EICAR was quarantined with its security notice; all four plans stayed
   `ACTIVE`; the isolation suite passes with the same counts. The plans' status is read once
   more on 2026-11-28, because it is not known when GuardDuty next checks a plan's role.
7. The GuardDuty scan role's object access stops at `inbox/`, behind a stack switch (the
   tenant isolation list above). Tested first on Isolation Test A, with a clean file and
   EICAR.

**Audit and what master roles see (sections 5 and 7a)**

- Both master roles see every audit row's actor and target, tenant users' email addresses
  included: the audit record is not an admin list view (2026-11-23).
- Who opened a PDF is shown to tenant admins only (2026-11-24).
- Master admins see a failed step's code and error type; tenants see plain words
  (2026-11-24).

**Build and deploy (sections 2 and 3)**

- Lambda and task images are built by `scripts/build_image.py` with attestations off, pinned
  base images, and their dependencies and code as two layers (2026-11-25).
- A file Git marks as LF is LF on disk (2026-11-27).
- Staging and Production: raise the Lambda concurrency quota before the first deploy
  (2026-11-25).
