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
- A caller needs three things, all in `infra/assay_infra/tss.py`: an entry in the
  `callers` argument (which gives it `execute-api:Invoke` and its service name), and a
  network path to the `execute-api` endpoint.

| Access | Allows |
| --- | --- |
| `UPLOAD` | Put new objects under `inbox/` only (for presigned upload links) |
| `READ` | Read objects and query vectors |
| `PIPELINE` | Read and write objects and vectors; use the face collection |

## Who can do what to a tenant's storage

| Principal | Bucket | Vector bucket | Key |
| --- | --- | --- | --- |
| Tenant role, tagged for this tenant | Read and write objects | Read and write vectors | Decrypt and generate data keys, only through S3 and S3 Vectors |
| Tenant role, tagged for another tenant | Nothing | Nothing | Nothing |
| Provisioning role | Settings, tags and the policy. No object data. It can list key names, because the AWS provider's `HeadBucket` needs `s3:ListBucket`; keys hold IDs only | The bucket, its index, tags and policy. No vectors | Administer, tag and set the policy. No decrypt. `GenerateDataKey` only through S3 Vectors, which requires it to create the vector bucket |
| GuardDuty scan role | Read and tag objects; write its one test object | Nothing | Decrypt and generate data keys through S3 |
| Break-glass role | Read objects; repair the bucket policy | Nothing (vectors can be rebuilt) | Decrypt; repair the key policy |
| Anyone else, including account administrators | Nothing | Nothing | Metadata only (description, policy, rotation, tags) |

Only the provisioning role may tag or untag a tenant key or bucket: the tenant role's
policy matches a key by its `tenant_id` tag. The other resources are matched by name,
which cannot change.
