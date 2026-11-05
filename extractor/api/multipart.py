"""Parseo mínimo de multipart/form-data sobre un cuerpo ya leído en memoria."""

import re
from typing import Optional, Union

MULTIPART_FILE_FIELD = "file"

_BOUNDARY = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)
_FIELD_NAME = re.compile(
    r'content-disposition:[^\r\n]*?[;\s]name="([^"]*)"', re.IGNORECASE
)


def file_from_multipart(
    body: Union[bytes, bytearray], content_type: str
) -> Optional[memoryview]:
    """Devuelve una vista del campo ``file`` sobre ``body``, o None si no está.

    Es un ``memoryview`` y no una copia: con cuerpos de decenas de MB, copiar
    el campo duplicaba el pedido en memoria (issue #36).
    """
    boundary = _boundary_from(content_type)
    if boundary is None:
        return None

    delimiter = b"--" + boundary
    position = body.find(delimiter)
    while position != -1:
        position += len(delimiter)
        if body.startswith(b"--", position):
            return None  # delimitador de cierre
        headers_end = body.find(b"\r\n\r\n", position)
        if headers_end == -1:
            return None
        data_start = headers_end + 4
        data_end = body.find(b"\r\n" + delimiter, data_start)
        if data_end == -1:
            return None
        if _field_name(body[position:headers_end]) == MULTIPART_FILE_FIELD:
            return memoryview(body)[data_start:data_end]
        position = data_end + 2
    return None


def _boundary_from(content_type: str) -> Optional[bytes]:
    match = _BOUNDARY.search(content_type)
    if match is None:
        return None
    return (match.group(1) or match.group(2)).encode("latin-1")


def _field_name(headers: Union[bytes, bytearray]) -> Optional[str]:
    match = _FIELD_NAME.search(headers.decode("latin-1"))
    return match.group(1) if match else None
