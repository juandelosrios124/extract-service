"""Tests for GET /health.

The client fixture builds the real app without patching anything, so these
tests also prove the service starts without MongoDB, JWT or Ollama.
"""

import threading
import time

import pymupdf
from fastapi.testclient import TestClient

from extractor.api import extract as extract_module
from extractor.config import Settings
from extractor.main import create_app


def test_health_returns_200_with_ok_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_responde_de_inmediato_con_todos_los_cupos_ocupados(monkeypatch):
    """Regresión: si /health dependiera de los cupos, bajo carga Consul daría de
    baja a todas las réplicas a la vez y el servicio caería por completo."""
    extraction_started = threading.Event()
    release_extraction = threading.Event()
    original = extract_module.extract_pdf_content

    def blocked(pdf_bytes, **kwargs):
        extraction_started.set()
        release_extraction.wait(timeout=5)
        return original(pdf_bytes, **kwargs)

    monkeypatch.setattr(extract_module, "extract_pdf_content", blocked)

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Hola mundo")
    pdf = doc.tobytes()

    # Un único cupo y un único worker: un pedido en curso satura los dos límites.
    settings = Settings(
        THREAD_POOL_SIZE=1, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=5, MAX_QUEUE_WAIT=5
    )
    with TestClient(create_app(settings)) as client:
        occupant = {}
        worker = threading.Thread(
            target=lambda: occupant.update(
                response=client.post(
                    "/extract", content=pdf, headers={"Content-Type": "application/pdf"}
                )
            )
        )
        worker.start()
        try:
            assert extraction_started.wait(timeout=2), "el pedido nunca ocupó el cupo"

            started_at = time.monotonic()
            response = client.get("/health")
            elapsed = time.monotonic() - started_at

            assert worker.is_alive()  # el cupo seguía ocupado durante el healthcheck
        finally:
            release_extraction.set()
            worker.join()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < 0.5
    assert occupant["response"].status_code == 200
