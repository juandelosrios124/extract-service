"""Composition root: builds the FastAPI app from explicit settings."""

import asyncio
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from extractor.api import extract, health
from extractor.api.extract import RunningExtractions
from extractor.api.middleware import RequestIdMiddleware
from extractor.config import Settings
from extractor.services.consul import Registrar, build_registrar, traefik_tags

logger = logging.getLogger(__name__)


def create_app(settings: Settings, *, consul_registrar: Optional[Registrar] = None) -> FastAPI:
    """Arma la app. ``consul_registrar`` permite inyectar el registro en los tests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Qué configuración recibió realmente esta réplica. Sin esto, una
        # variable que no llega (como en #27) pasa inadvertida.
        logger.info(
            "Configuración efectiva: %s",
            settings.model_dump(exclude={"CONSUL_HTTP_TOKEN"}),
        )

        # PyMuPDF is CPU-bound: run it off the event loop in a bounded pool.
        with ThreadPoolExecutor(
            max_workers=settings.THREAD_POOL_SIZE,
            thread_name_prefix="pdf-extract",
        ) as extraction_pool:
            app.state.extraction_pool = extraction_pool
            # Readiness (issue #35): qué extracciones están corriendo y desde
            # cuándo. Vive acá porque el pool es de este lifespan.
            app.state.running_extractions = RunningExtractions()
            # Created here because the semaphore must belong to the running event loop.
            app.state.upload_slots = (
                asyncio.Semaphore(settings.MAX_CONCURRENT_UPLOADS)
                if settings.MAX_CONCURRENT_UPLOADS > 0
                else None
            )

            # Service discovery (issue #16): opcional y nunca fatal. Se hace en
            # un thread porque el cliente de Consul es bloqueante.
            registrar = consul_registrar
            if settings.CONSUL_ENABLED and registrar is None:
                registrar = await asyncio.to_thread(
                    build_registrar,
                    consul_addr=settings.CONSUL_HTTP_ADDR,
                    token=settings.CONSUL_HTTP_TOKEN,
                    port=settings.PORT,
                    tags=traefik_tags(settings),
                )
            registered = (
                await asyncio.to_thread(registrar.register)
                if settings.CONSUL_ENABLED and registrar is not None
                else False
            )

            try:
                yield
            finally:
                # Solo se desregistra lo que se llegó a registrar.
                if registered and registrar is not None:
                    await asyncio.to_thread(registrar.deregister)


    app = FastAPI(title="PDF Extract Service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    # Lo mas externo: todo lo que loguee el pedido lleva su request id.
    app.add_middleware(RequestIdMiddleware)
    app.include_router(extract.router, prefix="/extract", tags=["Extract"])
    app.include_router(health.router, prefix="/health", tags=["Health"])
    return app
