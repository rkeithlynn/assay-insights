# tests/isolation

Cross-tenant isolation tests against the real AWS account (BUILD_SPEC.md section 6). Not
run in CI.

The TSS and the database are private, so the tests cannot run from a developer's machine.
They run with pytest in a Lambda inside the VPC (`assay-dev-isolation-tests`, deployed only
when stack config `isolationTests` is true). `scripts/run_isolation.py` starts it and
prints the result.

## Running them

The tests need two active business tenants. The development stack uses two permanent
fixture tenants, "Isolation Test A" and "Isolation Test B".

```powershell
uv run scripts/run_isolation.py --tenant-a <tenant id> --tenant-b <tenant id>
```

To save the IDs so the script needs no arguments:

```powershell
pulumi config set isolationTenantIds "<tenant id>,<tenant id>" --cwd infra --stack pilartech/assay-platform/dev
```

The script exits 1 unless every test passed. A skipped test counts as a failure.

The script runs two things and waits for both:

1. The isolation test Lambda, which runs the tests in this directory.
2. The provisioning task with its `probe` command (`tss/assay_tss/provision/probe.py`).
   This part has to run as the provisioning role, and only that task can be it.

## What they show

Every cross-tenant test runs in both directions (A against B, and B against A).

| File | Shows |
| --- | --- |
| `test_storage.py` | A tenant's TSS-issued credentials work on its own bucket, key, vector index and face collection, and are refused on the other tenant's. `UPLOAD` cannot read and `READ` cannot write. Credentials last at most 15 minutes. A tenant session cannot start another session, cannot change key, bucket, vector bucket or collection tags, and cannot invoke the Cognito admin Lambda. |
| `test_storage.py` (runner probes) | The runner's own role, whose IAM policy *allows* it, still cannot assume the tenant role (trust policy), read, write or tag a tenant bucket (bucket policy), use or tag a tenant key (key policy), read tenant vectors, or invoke the Cognito admin Lambda. |
| `test_tss_api.py` | The TSS refuses an unsigned request, an unknown tenant, another service's name in the source identity, and any route but `resolve`. |
| `test_rows.py` | As `app_tenant` with one tenant set, the other tenant's rows cannot be read or inserted, `tenant_storage` cannot be read at all, and with no tenant set nothing is visible. |
| The provisioning role's probe | The provisioning role cannot read or write a tenant's objects, list object versions, read, query or write its vectors, or use its key directly. It may use the key only through S3 Vectors, to create the vector bucket and index (an accepted exception; BUILD_SPEC.md section 6). Three settings calls it is allowed must succeed. |

The tests write nothing. Every write they attempt must be refused, and every database
transaction is rolled back. Tagging probes use a tag no policy looks at.

## Adding a test

- Put it in this directory; the runner image copies the whole directory.
- Use `assert_denied` for a refusal. It passes only on an access-denied error, so a missing
  object or an unreachable endpoint cannot look like a refusal.
- Pair every "refused" test with one that shows the same call working for the rightful
  tenant, so an environment where everything fails cannot pass.
- Build step 8 adds the cross-tenant face search (BUILD_SPEC.md section 8a).
