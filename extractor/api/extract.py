"""Stateless PDF text extraction.

Accepts the PDF either as multipart/form-data (field ``file``) or as the raw
request body. The PDF is kept in memory end to end: the body is read into a
bounded buffer and multipart is parsed in memory instead of through
Starlette's form parser, which spools uploads larger than 1MB to disk.
"""

import asyncio
import contextlib
import contextvars
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from extractor.api.dependencies import (
    get_extraction_pool,
    get_running_extractions,
    get_settings,
    get_upload_slots,
)
from extractor.api.multipart import MULTIPART_FILE_FIELD, file_from_multipart
from extractor.api.schemas import ExtractResponse
from extractor.config import Settings
from extractor.services.pdf_extraction import (
    ExtractionTimeout,
    PdfExtraction,
    TooManyPages,
    extract_pdf_content,
)

router = APIRouter()


class RequestExpired(Exception):
    """The request waited longer than MAX_QUEUE_WAIT before extraction could start."""


class RunningExtractions:
    """Las extracciones que están corriendo en el pool, con su hora de inicio.

    Sirve para la readiness (issue #35): si la más vieja lleva demasiado
    tiempo, el hilo está trabado y la réplica no puede atender, aunque
    ``/health`` siga diciendo ``ok``.

    El worker del pool escribe y el event loop lee, así que va con lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inicios: dict[object, float] = {}

    @contextlib.contextmanager
    def tracking(self) -> Iterator[None]:
        """Marca una extracción como en curso mientras dure el bloque."""
        marca = object()
        with self._lock:
            self._inicios[marca] = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self._inicios.pop(marca, None)

    def seconds_of_oldest(self) -> float:
        """Segundos que lleva la extracción más vieja. 0 si no hay ninguna."""
        with self._lock:
            if not self._inicios:
                return 0.0
            mas_vieja = min(self._inicios.values())
        return time.monotonic() - mas_vieja


@router.post(
    "",
    response_model=ExtractResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            MULTIPART_FILE_FIELD: {"type": "string", "format": "binary"}
                        },
                        "required": [MULTIPART_FILE_FIELD],
                    }
                },
                "application/pdf": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def extract_pdf(
    request: Request,
    settings: Settings = Depends(get_settings),
    extraction_pool: ThreadPoolExecutor = Depends(get_extraction_pool),
    upload_slots: Optional[asyncio.Semaphore] = Depends(get_upload_slots),
    running: "RunningExtractions" = Depends(get_running_extractions),
) -> ExtractResponse:
    started_at = time.monotonic()
    if upload_slots is None:
        return await _extract(request, settings, extraction_pool, started_at, running)

    await _acquire_slot(upload_slots, settings.UPLOAD_SLOT_TIMEOUT)
    try:
        return await _extract(request, settings, extraction_pool, started_at, running)
    finally:
        upload_slots.release()


async def _acquire_slot(upload_slots: asyncio.Semaphore, timeout: float) -> None:
    """Wait for a free slot without reading the body; answer 503 if none frees up in time."""
    try:
        await asyncio.wait_for(upload_slots.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio saturado: no hay cupo para atender el pedido",
            headers={"Retry-After": "1"},
        )


async def _extract(
    request: Request,
    settings: Settings,
    extraction_pool: ThreadPoolExecutor,
    started_at: float,
    running: "RunningExtractions",
) -> ExtractResponse:
    body = await _read_body_within_limit(request, settings.MAX_UPLOAD_SIZE)
    pdf_bytes = _pdf_bytes_from(body, request.headers.get("content-type", ""))

    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se recibió ningún PDF",
        )

    # run_in_executor no copia los ContextVar al thread, asi que sin esto las
    # lineas que loguea la extraccion saldrian con request_id "-" (issue #37).
    contexto = contextvars.copy_context()
    try:
        extraction = await asyncio.get_running_loop().run_in_executor(
            extraction_pool,
            contexto.run,
            _extract_if_not_expired,
            pdf_bytes,
            started_at,
            settings.MAX_QUEUE_WAIT,
            settings.EXTRACTION_DEADLINE,
            settings.MAX_PAGES,
            running,
        )
    except RequestExpired:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio saturado: el pedido esperó demasiado en la cola",
            headers={"Retry-After": "1"},
        )
    except ExtractionTimeout as error:
        # 503 y no 500: la réplica está sana, lo que se agotó es el presupuesto
        # de tiempo. Reintentar con menos carga puede andar (issue #34).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"La extracción no terminó en {error.deadline:g} s: "
                f"{error.pages_done} de {error.page_count} páginas"
            ),
            headers={"Retry-After": "1"},
        )
    except TooManyPages as error:
        # 422 y no 413: el tamaño está bien, lo que el servicio no procesa es
        # este documento. Reintentar no sirve, así que no lleva Retry-After.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"El PDF tiene {error.page_count} páginas y el máximo es {error.max_pages}"
            ),
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El contenido del archivo no es un PDF válido",
        )
    finally:
        # El PDF ya se extrajo: soltar la vista y el cuerpo ahora, en vez de
        # retenerlos mientras se serializa la respuesta (issue #36).
        pdf_bytes.release()
        del body

    return ExtractResponse(content=extraction.content, page_count=extraction.page_count)


def _extract_if_not_expired(
    pdf_bytes: memoryview,
    started_at: float,
    max_wait: float,
    deadline: float,
    max_pages: int,
    running: RunningExtractions,
) -> PdfExtraction:
    """Runs in a worker thread: skip the work if the request already expired in the queue."""
    if time.monotonic() - started_at > max_wait:
        raise RequestExpired
    # Mientras dure el bloque, la readiness sabe que este hilo está ocupado.
    with running.tracking():
        return extract_pdf_content(pdf_bytes, deadline=deadline, max_pages=max_pages)


async def _read_body_within_limit(request: Request, max_size: int) -> bytearray:
    """Read the request body, aborting as soon as it exceeds ``max_size``.

    Devuelve el ``bytearray`` tal cual: convertirlo a ``bytes`` duplicaba el
    cuerpo en memoria (issue #36).
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"El archivo supera el tamaño máximo de {max_size} bytes",
            )
    return body


def _pdf_bytes_from(body: bytearray, content_type: str) -> Optional[memoryview]:
    """Vista del PDF dentro de ``body``, sin copiarlo."""
    if content_type.startswith("multipart/form-data"):
        return file_from_multipart(body, content_type)
    return memoryview(body)
