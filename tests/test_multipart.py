"""Tests del parseo de multipart/form-data en memoria."""

from extractor.api.multipart import file_from_multipart

BOUNDARY = "----TestBoundary7MA4YWxkTrZu0gW"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def build_multipart(parts, boundary=BOUNDARY):
    """Arma un cuerpo multipart como lo hacen curl, k6 y Vegeta (CRLF entre líneas).

    ``parts`` es una lista de tuplas ``(nombre, contenido, nombre_de_archivo)``.
    """
    body = b""
    for name, content, filename in parts:
        disposition = f'form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        body += (
            f"--{boundary}\r\n"
            f"Content-Disposition: {disposition}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + b"\r\n"
    return body + f"--{boundary}--\r\n".encode()


def test_devuelve_los_bytes_exactos_del_campo_file():
    pdf = b"%PDF-1.7\n" + bytes(range(256)) * 50 + b"\n%%EOF"
    body = build_multipart([("file", pdf, "a.pdf")])

    assert file_from_multipart(body, CONTENT_TYPE) == pdf


def test_conserva_saltos_de_linea_y_guiones_dentro_del_archivo():
    pdf = b"linea1\r\nlinea2\r\n--no-es-el-boundary\r\n\r\nfin"
    body = build_multipart([("file", pdf, "a.pdf")])

    assert file_from_multipart(body, CONTENT_TYPE) == pdf


def test_acepta_el_boundary_entre_comillas():
    pdf = b"%PDF-1.7 contenido"
    body = build_multipart([("file", pdf, "a.pdf")])
    content_type = f'multipart/form-data; boundary="{BOUNDARY}"'

    assert file_from_multipart(body, content_type) == pdf


def test_ignora_los_otros_campos_y_toma_solo_file():
    pdf = b"%PDF-1.7 contenido"
    body = build_multipart(
        [
            ("descripcion", b"hola", None),
            ("file", pdf, "a.pdf"),
            ("otro", b"x", None),
        ]
    )

    assert file_from_multipart(body, CONTENT_TYPE) == pdf


def test_devuelve_none_si_no_hay_campo_file():
    body = build_multipart([("documento", b"%PDF-1.7", "a.pdf")])

    assert file_from_multipart(body, CONTENT_TYPE) is None


def test_devuelve_none_si_el_content_type_no_tiene_boundary():
    body = build_multipart([("file", b"%PDF-1.7", "a.pdf")])

    assert file_from_multipart(body, "multipart/form-data") is None


def test_un_archivo_vacio_devuelve_bytes_vacios():
    body = build_multipart([("file", b"", "a.pdf")])

    assert file_from_multipart(body, CONTENT_TYPE) == b""
