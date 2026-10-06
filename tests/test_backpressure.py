"""Backpressure: no extraer pedidos que ya esperaron más de MAX_QUEUE_WAIT."""

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
def spy(monkeypatch):
    """Cuenta cuántas veces se llegó a extraer de verdad."""
    calls = []
    original = extract_module.extract_pdf_content

    def counting(pdf_bytes):
        calls.append(len(pdf_bytes))
        return original(pdf_bytes)

    monkeypatch.setattr(extract_module, "extract_pdf_content", counting)
    return calls


def test_con_el_pool_libre_extrae_normalmente(spy):
    app = create_app(Settings(THREAD_POOL_SIZE=1, MAX_QUEUE_WAIT=5))
    with TestClient(app) as client:
        response = post_pdf(client, make_pdf("Hola mundo"))

    assert response.status_code == 200
    assert "Hola mundo" in response.json()["content"]
    assert len(spy) == 1


def test_un_pedido_que_espero_demasiado_responde_503_sin_extraer(spy):
    app = create_app(Settings(THREAD_POOL_SIZE=1, MAX_QUEUE_WAIT=0.05))
    with TestClient(app) as client:
        # Ocupa el único worker más tiempo que MAX_QUEUE_WAIT.
        client.app.state.extraction_pool.submit(time.sleep, 0.4)
        response = post_pdf(client, make_pdf())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert spy == []  # no se gastó CPU en un pedido vencido


def test_despues_de_descartar_un_pedido_el_servicio_sigue_respondiendo(spy):
    app = create_app(Settings(THREAD_POOL_SIZE=1, MAX_QUEUE_WAIT=0.05))
    with TestClient(app) as client:
        client.app.state.extraction_pool.submit(time.sleep, 0.4)
        assert post_pdf(client, make_pdf()).status_code == 503

        time.sleep(0.5)  # el worker vuelve a estar libre
        response = post_pdf(client, make_pdf("Segundo"))

    assert response.status_code == 200
    assert len(spy) == 1


def test_un_pdf_invalido_sigue_dando_400(spy):
    app = create_app(Settings(THREAD_POOL_SIZE=1, MAX_QUEUE_WAIT=5))
    with TestClient(app) as client:
        response = post_pdf(client, b"esto no es un pdf")

    assert response.status_code == 400
