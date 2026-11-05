"""Tests for logging configuration (12-Factor XI: logs as event streams)."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.logging_config import configure_logging, request_id_var
from extractor.main import create_app


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_logs_are_written_to_stdout(capsys):
    configure_logging("INFO")

    logging.getLogger("extractor.test").info("evento de prueba")

    captured = capsys.readouterr()
    assert "evento de prueba" in captured.out
    assert "evento de prueba" not in captured.err


def test_log_level_filters_lower_severity(capsys):
    configure_logging("WARNING")

    logging.getLogger("extractor.test").info("no deberia salir")
    logging.getLogger("extractor.test").warning("si deberia salir")

    out = capsys.readouterr().out
    assert "no deberia salir" not in out
    assert "si deberia salir" in out


def test_configure_logging_is_idempotent(capsys):
    configure_logging("INFO")
    configure_logging("INFO")

    logging.getLogger("extractor.test").info("una sola vez")

    assert capsys.readouterr().out.count("una sola vez") == 1


def test_al_arrancar_loguea_la_configuracion_efectiva_sin_el_token(caplog):
    """Issue #31: la réplica deja por escrito qué configuración recibió."""
    settings = Settings(
        THREAD_POOL_SIZE=3,
        MAX_CONCURRENT_UPLOADS=2,
        CONSUL_HTTP_TOKEN="un-token-que-no-debe-aparecer",
    )

    with caplog.at_level(logging.INFO):
        with TestClient(create_app(settings)):
            pass

    assert "Configuración efectiva" in caplog.text
    assert "'THREAD_POOL_SIZE': 3" in caplog.text
    assert "'MAX_CONCURRENT_UPLOADS': 2" in caplog.text
    assert "un-token-que-no-debe-aparecer" not in caplog.text
    assert "CONSUL_HTTP_TOKEN" not in caplog.text


def test_el_formato_json_es_un_objeto_parseable_por_linea(capsys):
    """Issue #37: mismo formato que documents-service, para cruzar los logs."""
    configure_logging("INFO", "json")

    logging.getLogger("extractor.test").info("documento extraido", extra={"page_count": 7})

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["message"] == "documento extraido"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "extractor.test"
    assert payload["page_count"] == 7
    assert payload["request_id"] == "-"  # fuera de un pedido


def test_el_formato_texto_agrega_los_extras_como_clave_valor(capsys):
    configure_logging("INFO", "text")

    logging.getLogger("extractor.test").info("documento extraido", extra={"page_count": 7})

    out = capsys.readouterr().out
    assert "documento extraido" in out
    assert "page_count=7" in out


def test_el_request_id_viaja_en_cada_linea(capsys):
    configure_logging("INFO", "json")
    token = request_id_var.set("trace-123")
    try:
        logging.getLogger("extractor.test").info("algo paso")
    finally:
        request_id_var.reset(token)

    assert json.loads(capsys.readouterr().out.strip())["request_id"] == "trace-123"


def test_el_request_id_llega_a_los_logs_del_thread_de_extraccion(capsys):
    """De punta a punta, y regresion del caso sutil.

    La extraccion corre en un ThreadPoolExecutor y ``run_in_executor`` no copia
    los ContextVar al thread. Sin ``contextvars.copy_context()`` en
    ``extract.py``, esta linea -- la que dice cuantas paginas y por que fallo,
    o sea la util -- saldria con ``request_id`` en ``-``.
    """
    configure_logging("INFO", "json")

    with TestClient(create_app(Settings())) as client:
        client.post(
            "/extract",
            content=b"esto no es un pdf",
            headers={
                "Content-Type": "application/pdf",
                "X-Request-ID": "trace-de-punta-a-punta",
            },
        )

    lineas = [
        json.loads(linea)
        for linea in capsys.readouterr().out.splitlines()
        if linea.startswith("{")
    ]
    del_thread = [
        linea
        for linea in lineas
        if linea["logger"] == "extractor.services.pdf_extraction"
    ]
    assert del_thread, "la extraccion no logueo nada"
    assert all(linea["request_id"] == "trace-de-punta-a-punta" for linea in del_thread)
