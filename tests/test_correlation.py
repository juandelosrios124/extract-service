"""Correlación de pedidos con ``X-Request-ID`` (issue #37).

``documents-service`` ya propaga el header; acá se comprueba el otro extremo del
salto: que extract lo respete, lo exponga y lo devuelva.
"""

import logging
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from extractor.api.middleware import resolve_request_id
from extractor.config import Settings
from extractor.logging_config import request_id_var
from extractor.main import create_app

#: Lo que genera el middleware cuando no hay un id válido: ``uuid4().hex``.
GENERATED = re.compile(r"[0-9a-f]{32}")


def build_client() -> TestClient:
    return TestClient(create_app(Settings(LOG_LEVEL="WARNING")))


def test_se_respeta_el_request_id_que_llega():
    with build_client() as client:
        response = client.get("/health", headers={"X-Request-ID": "trace-abc"})

    assert response.headers["X-Request-ID"] == "trace-abc"


def test_se_genera_un_request_id_si_no_llega_ninguno():
    with build_client() as client:
        response = client.get("/health")

    assert uuid.UUID(response.headers["X-Request-ID"])


def test_cada_pedido_tiene_su_propio_request_id():
    with build_client() as client:
        primero = client.get("/health")
        segundo = client.get("/health")

    assert primero.headers["X-Request-ID"] != segundo.headers["X-Request-ID"]


def test_el_request_id_se_ve_dentro_del_endpoint():
    """Por esto el middleware es ASGI puro y no un ``BaseHTTPMiddleware``.

    ``BaseHTTPMiddleware`` corre la app en otra task, así que un ``ContextVar``
    seteado antes de ``call_next`` no se lee de forma confiable desde el
    endpoint: cada línea de log diría ``-`` en silencio.
    """
    visto: list[str] = []
    app = create_app(Settings(LOG_LEVEL="WARNING"))

    @app.get("/__sonda")
    async def sonda() -> dict[str, bool]:
        visto.append(request_id_var.get())
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/__sonda", headers={"X-Request-ID": "visible-adentro"})

    assert visto == ["visible-adentro"]


def test_fuera_de_un_pedido_el_request_id_queda_en_guion():
    """El default del ContextVar: los logs de arranque no inventan un id."""
    assert request_id_var.get() == "-"


def test_el_request_id_se_devuelve_tambien_cuando_el_pedido_falla():
    with build_client() as client:
        response = client.post(
            "/extract",
            content=b"esto no es un pdf",
            headers={"Content-Type": "application/pdf", "X-Request-ID": "trace-400"},
        )

    assert response.status_code == 400
    assert response.headers["X-Request-ID"] == "trace-400"


# --- Validación del id entrante (issue #69) ---------------------------------
# Mismas reglas que documents-service: ``[A-Za-z0-9-]{1,64}``. El id va a cada
# línea de log y a la respuesta, así que un valor largo, con espacios, saltos
# de línea o caracteres de control permitiría falsificar líneas de log.

VALID_IDS = ["abc-123", "trace-xyz", "A" * 64, "0", "550e8400-e29b-41d4-a716-446655440000"]

INVALID_IDS = [
    pytest.param("hola mundo", id="espacio"),
    pytest.param("con\nsalto", id="salto-de-linea"),
    pytest.param("con\r\nX-Falso: 1", id="crlf"),
    pytest.param("A" * 65, id="65-caracteres"),
    pytest.param("acción".encode(), id="acentos"),
    pytest.param("", id="vacio"),
    pytest.param("a_b", id="guion-bajo"),
    pytest.param("a.b/c", id="puntuacion"),
    pytest.param(b"ctl\x00\x1b[31m", id="control"),
]


def _raw(request_id) -> bytes:
    return request_id if isinstance(request_id, bytes) else request_id.encode("latin-1")


@pytest.mark.parametrize("request_id", VALID_IDS)
def test_un_request_id_valido_se_respeta(request_id):
    with build_client() as client:
        response = client.get("/health", headers={"X-Request-ID": request_id})

    assert response.headers["X-Request-ID"] == request_id


@pytest.mark.parametrize("request_id", INVALID_IDS)
def test_resolve_request_id_rechaza_lo_que_no_cumple(request_id):
    assert GENERATED.fullmatch(resolve_request_id(_raw(request_id)))


def test_resolve_request_id_sin_header_genera_uno():
    assert GENERATED.fullmatch(resolve_request_id(None))


def test_un_salto_de_linea_al_final_no_se_acepta():
    """Con ``re.match`` y ``$`` pasaría: ``$`` acepta un "\\n" final."""
    assert resolve_request_id(b"abc-123\n") != "abc-123"


@pytest.mark.parametrize(
    "request_id",
    [
        pytest.param("hola mundo", id="espacio"),
        pytest.param("A" * 65, id="65-caracteres"),
        pytest.param("a_b", id="guion-bajo"),
        pytest.param("a.b/c", id="puntuacion"),
    ],
)
def test_un_request_id_invalido_se_reemplaza_en_la_respuesta(request_id):
    with build_client() as client:
        response = client.get("/health", headers={"X-Request-ID": request_id})

    assert GENERATED.fullmatch(response.headers["X-Request-ID"])


def test_el_reemplazo_se_loguea_sin_el_valor_completo(caplog):
    recibido = "x" * 60 + " " + "secreto-que-no-va-al-log" * 10

    with build_client() as client:
        with caplog.at_level(logging.INFO, logger="extractor.api.middleware"):
            response = client.get("/health", headers={"X-Request-ID": recibido})

    [record] = [r for r in caplog.records if r.name == "extractor.api.middleware"]
    assert record.levelno == logging.INFO
    assert "X-Request-ID inválido" in record.getMessage()
    assert record.request_id_recibido == "x" * 60 + " sec"  # 64 caracteres
    assert "secreto-que" not in record.request_id_recibido
    assert record.request_id_largo == len(recibido)
    assert GENERATED.fullmatch(response.headers["X-Request-ID"])


def test_con_un_id_invalido_el_endpoint_ve_el_id_nuevo():
    """El id generado es el único: el de los logs del pedido y el de la respuesta."""
    visto: list[str] = []
    app = create_app(Settings(LOG_LEVEL="WARNING"))

    @app.get("/__sonda")
    async def sonda() -> dict[str, bool]:
        visto.append(request_id_var.get())
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/__sonda", headers={"X-Request-ID": "no valido"})

    assert visto == [response.headers["X-Request-ID"]]
    assert GENERATED.fullmatch(visto[0])


def test_el_valor_rechazado_se_loguea_escapado():
    """Un salto de línea o un carácter de control no llegan crudos al log."""
    from extractor.api.middleware import _escaped_prefix

    assert _escaped_prefix(b"ab\ncd\x1b") == "ab\\ncd\\x1b"


def test_un_request_id_valido_o_ausente_no_se_loguea(caplog):
    with build_client() as client:
        with caplog.at_level(logging.DEBUG, logger="extractor.api.middleware"):
            client.get("/health", headers={"X-Request-ID": "abc-123"})
            client.get("/health")

    assert not [r for r in caplog.records if r.name == "extractor.api.middleware"]
