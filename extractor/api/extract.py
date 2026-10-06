"""Stateless PDF text extraction.

Accepts the PDF either as multipart/form-data (field ``file``) or as the raw
request body. The PDF is kept in memory end to end: the body is read into a
bounded buffer and multipart is parsed in memory instead of through
Starlette's form parser, which spools uploads larger than 1MB to disk.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from extractor.api.dependencies import get_extraction_pool, get_settings, get_upload_slots
from extractor.api.multipart import MULTIPART_FILE_FIELD, file_from_multipart
from extractor.api.schemas import ExtractResponse
from extractor.config import Settings
from extractor.services.pdf_extraction import PdfExtraction, extract_pdf_content

router = APIRouter()


class RequestExpired(Exception):
    """The request waited longer than MAX_QUEUE_WAIT before extraction could start."""


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
) -> ExtractResponse:
    started_at = time.monotonic()
    if upload_slots is None:
        return await _extract(request, settings, extraction_pool, started_at)

    await _acquire_slot(upload_slots, settings.UPLOAD_SLOT_TIMEOUT)
    try:
        return await _extract(request, settings, extraction_pool, started_at)
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
) -> ExtractResponse:
    body = await _read_body_within_limit(request, settings.MAX_UPLOAD_SIZE)
    pdf_bytes = _pdf_bytes_from(body, request.headers.get("content-type", ""))

    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se recibió ningún PDF",
        )

    try:
        extraction = await asyncio.get_running_loop().run_in_executor(
            extraction_pool,
            _extract_if_not_expired,
            pdf_bytes,
            started_at,
            settings.MAX_QUEUE_WAIT,
        )
    except RequestExpired:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio saturado: el pedido esperó demasiado en la cola",
            headers={"Retry-After": "1"},
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El contenido del archivo no es un PDF válido",
        )

    return ExtractResponse(content=extraction.content, page_count=extraction.page_count)


def _extract_if_not_expired(
    pdf_bytes: bytes, started_at: float, max_wait: float
) -> PdfExtraction:
    """Runs in a worker thread: skip the work if the request already expired in the queue."""
    if time.monotonic() - started_at > max_wait:
        raise RequestExpired
    return extract_pdf_content(pdf_bytes)


async def _read_body_within_limit(request: Request, max_size: int) -> bytes:
    """Read the request body, aborting as soon as it exceeds ``max_size``."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"El archivo supera el tamaño máximo de {max_size} bytes",
            )
    return bytes(body)


def _pdf_bytes_from(body: bytes, content_type: str) -> Optional[bytes]:
    if content_type.startswith("multipart/form-data"):
        return file_from_multipart(body, content_type)
    return body
