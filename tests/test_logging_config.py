"""Tests for logging configuration (12-Factor XI: logs as event streams)."""

import logging

import pytest
from fastapi.testclient import TestClient

from extractor.config import Settings
from extractor.logging_config import configure_logging
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
