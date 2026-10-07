"""Backpressure: límite de pedidos simultáneos por réplica (MAX_CONCURRENT_UPLOADS)."""

import threading
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from extractor.api import extract as extract_module
from extractor.config import Settings
from extractor.main import create_app


def make_pdf(text: str = "Hola mundo") -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def post_pdf(client: TestClient, pdf: bytes):
    return client.post("/extract", content=pdf, headers={"Content-Type": "application/pdf"})


@pytest.fixture
def slow_extraction(monkeypatch):
    """Hace que cada extracción tarde 0,4 s para poder solapar pedidos."""
    original = extract_module.extract_pdf_content

    def slow(pdf_bytes):
        time.sleep(0.4)
        return original(pdf_bytes)

    monkeypatch.setattr(extract_module, "extract_pdf_content", slow)


def test_sin_cupo_el_segundo_pedido_recibe_503(slow_extraction):
    settings = Settings(
        THREAD_POOL_SIZE=2, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.05, MAX_QUEUE_WAIT=5
    )
    with TestClient(create_app(settings)) as client:
        first = {}
        worker = threading.Thread(target=lambda: first.update(response=post_pdf(client, make_pdf())))
        worker.start()
        time.sleep(0.1)  # el primero ya tiene el cupo y sigue extrayendo
        second = post_pdf(client, make_pdf())
        worker.join()

    assert second.status_code == 503
    assert second.headers["retry-after"] == "1"
    assert first["response"].status_code == 200


def test_si_el_cupo_se_libera_a_tiempo_el_pedido_espera_y_se_atiende(slow_extraction):
    settings = Settings(
        THREAD_POOL_SIZE=2, MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=5, MAX_QUEUE_WAIT=5
    )
    with TestClient(create_app(settings)) as client:
        first = {}
        worker = threading.Thread(target=lambda: first.update(response=post_pdf(client, make_pdf())))
        worker.start()
        time.sleep(0.1)
        second = post_pdf(client, make_pdf("Segundo"))
        worker.join()

    assert second.status_code == 200
    assert first["response"].status_code == 200


def test_el_cupo_se_libera_aunque_el_pedido_falle():
    settings = Settings(MAX_CONCURRENT_UPLOADS=1, UPLOAD_SLOT_TIMEOUT=0.2)
    with TestClient(create_app(settings)) as client:
        assert post_pdf(client, b"esto no es un pdf").status_code == 400
        assert post_pdf(client, b"tampoco").status_code == 400
        assert post_pdf(client, make_pdf()).status_code == 200


def test_sin_limite_configurado_no_se_rechaza_nada(slow_extraction):
    with TestClient(create_app(Settings(THREAD_POOL_SIZE=2, MAX_QUEUE_WAIT=5))) as client:
        results = []
        workers = [
            threading.Thread(target=lambda: results.append(post_pdf(client, make_pdf()).status_code))
            for _ in range(2)
        ]
        [w.start() for w in workers]
        [w.join() for w in workers]

    assert results == [200, 200]
