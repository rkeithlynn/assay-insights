# tss

The Tenant Storage Service (BUILD_SPEC.md section 6): package `assay_tss`.

| Part | What it is | Runs in |
| --- | --- | --- |
| `models.py`, `keys.py`, `naming.py`, `client.py` | The client package: `StorageContext`, the only object-key builders, resource names, and the SigV4 client | The API and the workers (from build step 6) |
| `policies.py` | Every policy that keeps tenants apart: the tenant role's trust policy and template, session policies, and each tenant's bucket, vector bucket and key policy | Read by `infra/` and by the provisioning task |
| `service.py`, `handler.py` | `resolve`: reads `tenant_storage` as database role `tss` and assumes the tenant role for 15 minutes | The TSS Lambda (`tss/Dockerfile`), behind a private API with IAM authorization |
| `provision/` | `provision`: one Pulumi stack per tenant (`assay-tenant/<tenant_id>`), then the `tenant_storage` row, the first admin's invitation and status `active` | The provisioning Fargate task (`tss/provision.Dockerfile`), started by the provisioning state machine |

`offboard` is not built in the first deployment.

## Getting a storage context

```python
from assay_tss.client import TssClient
from assay_tss.models import StorageAccess, source_identity

tss = TssClient(endpoint_url, region=region)
ctx = tss.resolve(tenant_id, StorageAccess.READ, source_identity("api", f"request-{request_id}"))
s3 = ctx.session().client("s3")
s3.get_object(Bucket=ctx.bucket, Key=ctx.text_key(document_id))
```

- The tenant ID comes from the verified token or a pipeline job message, never from a
  request body or model output.
- Build keys only with the context's methods (`inbox_key`, `original_key`, ...). No other
  code formats an S3 key or names a KMS key.
- No key contains a file name. An upload goes to `inbox/{batch_id}/{file_id}`; its
  `file_name` is kept only in the database.
- The source identity is `<service>.<job>`. The TSS takes the service from the caller's
  IAM role and refuses a request that claims another one.
- Credentials last 15 minutes. Ask for a new context when `ctx.expired()` is true.
- A caller needs an entry in the `callers` argument of `TenantStorage` (a `TssCaller`: its
  role and the accesses it may ask for), which gives it `execute-api:Invoke`, its service name
  and its accesses (`TSS_CALLERS`), and a network path to the `execute-api` endpoint. The TSS
  refuses any other access (`access_not_allowed`).
- No access may list a bucket: callers work from database rows and scan events.

| Access | Allows |
| --- | --- |
| `UPLOAD` | Put new objects under `inbox/` only (for presigned upload links) |
| `READ` | Read objects under `originals/`, `derived/` and `chat/` (never `inbox/` or `quarantine/`), and query vectors |
| `PIPELINE` | Read and write every object, by key, and vectors; use the face collection |
| `WORKER` | One document's own keys: read its inbox files, read and write its original, text and fields objects. Not its stored companion file (the pipeline's companion step writes that; owner decision F, 2026-11-27). Nothing else in the bucket, no vectors, no face collection |

| Caller (service) | Route | Accesses |
| --- | --- | --- |
| `api` (the API's task role) | `/resolve` | `UPLOAD`, `READ` |
| `scan`, `pipeline`, `sweeper` (the pipeline's Lambdas) | `/resolve` | `PIPELINE` |
| `worker` (the pipeline's worker task) | `/resolve-worker` | `WORKER` |
| `isolation` (the isolation test runner, development stacks only) | `/resolve` | `UPLOAD`, `READ`, `PIPELINE` |

### The worker task's route

The worker task opens tenants' PDFs, so it is trusted least (build step 6, second half). It
never names a tenant and never asks for an access:

```python
ctx = tss.resolve_worker(document_id, attempt, token)
```

- `token` is the one-time token of the attempt's grant (`assay_db.grants`), which the
  pipeline's `claim` step created and Step Functions passed to that one task.
- The TSS takes the tenant from the grant's database row (`pipeline.worker_grant`, migration
  0010), and only while the grant is live: not ended, not expired, for the document's current
  attempt, with the document still `processing`. Anything else is `worker_grant_refused`,
  with no more detail than that.
- The credentials reach that one document's keys (`policies.worker_session_policy`): no
  wildcard, nothing under `quarantine/` or `chat/`, no other document.
- `/resolve` refuses the worker for every access, and `/resolve-worker` refuses every other
  caller: in the service, in each caller's own IAM policy, and in the API's resource policy,
  each of which names one route per caller.
- Credentials issued just before an attempt ends still last their 15 minutes (the shortest
  AWS STS allows).

## Who can do what to a tenant's storage

| Principal | Bucket | Vector bucket | Key |
| --- | --- | --- | --- |
| Tenant role, tagged for this tenant | Read and write objects | Read and write vectors | Decrypt and generate data keys, only through S3 and S3 Vectors |
| Tenant role, tagged for another tenant | Nothing | Nothing | Nothing |
| Provisioning role | Settings, tags and the policy. No object data. It can list key names, because the AWS provider's `HeadBucket` needs `s3:ListBucket`; keys hold IDs only | The bucket, its index, tags and policy. No vectors | Administer, tag and set the policy. `GenerateDataKey` and `Decrypt` only through S3 Vectors, which requires them to create the vector bucket and index; never directly or through S3. `tenant_id` is write-once |
| GuardDuty scan role | Read and tag objects under `inbox/`, the prefix a tenant's plan scans (stack config `malwareScanInboxOnly`; without it, every object); write its one test object | Nothing | Decrypt and generate data keys through S3 |
| Break-glass role | Read objects; repair the bucket policy | Nothing (vectors can be rebuilt) | Decrypt; repair the key policy |
| Anyone else, including account administrators | Nothing | Nothing | Metadata only (description, policy, rotation, tags) |

Only the provisioning role may tag or untag a tenant key or bucket: the tenant role's
policy matches a key by its `tenant_id` tag. The other resources are matched by name,
which cannot change.
