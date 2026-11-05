"""Correlación de pedidos (``X-Request-ID``).

Escrito como ASGI puro a propósito. ``BaseHTTPMiddleware`` de Starlette corre la
app en otra task, así que un ``ContextVar`` seteado en ``dispatch`` no se ve de
forma confiable dentro del endpoint: el request id quedaría en ``-`` en todas
las líneas de log del servicio. Seteándolo acá, en la misma task que corre la
app, se ve hasta la última capa.

Mismo enfoque y mismo header que ``documents-service`` (issue #37).
"""

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from extractor.logging_config import request_id_var

HEADER_NAME = b"x-request-id"


class RequestIdMiddleware:
    """Lee o genera un request id, lo expone y lo devuelve en la respuesta."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(HEADER_NAME)
        request_id = incoming.decode("latin-1") if incoming else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((HEADER_NAME, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            request_id_var.reset(token)
