"""The FastAPI application. Every route is under /api (BUILD_SPEC.md section 10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from assay_api.auth import TokenVerifier
from assay_api.database import Databases
from assay_api.routes import (
    admin,
    audit,
    auth_events,
    batches,
    documents,
    health,
    ingestion,
    me,
    models,
    users,
)
from assay_api.services import (
    CognitoAdmin,
    LambdaCognitoAdmin,
    Provisioning,
    StepFunctionsProvisioning,
)
from assay_api.settings import Settings
from assay_api.storage import IngestQueue, SqsIngestQueue, Storage, TssStorage

API_PREFIX = "/api"


def create_app(
    *,
    verifier: TokenVerifier,
    databases: Databases,
    cognito: CognitoAdmin,
    provisioning: Provisioning,
    # Build step 6. Left out only by tests that never reach a batch or document route.
    storage: Storage | None = None,
    ingest: IngestQueue | None = None,
) -> FastAPI:
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
    app.state.cognito = cognito
    app.state.provisioning = provisioning
    app.state.storage = storage
    app.state.ingest = ingest
    for router in (
        health.router,
        me.router,
        auth_events.router,
        users.router,
        admin.router,
        audit.router,
        models.router,
        batches.router,
        documents.router,
        ingestion.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    return app


def app_from_env() -> FastAPI:
    settings = Settings.from_env()
    region = settings.db.region
    return create_app(
        verifier=TokenVerifier(settings.pools),
        databases=Databases.for_target(settings.db),
        cognito=LambdaCognitoAdmin(settings.cognito_admin_function, region=region),
        provisioning=StepFunctionsProvisioning(settings.provisioning_state_machine, region=region),
        storage=TssStorage(settings.tss_endpoint_url, region=region),
        ingest=SqsIngestQueue(settings.ingest_queue_url, region=region),
    )
