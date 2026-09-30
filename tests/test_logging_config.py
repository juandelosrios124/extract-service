"""Tests for logging configuration (12-Factor XI: logs as event streams)."""

import logging

import pytest

from extractor.logging_config import configure_logging


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
