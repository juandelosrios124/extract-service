"""Tests for environment-based configuration (12-Factor III)."""

import pytest
from pydantic import ValidationError

from extractor.config import Settings

ENV_VARS = (
    "HOST",
    "PORT",
    "MAX_UPLOAD_SIZE",
    "THREAD_POOL_SIZE",
    "MAX_QUEUE_WAIT",
    "MAX_CONCURRENT_UPLOADS",
    "UPLOAD_SLOT_TIMEOUT",
    "EXTRACTION_DEADLINE",
    "MAX_PAGES",
    "LOG_LEVEL",
    "CONSUL_ENABLED",
    "CONSUL_HTTP_ADDR",
    "CONSUL_HTTP_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_settings_have_sensible_defaults():
    settings = Settings()

    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8001
    assert settings.MAX_UPLOAD_SIZE == 50 * 1024 * 1024
    assert settings.LOG_LEVEL == "INFO"


def test_los_defaults_son_los_valores_de_produccion():
    """Sin variables de entorno el servicio arranca protegido, no sin límites.

    Regresión de la issue #27: las variables quedaron en el proxy y nunca
    llegaron a las réplicas; el default de 0 dejó la admisión por cupos
    apagada un mes sin que se notara (issue #31).
    """
    settings = Settings()

    assert settings.THREAD_POOL_SIZE == 1
    assert settings.MAX_CONCURRENT_UPLOADS == 8
    assert settings.UPLOAD_SLOT_TIMEOUT == 5.0
    assert settings.MAX_QUEUE_WAIT == 15.0
    assert settings.EXTRACTION_DEADLINE == 8.0
    assert settings.MAX_PAGES == 10000


def test_el_tramo_de_extract_entra_en_el_presupuesto_de_timeouts():
    """cliente 30 s > documents 27 s > Traefik 25 s >= extract 23 s (issue #68).

    La espera por un cupo ya está dentro de MAX_QUEUE_WAIT (se cuenta desde
    que llega el pedido), así que el tramo de extract es
    MAX_QUEUE_WAIT + EXTRACTION_DEADLINE.
    """
    settings = Settings()

    assert settings.UPLOAD_SLOT_TIMEOUT < settings.MAX_QUEUE_WAIT
    assert settings.MAX_QUEUE_WAIT + settings.EXTRACTION_DEADLINE == 23.0


def test_settings_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("MAX_UPLOAD_SIZE", "1024")
    monkeypatch.setenv("THREAD_POOL_SIZE", "2")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.HOST == "127.0.0.1"
    assert settings.PORT == 9000
    assert settings.MAX_UPLOAD_SIZE == 1024
    assert settings.THREAD_POOL_SIZE == 2
    assert settings.LOG_LEVEL == "DEBUG"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "70000"),
        ("MAX_UPLOAD_SIZE", "0"),
        ("THREAD_POOL_SIZE", "0"),
        ("LOG_LEVEL", "VERBOSE"),
    ],
)
def test_settings_fail_fast_on_invalid_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_do_not_read_dotenv_files(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PORT=9999\n")
    monkeypatch.chdir(tmp_path)

    assert Settings().PORT == 8001


def test_consul_viene_apagado_por_defecto():
    """CONSUL_ENABLED=false: el servicio funciona igual sin Consul (issue #16)."""
    settings = Settings()

    assert settings.CONSUL_ENABLED is False
    assert settings.CONSUL_HTTP_ADDR == "http://consul:8500"
    assert settings.CONSUL_HTTP_TOKEN == ""


def test_consul_se_configura_por_entorno(monkeypatch):
    monkeypatch.setenv("CONSUL_ENABLED", "true")
    monkeypatch.setenv("CONSUL_HTTP_ADDR", "http://otro-consul:8500")
    monkeypatch.setenv("CONSUL_HTTP_TOKEN", "un-uuid")

    settings = Settings()

    assert settings.CONSUL_ENABLED is True
    assert settings.CONSUL_HTTP_ADDR == "http://otro-consul:8500"
    assert settings.CONSUL_HTTP_TOKEN == "un-uuid"
