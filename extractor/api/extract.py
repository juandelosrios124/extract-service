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

from extractor.api.dependencies import get_extraction_pool, get_settings
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
) -> ExtractResponse:
    started_at = time.monotonic()
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
