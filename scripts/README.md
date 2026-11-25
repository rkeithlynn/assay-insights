# scripts

Operational scripts, run with `uv run` from the repository root. Each reads the AWS
profile, Region and resource names from the Pulumi stack's config and outputs.

| Script | Does |
| --- | --- |
| `migrate.py` | Runs the migration Lambda: database roles, `alembic upgrade head`, and the RLS and role checks. `--command verify` is read-only. Run it after every `pulumi up` that changes `db/`. |
| `add_tenant.py` | Adds a business tenant and waits for provisioning: `--name`, `--admin-name`, `--admin-email`. Prints the tenant ID. `--retry <tenant id>` retries a failed tenant. |
| `run_isolation.py` | Runs `tests/isolation` inside the VPC against two tenants, then the provisioning role's probe, and prints each result. See `tests/isolation/README.md`. |
| `update_tenants.py` | Runs every active tenant's stack again (the provisioning task's `update` command), so a change to the tenant stack program reaches existing tenants: build step 6's bucket CORS and inbox-only malware scanning. `--tenant <id>` (repeatable) limits it. Changes AWS resources, as `pulumi up` does: the owner runs it, after `pulumi up`. |
| `make_test_files.py` | Writes synthetic browser-check files to `.test-files/` (git-ignored): a plain PDF, the same PDF under another name (duplicate), a password-protected PDF, a PDF with a valid companion file and one with an invalid one (four key errors, all reported at once), and plain text named `06-not-a-pdf.pdf`. `--tenant-id` puts that tenant's ID in the valid companion. Never the EICAR file (see CLAUDE.md, build step 6). |
| `ingest_check.py` | Exercises the pipeline in AWS through the portal's own API, signed in with your portal tab's access token (hidden prompt; never echoed, logged or stored). `bad-companion`: a PDF and a companion file with six errors; close must be refused. `eicar`: the EICAR test file, assembled in memory from a masked form, registered as a PDF; it must be quarantined. Prints the tenant and asks before creating anything; refuses any tenant but `--tenant`. Standard library only, Python 3.9+, so it also runs in AWS CloudShell. |
| `seed.py` | Seeds the environment from stack config: verifies each `aiModels` profile with a test call, loads the catalog, creates the platform tenant and its users, adds the business tenants through the portal's add-tenant path and waits for them, invites their users and gives every tenant its default models. Safe to run again. |
| `export_openapi.py` | Writes `portal/src/api/openapi.json` from the API; then `npm run gen:api` in `portal/`. `--check` fails if it is out of date. |
| `build_image.py` | Builds and pushes a container image. Run by Pulumi during `pulumi up`, not by hand. |
| `build_portal.py` | Builds the portal in its container and uploads it. Run by Pulumi during `pulumi up`, not by hand. |
