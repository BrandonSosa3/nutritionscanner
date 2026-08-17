"""FastAPI application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ns.api.routers import health, receipts
from ns.config import get_settings
from ns.db import dispose_engine
from ns.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    settings = get_settings()
    log.info("app.startup", environment=settings.environment.value)
    yield
    await dispose_engine()
    log.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="NutritionScanner",
        description="Grocery receipts to nutrition and cost intelligence.",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs in production — this is a single-user service and
        # the schema is not a public artifact.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(receipts.router)

    return app


app = create_app()
