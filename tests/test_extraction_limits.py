"""Límites de la extracción: deadline y tope de páginas (issue #34).

Sin estos límites un PDF pesado o malicioso congela la réplica entera, porque
la extracción corre en el pool (``THREAD_POOL_SIZE=1``) y ``run_in_executor``
no se puede cancelar.
"""

import threading
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.main import create_app
from extractor.services.pdf_extraction import (
    ExtractionTimeout,
    TooManyPages,
    extract_pdf_content,
)

EXTRACT_URL = "/extract"
CABECERAS = {"Content-Type": "application/pdf"}


def pdf_con_paginas(cantidad: int, texto: str = "Pagina de prueba") -> bytes:
    doc = pymupdf.open()
    for _ in range(cantidad):
        doc.new_page().insert_text((72, 72), texto)
    return doc.tobytes()


@pytest.fixture(scope="module")
def pdf_5000_paginas() -> bytes:
    """El PDF del criterio de aceptación.

    Se arma con 50 páginas y 100 ``insert_pdf``, que tarda ~0,7 s; hacerlo
    página por página tarda ~4,5 s.
    """
    base = pymupdf.open()
    for i in range(50):
        base.new_page().insert_text((72, 72), f"Pagina {i} del bloque")
    doc = pymupdf.open()
    for _ in range(100):
        doc.insert_pdf(base)
    assert doc.page_count == 5000
    return doc.tobytes()


# --- Tope de páginas -------------------------------------------------------


def test_un_pdf_dentro_del_tope_se_extrae_normalmente():
    extraction = extract_pdf_content(pdf_con_paginas(3), max_pages=10)

    assert extraction.page_count == 3


def test_un_pdf_que_pasa_el_tope_no_se_extrae():
    with pytest.raises(TooManyPages) as error:
        extract_pdf_content(pdf_con_paginas(5), max_pages=4)

    assert error.value.page_count == 5
    assert error.value.max_pages == 4


def test_max_pages_en_cero_es_sin_limite():
    assert extract_pdf_content(pdf_con_paginas(5), max_pages=0).page_count == 5


# --- Deadline --------------------------------------------------------------


def test_un_deadline_holgado_no_interrumpe_nada():
    extraction = extract_pdf_content(pdf_con_paginas(5), deadline=30)

    assert extraction.page_count == 5


def test_un_deadline_vencido_corta_la_extraccion(pdf_5000_paginas):
    """Se usa un PDF grande de verdad y no un deadline infinitesimal.

    En Windows ``time.monotonic()`` es ``GetTickCount64()``, con 15,6 ms de
    resolución: con pocas páginas rápidas el reloj no alcanza a avanzar y el
    corte no se puede observar. 5000 páginas tardan ~330 ms, o sea unas 20
    veces la resolución, así que el corte es seguro.
    """
    with pytest.raises(ExtractionTimeout) as error:
        extract_pdf_content(pdf_5000_paginas, deadline=0.001)

    assert error.value.page_count == 5000
    assert error.value.pages_done < 5000  # cortó antes de terminar
    assert error.value.deadline == 0.001


def test_deadline_en_cero_es_sin_limite():
    assert extract_pdf_content(pdf_con_paginas(5), deadline=0).page_count == 5


def test_el_tope_de_paginas_se_chequea_antes_del_deadline(pdf_5000_paginas):
    """Con las dos cosas mal, gana el 422: no se gasta CPU en extraer."""
    with pytest.raises(TooManyPages):
        extract_pdf_content(pdf_5000_paginas, deadline=0.001, max_pages=5)


# --- Separación de páginas en content --------------------------------------


def test_las_paginas_se_separan_con_un_salto_de_linea():
    """Antes se concatenaban sin separador y dos páginas quedaban pegadas."""
    doc = pymupdf.open()
    for texto in ("Uno", "Dos", "Tres"):
        doc.new_page().insert_text((72, 72), texto)

    content = extract_pdf_content(doc.tobytes()).content

    # get_text() ya trae su propio "\n" al final, así que el separador deja
    # una línea en blanco entre páginas.
    assert "Uno\n\nDos\n\nTres" in content


# --- A través del endpoint -------------------------------------------------


def test_demasiadas_paginas_responde_422_sin_retry_after():
    settings = Settings(LOG_LEVEL="WARNING", MAX_PAGES=3)
    with TestClient(create_app(settings)) as client:
        response = client.post(EXTRACT_URL, content=pdf_con_paginas(10), headers=CABECERAS)

    assert response.status_code == 422
    assert "10" in response.json()["detail"] and "3" in response.json()["detail"]
    # Reintentar no cambia nada: el documento es el problema.
    assert "retry-after" not in response.headers


def test_el_deadline_agotado_responde_503_con_retry_after(pdf_5000_paginas):
    settings = Settings(LOG_LEVEL="WARNING", EXTRACTION_DEADLINE=0.001, MAX_QUEUE_WAIT=30)
    with TestClient(create_app(settings)) as client:
        response = client.post(EXTRACT_URL, content=pdf_5000_paginas, headers=CABECERAS)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


def test_un_pdf_de_5000_paginas_responde_y_la_replica_sigue_atendiendo(pdf_5000_paginas):
    """Criterio de aceptación de la issue #34.

    Con los defaults (`EXTRACTION_DEADLINE=8`, `MAX_PAGES=10000`) las 5000
    páginas entran dentro del deadline, y la réplica queda sana: después
    atiende otro pedido normal.

    El caso «mientras se extrae» con el pool ocupado está cubierto de forma
    determinista en ``test_health.py``, con eventos en vez de carrera.
    """
    settings = Settings(LOG_LEVEL="WARNING")
    assert settings.MAX_PAGES == 10000
    assert settings.EXTRACTION_DEADLINE == 8.0

    with TestClient(create_app(settings)) as client:
        grande: dict = {}

        def extraer() -> None:
            inicio = time.monotonic()
            grande["response"] = client.post(
                EXTRACT_URL, content=pdf_5000_paginas, headers=CABECERAS
            )
            grande["segundos"] = time.monotonic() - inicio

        worker = threading.Thread(target=extraer)
        worker.start()
        salud = client.get("/health")
        worker.join(timeout=30)
        assert not worker.is_alive(), "la extracción no terminó"

        # La réplica no quedó tomada: atiende otro pedido después.
        despues = client.post(EXTRACT_URL, content=pdf_con_paginas(2), headers=CABECERAS)

    assert salud.status_code == 200
    assert grande["response"].status_code == 200
    assert grande["response"].json()["page_count"] == 5000
    # Dentro del deadline, no cortado por él.
    assert grande["segundos"] < settings.EXTRACTION_DEADLINE
    assert despues.status_code == 200
    assert despues.json()["page_count"] == 2


def test_el_mismo_pdf_con_un_tope_mas_bajo_se_rechaza(pdf_5000_paginas):
    """El otro lado del criterio: si no entra, se rechaza rápido y sin extraer."""
    settings = Settings(LOG_LEVEL="WARNING", MAX_PAGES=1000)
    with TestClient(create_app(settings)) as client:
        inicio = time.monotonic()
        response = client.post(EXTRACT_URL, content=pdf_5000_paginas, headers=CABECERAS)
        segundos = time.monotonic() - inicio

    assert response.status_code == 422
    assert segundos < 2  # no se extrajo nada
