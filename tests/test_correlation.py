"""Correlación de pedidos con ``X-Request-ID`` (issue #37).

``documents-service`` ya propaga el header; acá se comprueba el otro extremo del
salto: que extract lo respete, lo exponga y lo devuelva.
"""

import uuid

from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.logging_config import request_id_var
from extractor.main import create_app


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
