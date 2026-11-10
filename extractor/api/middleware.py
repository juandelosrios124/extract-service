"""Correlación de pedidos (``X-Request-ID``).

Escrito como ASGI puro a propósito. ``BaseHTTPMiddleware`` de Starlette corre la
app en otra task, así que un ``ContextVar`` seteado en ``dispatch`` no se ve de
forma confiable dentro del endpoint: el request id quedaría en ``-`` en todas
las líneas de log del servicio. Seteándolo acá, en la misma task que corre la
app, se ve hasta la última capa.

Un id entrante solo se respeta si cumple ``[A-Za-z0-9-]{1,64}`` (issue #69). Va
a cada línea de log y a la respuesta, así que un valor largo, con espacios,
saltos de línea o caracteres de control permitiría falsificar líneas de log o
headers. Cualquier otro valor se reemplaza por un ``uuid4().hex`` nuevo, que es
el único id que se usa en todo el pedido.

Mismo enfoque, mismo header y mismas reglas que ``documents-service``
(issue #37): los dos servicios aceptan exactamente los mismos ids.
"""

import logging
import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from extractor.logging_config import request_id_var

logger = logging.getLogger(__name__)

HEADER_NAME = b"x-request-id"

#: Lo único que se acepta de afuera. Sobre los bytes crudos del header, y con
#: fullmatch: con ``$`` un id terminado en "\n" pasaría.
VALID_REQUEST_ID = re.compile(rb"[A-Za-z0-9-]{1,64}")

#: Cuánto del valor rechazado se copia al log, escapado.
MAX_LOGGED_CHARS = 64


def resolve_request_id(incoming: bytes | None) -> str:
    """El id entrante si es válido; si no, un ``uuid4().hex`` nuevo."""
    if incoming is not None and VALID_REQUEST_ID.fullmatch(incoming):
        return incoming.decode("ascii")
    return uuid.uuid4().hex


def _escaped_prefix(raw: bytes) -> str:
    """Los primeros caracteres de un id rechazado, seguros para una línea de log."""
    return raw[:MAX_LOGGED_CHARS].decode("latin-1").encode("unicode_escape").decode("ascii")


class RequestIdMiddleware:
    """Lee o genera un request id, lo expone y lo devuelve en la respuesta."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(HEADER_NAME)
        request_id = resolve_request_id(incoming)
        token = request_id_var.set(request_id)
        if incoming is not None and request_id.encode("ascii") != incoming:
            # Ya con el id nuevo en el contexto: la línea sale con ese.
            logger.info(
                "X-Request-ID inválido, se generó uno nuevo",
                extra={
                    "request_id_recibido": _escaped_prefix(incoming),
                    "request_id_largo": len(incoming),
                },
            )

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((HEADER_NAME, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
