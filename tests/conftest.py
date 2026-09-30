import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(MAX_UPLOAD_SIZE=5 * 1024 * 1024, THREAD_POOL_SIZE=2, LOG_LEVEL="WARNING")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """Genera un PDF mínimo en memoria para los tests."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((100, 100), "Hola mundo desde el PDF de prueba")
    return doc.tobytes()
