# Assay Insights

Multi-tenant AWS SaaS for querying archives of signed PDF loan documents through AI chat.

- Build contract: [BUILD_SPEC.md](BUILD_SPEC.md)
- Working rules for this repository: [CLAUDE.md](CLAUDE.md)

## Setup (PowerShell)

```powershell
uv sync
uv run pytest
cd infra; pulumi preview --stack pilartech/dev
# After the owner runs `pulumi up`: create database roles and run migrations
uv run scripts/migrate.py
```
