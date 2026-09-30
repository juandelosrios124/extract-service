"""Tests for environment-based configuration (12-Factor III)."""

import pytest
from pydantic import ValidationError

from extractor.config import Settings

ENV_VARS = ("HOST", "PORT", "MAX_UPLOAD_SIZE", "THREAD_POOL_SIZE", "LOG_LEVEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_settings_have_sensible_defaults():
    settings = Settings()

    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8001
    assert settings.MAX_UPLOAD_SIZE == 50 * 1024 * 1024
    assert settings.THREAD_POOL_SIZE == 4
    assert settings.LOG_LEVEL == "INFO"


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
