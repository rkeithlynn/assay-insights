# scripts

Operational scripts, run with `uv run` from the repository root. Each reads the AWS
profile, Region and resource names from the Pulumi stack's config and outputs.

| Script | Does |
| --- | --- |
| `migrate.py` | Runs the migration Lambda: database roles, `alembic upgrade head`, and the RLS and role checks. `--command verify` is read-only. Run it after every `pulumi up` that changes `db/`. |
| `add_tenant.py` | Adds a business tenant and waits for provisioning: `--name`, `--admin-name`, `--admin-email`. Prints the tenant ID. `--retry <tenant id>` retries a failed tenant. |
| `run_isolation.py` | Runs `tests/isolation` inside the VPC against two tenants, then the provisioning role's probe, and prints each result. See `tests/isolation/README.md`. |
| `seed.py` | Seeds the environment from stack config: verifies each `aiModels` profile with a test call, loads the catalog, creates the platform tenant and its users, adds the business tenants through the portal's add-tenant path and waits for them, invites their users and gives every tenant its default models. Safe to run again. |
| `export_openapi.py` | Writes `portal/src/api/openapi.json` from the API; then `npm run gen:api` in `portal/`. `--check` fails if it is out of date. |
| `build_image.py` | Builds and pushes a container image. Run by Pulumi during `pulumi up`, not by hand. |
| `build_portal.py` | Builds the portal in its container and uploads it. Run by Pulumi during `pulumi up`, not by hand. |
