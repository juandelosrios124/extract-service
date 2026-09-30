"""Genera un set PROVISORIO de 4 PDFs para las pruebas de carga.

No reemplaza a los PDFs oficiales de la cátedra: sirve para desarrollar y comparar
"antes/después" de cada optimización mientras no se consigan. Es determinista
(semilla fija), así que tu compañero y vos generan exactamente los mismos archivos.

Uso, desde la raíz del repo:
    uv run python tests/stress/generate_pdfs.py
    uv run python tests/stress/generate_pdfs.py --out tests/stress/pdfs_provisorios

Los nombres (01.pdf a 04.pdf) son los que esperan los scripts de k6 y Vegeta.
"""

import argparse
import random
from pathlib import Path

import pymupdf

ANCHO, ALTO = 595, 842  # A4 en puntos
SEMILLA = 42

PALABRAS = (
    "servicio extraccion documento pagina texto contenido cola carga prueba "
    "latencia balanceo replica proceso memoria cpu limite tiempo respuesta "
    "peticion cliente servidor archivo formato indice capitulo tabla figura "
    "resumen analisis resultado medicion metodo sistema red"
).split()

# nombre -> (descripcion, tipo de pagina, cantidad de paginas)
PERFILES = {
    "01.pdf": ("liviano, solo texto", "texto", 2),
    "02.pdf": ("mediano, solo texto", "texto", 40),
    "03.pdf": ("largo, 292 paginas de texto", "texto", 292),
    "04.pdf": ("pesado, graficos vectoriales e imagenes", "pesada", 3),
}

LADO_RUIDO = 1000  # 1000x1000 RGB de ruido = ~3 MB por pagina (no comprime)


def pagina_de_texto(doc, rng, lineas=62):
    page = doc.new_page(width=ANCHO, height=ALTO)
    y = 50
    for _ in range(lineas):
        linea = " ".join(rng.choices(PALABRAS, k=14)).capitalize()
        page.insert_text((50, y), linea, fontsize=9)
        y += 12


def pagina_pesada(doc, rng):
    page = doc.new_page(width=ANCHO, height=ALTO)
    # Capa vectorial: muchas lineas, costosa de parsear.
    forma = page.new_shape()
    for _ in range(1500):
        x, y = rng.uniform(0, ANCHO), rng.uniform(0, ALTO)
        forma.draw_line((x, y), (x + rng.uniform(-80, 80), y + rng.uniform(-80, 80)))
    forma.finish(color=(0.15, 0.25, 0.6), width=0.3)
    forma.commit()
    page.insert_text((50, 40), "Pagina con graficos y capas (provisoria)", fontsize=11)
    # Imagen de ruido: hace que el archivo pese, como un PDF con fotos.
    muestras = rng.randbytes(LADO_RUIDO * LADO_RUIDO * 3)
    pix = pymupdf.Pixmap(pymupdf.csRGB, LADO_RUIDO, LADO_RUIDO, muestras, False)
    page.insert_image(pymupdf.Rect(60, 120, 535, 595), pixmap=pix)


def generar(salida: Path) -> None:
    salida.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEMILLA)
    print(f"Generando en {salida}")
    for nombre, (descripcion, tipo, paginas) in PERFILES.items():
        doc = pymupdf.open()
        for _ in range(paginas):
            if tipo == "texto":
                pagina_de_texto(doc, rng)
            else:
                pagina_pesada(doc, rng)
        ruta = salida / nombre
        # Metadatos fijos y sin /ID nuevo: el mismo codigo genera los mismos bytes.
        doc.set_metadata({"creationDate": "D:20260101000000Z", "modDate": "D:20260101000000Z",
                          "producer": "generate_pdfs.py", "creator": "generate_pdfs.py"})
        doc.save(ruta, garbage=3, deflate=True, no_new_id=True)
        doc.close()
        mb = ruta.stat().st_size / (1024 * 1024)
        print(f"  {nombre}  {paginas:>3} paginas  {mb:6.2f} MB  ({descripcion})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genera PDFs provisorios de prueba.")
    parser.add_argument(
        "--out",
        default="tests/stress/pdfs_provisorios",
        help="carpeta de salida (por defecto: tests/stress/pdfs_provisorios)",
    )
    generar(Path(parser.parse_args().out))
