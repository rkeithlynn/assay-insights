# api

FastAPI app on ECS Fargate (BUILD_SPEC.md section 10), package `assay_api`, plus the tenant
pool's Cognito pre-token trigger.

- `auth.py`: Cognito access-token verification against the JWKS baked in at deploy time.
- `security.py`: the route dependencies. Every route except `/api/health` takes its caller
  from one of them, which runs the account check in `accounts.py`.
- `database.py`: `app_tenant` transactions scoped with `SET LOCAL app.tenant_id`, and
  read-only `platform_reader` transactions.
- `cognito_pre_token.py`: adds `tenant_id` and `mfa` to tenant-pool access tokens.

The images are `api/Dockerfile` (the API) and `api/pre-token.Dockerfile` (the trigger),
both built by Pulumi through `scripts/build_image.py`.
