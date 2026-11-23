"""Database transactions for the two kinds of request.

- Tenant-pool requests run as `app_tenant`. Every transaction starts with
  `SET LOCAL app.tenant_id` (through `set_config(..., true)`), taken from the verified token,
  so row-level security shows that tenant's rows and nothing else (section 6, rule 5).
- Platform-pool requests run as `platform_reader`, in read-only transactions. That role can
  read `tenants`, `audit_log` and the views in schema `platform`, and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text

from assay_db.connect import DbTarget, iam_engine
from assay_db.roles import APP_TENANT, PLATFORM_READER

APPLICATION_NAME = "assay-api"
POOL_SIZE = 4


class Databases:
    def __init__(self, *, tenant_engine: Engine, platform_engine: Engine) -> None:
        self._tenant = tenant_engine
        self._platform = platform_engine

    @classmethod
    def for_target(cls, target: DbTarget) -> Databases:
        def engine(user: str) -> Engine:
            return iam_engine(target, user, application_name=APPLICATION_NAME, pool_size=POOL_SIZE)

        return cls(tenant_engine=engine(APP_TENANT), platform_engine=engine(PLATFORM_READER))

    @contextmanager
    def tenant(self, tenant_id: uuid.UUID) -> Iterator[Connection]:
        """One transaction as app_tenant, scoped to `tenant_id`. Commits on success."""
        with self._tenant.begin() as conn:
            conn.execute(
                text("SELECT pg_catalog.set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            yield conn

    @contextmanager
    def platform(self) -> Iterator[Connection]:
        """One read-only transaction as platform_reader."""
        with self._platform.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            yield conn

    def dispose(self) -> None:
        self._tenant.dispose()
        self._platform.dispose()
