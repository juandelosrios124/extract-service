"""FastAPI dependencies resolved from the objects wired in the composition root."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import Request

from extractor.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_extraction_pool(request: Request) -> ThreadPoolExecutor:
    return request.app.state.extraction_pool


def get_upload_slots(request: Request) -> Optional[asyncio.Semaphore]:
    return request.app.state.upload_slots


def get_running_extractions(request: Request):
    """El registro de extracciones en curso, que usa la readiness (issue #35)."""
    return request.app.state.running_extractions
