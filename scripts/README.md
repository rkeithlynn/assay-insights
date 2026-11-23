# scripts

Operational scripts, run with `uv run` from the repository root. Each reads the AWS
profile, Region and resource names from the Pulumi stack's config and outputs.

| Script | Does |
| --- | --- |
| `migrate.py` | Runs the migration Lambda: database roles, `alembic upgrade head`, and the RLS and role checks. `--command verify` is read-only. Run it after every `pulumi up` that changes `db/`. |
| `add_tenant.py` | Adds a business tenant and waits for provisioning: `--name`, `--admin-name`, `--admin-email`. Prints the tenant ID. `--retry <tenant id>` retries a failed tenant. |
| `run_isolation.py` | Runs `tests/isolation` inside the VPC against two tenants, then the provisioning role's probe, and prints each result. See `tests/isolation/README.md`. |
| `build_image.py` | Builds and pushes a container image. Run by Pulumi during `pulumi up`, not by hand. |

`seed.py` arrives in build step 5.
