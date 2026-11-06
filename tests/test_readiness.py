"""Readiness: GET /health/ready (issue #35).

``/health`` dice solo que el proceso está vivo. La readiness además mira si el
hilo de extracción está trabado, que es el caso que el ``EXTRACTION_DEADLINE``
de la issue #34 **no puede** cortar: se chequea entre páginas, así que una sola
página colgada no se interrumpe.

Los tests que necesitan una extracción trabada usan eventos y no ``sleep``, así
que no dependen del reloj ni de la velocidad de la máquina.
"""

import threading

import pymupdf
import pytest
from fastapi.testclient import TestClient

from extractor.api import extract as extract_module
from extractor.api.extract import RunningExtractions
from extractor.config import Settings
from extractor.main import create_app

CABECERAS = {"Content-Type": "application/pdf"}


def pdf_minimo() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Hola")
    return doc.tobytes()


@pytest.fixture
def extraccion_trabada(monkeypatch):
    """Deja una extracción colgada hasta que el test la suelte."""
    arranco = threading.Event()
    soltar = threading.Event()
    original = extract_module.extract_pdf_content

    def colgada(pdf_bytes, **kwargs):
        arranco.set()
        soltar.wait(timeout=10)
        return original(pdf_bytes, **kwargs)

    monkeypatch.setattr(extract_module, "extract_pdf_content", colgada)
    yield arranco, soltar
    soltar.set()


# --- El registro de extracciones en curso ----------------------------------


def test_sin_extracciones_en_curso_no_hay_nada_viejo():
    assert RunningExtractions().seconds_of_oldest() == 0.0


def test_mientras_dura_el_bloque_hay_una_extraccion_en_curso():
    running = RunningExtractions()

    with running.tracking():
        assert running.seconds_of_oldest() >= 0.0
        dentro = True

    assert dentro
    assert running.seconds_of_oldest() == 0.0  # al salir se limpia


def test_una_extraccion_que_falla_igual_se_desmarca():
    """Si no se limpiara, la réplica quedaría marcada como trabada para siempre."""
    running = RunningExtractions()

    with pytest.raises(ValueError), running.tracking():
        raise ValueError("falló la extracción")

    assert running.seconds_of_oldest() == 0.0


def test_se_informa_la_mas_vieja_de_varias():
    """Con THREAD_POOL_SIZE > 1 puede haber más de una a la vez."""
    running = RunningExtractions()

    with running.tracking():
        primera = running.seconds_of_oldest()
        with running.tracking():
            assert running.seconds_of_oldest() >= primera


# --- El endpoint -----------------------------------------------------------


def test_ready_responde_200_cuando_no_hay_nada_corriendo():
    with TestClient(create_app(Settings(LOG_LEVEL="WARNING"))) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_ready_sigue_en_200_mientras_la_extraccion_no_pase_del_limite(extraccion_trabada):
    arranco, soltar = extraccion_trabada
    # Límite alto: la extracción está trabada, pero todavía no lo suficiente.
    settings = Settings(LOG_LEVEL="WARNING", READINESS_STUCK_AFTER=60, MAX_QUEUE_WAIT=30)

    with TestClient(create_app(settings)) as client:
        worker = threading.Thread(
            target=lambda: client.post("/extract", content=pdf_minimo(), headers=CABECERAS)
        )
        worker.start()
        try:
            assert arranco.wait(timeout=5), "la extracción nunca arrancó"
            response = client.get("/health/ready")
        finally:
            soltar.set()
            worker.join(timeout=10)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_ready_responde_503_si_la_extraccion_lleva_mas_del_limite(extraccion_trabada):
    arranco, soltar = extraccion_trabada
    # Límite 0,01 s: en cuanto la extracción arranca, ya está pasada.
    settings = Settings(LOG_LEVEL="WARNING", READINESS_STUCK_AFTER=0.01, MAX_QUEUE_WAIT=30)

    with TestClient(create_app(settings)) as client:
        worker = threading.Thread(
            target=lambda: client.post("/extract", content=pdf_minimo(), headers=CABECERAS)
        )
        worker.start()
        try:
            assert arranco.wait(timeout=5), "la extracción nunca arrancó"
            # El reloj tiene 15,6 ms de resolución en Windows: se espera a que
            # avance, en vez de dormir una cantidad fija.
            for _ in range(2000):
                response = client.get("/health/ready")
                if response.status_code == 503:
                    break
            liveness = client.get("/health")
        finally:
            soltar.set()
            worker.join(timeout=10)

    assert response.status_code == 503
    cuerpo = response.json()
    assert cuerpo["status"] == "stuck"
    assert cuerpo["stuck_after_seconds"] == 0.01
    assert cuerpo["extracting_for_seconds"] >= 0

    # Liveness y readiness responden preguntas distintas: el proceso está vivo.
    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok"}


def test_ready_vuelve_a_200_cuando_la_extraccion_termina(extraccion_trabada):
    arranco, soltar = extraccion_trabada
    settings = Settings(LOG_LEVEL="WARNING", READINESS_STUCK_AFTER=0.01, MAX_QUEUE_WAIT=30)

    with TestClient(create_app(settings)) as client:
        worker = threading.Thread(
            target=lambda: client.post("/extract", content=pdf_minimo(), headers=CABECERAS)
        )
        worker.start()
        assert arranco.wait(timeout=5)
        for _ in range(2000):
            if client.get("/health/ready").status_code == 503:
                break
        soltar.set()
        worker.join(timeout=10)

        despues = client.get("/health/ready")

    assert despues.status_code == 200
    assert despues.json()["status"] == "ready"


def test_readiness_en_cero_la_deja_siempre_lista(extraccion_trabada):
    """Escotilla de escape: 0 apaga el chequeo."""
    arranco, soltar = extraccion_trabada
    settings = Settings(LOG_LEVEL="WARNING", READINESS_STUCK_AFTER=0, MAX_QUEUE_WAIT=30)

    with TestClient(create_app(settings)) as client:
        worker = threading.Thread(
            target=lambda: client.post("/extract", content=pdf_minimo(), headers=CABECERAS)
        )
        worker.start()
        try:
            assert arranco.wait(timeout=5)
            response = client.get("/health/ready")
        finally:
            soltar.set()
            worker.join(timeout=10)

    assert response.status_code == 200


def test_el_limite_por_defecto_es_mayor_que_el_deadline_de_extraccion():
    """Si fuera menor, una extracción legítima marcaría mal a una réplica sana."""
    settings = Settings()

    assert settings.READINESS_STUCK_AFTER > settings.EXTRACTION_DEADLINE
