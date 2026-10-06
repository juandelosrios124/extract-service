"""FastAPI dependencies resolved from the objects wired in the composition root."""

import asyncio
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import Request

from extractor.config import Settings






def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_extraction_pool(request: Request) -> ThreadPoolExecutor:
    return request.app.state.extraction_pool

def get_upload_slots(request: Request) -> Optional[asyncio.Semaphore]:
    return request.app.state.upload_slots

