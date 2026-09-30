"""Composition root: builds the FastAPI app from explicit settings."""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI

from extractor.api import extract, health
from extractor.config import Settings


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # PyMuPDF is CPU-bound: run it off the event loop in a bounded pool.
        with ThreadPoolExecutor(
            max_workers=settings.THREAD_POOL_SIZE,
            thread_name_prefix="pdf-extract",
        ) as extraction_pool:
            app.state.extraction_pool = extraction_pool
            yield

    app = FastAPI(title="PDF Extract Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(extract.router, prefix="/extract", tags=["Extract"])
    app.include_router(health.router, prefix="/health", tags=["Health"])
    return app
