"""Apagado prolijo (12-Factor IX, issue #72).

Un pedido puede durar hasta MAX_QUEUE_WAIT + EXTRACTION_DEADLINE (23 s, el
tramo de extract en el presupuesto de timeouts). Al apagar una réplica:

* uvicorn deja de aceptar conexiones y espera a los pedidos en curso hasta
  ``SHUTDOWN_TIMEOUT``; después corre el lifespan, que la desregistra de Consul;
* Docker espera ``stop_grace_period`` antes de mandar SIGKILL.

Por eso: tramo de extract < SHUTDOWN_TIMEOUT < stop_grace_period.
"""

import re
from pathlib import Path

import pytest

from extractor import __main__ as entrypoint
from extractor.config import Settings

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


@pytest.fixture
def uvicorn_run(monkeypatch):
    """Captura los argumentos con los que se arranca uvicorn, sin arrancarlo."""
    calls: list[dict] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(entrypoint, "configure_logging", lambda *args, **kwargs: None)
    return calls


def _stop_grace_period_seconds() -> int:
    match = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*$", COMPOSE.read_text(), re.MULTILINE)
    assert match, "docker-compose.yml no define stop_grace_period en segundos"
    return int(match.group(1))


def test_el_timeout_de_apagado_por_defecto_es_25_segundos():
    assert Settings().SHUTDOWN_TIMEOUT == 25.0


def test_el_apagado_espera_mas_que_el_pedido_mas_largo():
    """Si fuera menor, apagar una réplica cortaría pedidos legítimos."""
    settings = Settings()

    assert settings.SHUTDOWN_TIMEOUT > settings.MAX_QUEUE_WAIT + settings.EXTRACTION_DEADLINE


def test_docker_espera_mas_que_uvicorn():
    """Si Docker matara antes, el lifespan no llegaría a desregistrar la réplica."""
    assert _stop_grace_period_seconds() > Settings().SHUTDOWN_TIMEOUT


def test_uvicorn_arranca_con_el_timeout_de_apagado(uvicorn_run, monkeypatch):
    monkeypatch.setenv("SHUTDOWN_TIMEOUT", "21")

    entrypoint.main()

    [kwargs] = uvicorn_run
    assert kwargs["timeout_graceful_shutdown"] == 21
