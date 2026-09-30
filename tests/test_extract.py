"""
Tests for POST /extract.

Follows the Arrange-Act-Assert pattern.
The endpoint is stateless: it extracts text in memory and never touches disk.
"""

import threading
from io import BytesIO
from unittest.mock import patch

import pymupdf
import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.main import create_app
from extractor.services.pdf_extraction import extract_pdf_content

EXTRACT_URL = "/extract"


@pytest.fixture
def two_page_pdf_bytes() -> bytes:
    doc = pymupdf.open()
    for text in ("Primera pagina", "Segunda pagina"):
        doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


class TestExtractEndpoint:

    def test_extract_returns_200_with_content_and_page_count(self, client, two_page_pdf_bytes):
        response = client.post(
            EXTRACT_URL,
            files={"file": ("doc.pdf", BytesIO(two_page_pdf_bytes), "application/pdf")},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["page_count"] == 2
        assert "Primera pagina" in data["content"]
        assert "Segunda pagina" in data["content"]

    def test_extract_accepts_raw_binary_body(self, client, sample_pdf_bytes):
        response = client.post(
            EXTRACT_URL,
            content=sample_pdf_bytes,
            headers={"Content-Type": "application/pdf"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["page_count"] == 1
        assert "Hola mundo desde el PDF de prueba" in data["content"]

    def test_extract_rejects_invalid_pdf(self, client):
        response = client.post(
            EXTRACT_URL,
            files={"file": ("fake.pdf", BytesIO(b"esto no es un pdf"), "application/pdf")},
        )

        assert response.status_code == 400
        assert "detail" in response.json()

    def test_extract_rejects_empty_body(self, client):
        response = client.post(
            EXTRACT_URL,
            content=b"",
            headers={"Content-Type": "application/pdf"},
        )

        assert response.status_code == 400
        assert "detail" in response.json()

    def test_extract_rejects_multipart_without_file(self, client):
        response = client.post(EXTRACT_URL, data={"otro_campo": "valor"})

        assert response.status_code == 400

    def test_extract_rejects_body_exceeding_size_limit(self):
        app = create_app(Settings(MAX_UPLOAD_SIZE=10))

        with TestClient(app) as small_limit_client:
            response = small_limit_client.post(
                EXTRACT_URL,
                content=b"%PDF-1.4" + b"x" * 100,
                headers={"Content-Type": "application/pdf"},
            )

        assert response.status_code == 413

    def test_extract_multipart_does_not_spool_to_disk(self, client, sample_pdf_bytes):
        # Starlette's form parser spools uploads > 1MB to a temp file on disk.
        with patch("starlette.formparsers.SpooledTemporaryFile") as mock_spooled, \
             patch("tempfile.NamedTemporaryFile") as mock_named_tmp, \
             patch("tempfile.mkstemp") as mock_mkstemp:
            response = client.post(
                EXTRACT_URL,
                files={"file": ("doc.pdf", BytesIO(sample_pdf_bytes), "application/pdf")},
            )

        assert response.status_code == 200
        mock_spooled.assert_not_called()
        mock_named_tmp.assert_not_called()
        mock_mkstemp.assert_not_called()


class TestExtractionThreadPool:

    def test_pool_size_comes_from_settings(self, client, settings):
        assert client.app.state.extraction_pool._max_workers == settings.THREAD_POOL_SIZE

    def test_extraction_runs_in_the_dedicated_pool(self, client, sample_pdf_bytes):
        thread_names = []

        def spy(pdf_bytes):
            thread_names.append(threading.current_thread().name)
            return extract_pdf_content(pdf_bytes)

        with patch("extractor.api.extract.extract_pdf_content", spy):
            client.post(
                EXTRACT_URL,
                content=sample_pdf_bytes,
                headers={"Content-Type": "application/pdf"},
            )

        assert thread_names and thread_names[0].startswith("pdf-extract")

    def test_pool_is_shut_down_when_app_stops(self, settings):
        app = create_app(settings)
        with TestClient(app):
            pool = app.state.extraction_pool

        with pytest.raises(RuntimeError):
            pool.submit(lambda: None)
