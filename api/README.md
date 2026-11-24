# api

FastAPI app on ECS Fargate (BUILD_SPEC.md section 10), package `assay_api`, plus the tenant
pool's Cognito pre-token trigger.

- `auth.py`: Cognito access-token verification against the JWKS baked in at deploy time.
- `security.py`: the route dependencies. Every route except `/api/health` takes its caller
  from one of them, which runs the account check in `accounts.py`.
- `database.py`: `app_tenant` transactions scoped with `SET LOCAL app.tenant_id`, and
  read-only `platform_reader` transactions.
- `cognito_pre_token.py`: adds `tenant_id` and `mfa` to tenant-pool access tokens.
- `routes/users.py`, `routes/admin.py`, `routes/auth_events.py`: users (tenant-admin), tenants
  and MFA resets (platform), and the sign-out and password-change audit rows (build step 5).
  `users.py` and `tenants.py` hold the logic, shared with the seed Lambda (`seed.py`).
- `services.py`: the two AWS calls, to the Cognito admin Lambda and the provisioning state
  machine.
- `cognito_admin.py`: the Cognito admin Lambda (two functions, limited by `OPERATIONS`).
- `cognito_auth_trigger.py` and `auth_event_consumer.py`: sign-in and password-reset audit
  rows, through SQS.

Images: `api/Dockerfile` (the API), `api/pre-token.Dockerfile` (the pre-token trigger),
`api/cognito-admin.Dockerfile` (both Cognito admin functions), `api/auth-trigger.Dockerfile`
(the auth-event trigger) and `api/lambda.Dockerfile` (the consumer and the seed Lambda), all
built by Pulumi through `scripts/build_image.py`.
