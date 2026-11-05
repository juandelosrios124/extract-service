"""Logging setup.

Follows 12-Factor App (XI. Logs): the service never manages log files, it
writes an unbuffered event stream to stdout and the environment routes it.
``LOG_FORMAT=json`` emits one JSON object per line para los recolectores de
logs; ``text`` queda legible en una terminal.

El request id del pedido en curso vive en un :class:`~contextvars.ContextVar`,
así cada línea lo puede llevar sin pasarlo como argumento por todas las capas.
El formato es el mismo que el de ``documents-service`` para que las dos salidas
se puedan cruzar por ``request_id`` (issue #37).
"""

import json
import logging
import sys
from contextvars import ContextVar

#: Lo setea ``RequestIdMiddleware`` mientras dura cada pedido.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class _ContextFilter(logging.Filter):
    """Pega el request id del pedido en curso en cada registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "request_id"}


class TextFormatter(logging.Formatter):
    """Línea legible, con los ``extra`` agregados como ``clave=valor``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        return f"{base} | {extras}" if extras else base


class JsonFormatter(logging.Formatter):
    """Un objeto JSON por línea."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str, log_format: str = "text") -> None:
    """Send every log record (including uvicorn's) to stdout at ``level``."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
