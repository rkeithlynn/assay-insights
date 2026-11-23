"""The FastAPI application. Every route is under /api (BUILD_SPEC.md section 10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from assay_api.auth import TokenVerifier
from assay_api.database import Databases
from assay_api.routes import health, me
from assay_api.settings import Settings

API_PREFIX = "/api"


def create_app(*, verifier: TokenVerifier, databases: Databases) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        databases.dispose()

    app = FastAPI(
        title="Assay Insights API",
        version="0.1.0",
        # The portal's API client is generated from this schema. No interactive docs.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.verifier = verifier
    app.state.databases = databases
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(me.router, prefix=API_PREFIX)
    return app


def app_from_env() -> FastAPI:
    settings = Settings.from_env()
    return create_app(
        verifier=TokenVerifier(settings.pools), databases=Databases.for_target(settings.db)
    )
