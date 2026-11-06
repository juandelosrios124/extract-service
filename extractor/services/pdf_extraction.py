"""PDF text extraction use case.

SRP: only turns PDF bytes into text and a page count, entirely in memory.
Knows nothing about HTTP.

La extracción corre en un thread del pool y ``run_in_executor`` no se puede
cancelar, así que un PDF pesado o malicioso congelaría la réplica entera
(``THREAD_POOL_SIZE=1``). Por eso hay dos límites (issue #34):

* ``max_pages``: se rechaza antes de extraer nada, mirando ``page_count``.
* ``deadline``: se extrae **página por página** y se corta al pasarse.

El deadline se chequea *entre* páginas, que es la granularidad más fina
posible sin aislar la extracción en otro proceso: una única página que tarde
más que el deadline igual se completa. Lo que evita es el caso real, que son
muchas páginas.
"""

import logging
import time
from dataclasses import dataclass
from typing import Union

import pymupdf

logger = logging.getLogger(__name__)

PdfBytes = Union[bytes, bytearray, memoryview]


class ExtractionTimeout(Exception):
    """Se agotó el deadline antes de terminar de extraer."""

    def __init__(self, pages_done: int, page_count: int, deadline: float) -> None:
        super().__init__(
            f"Extraction stopped after {pages_done} of {page_count} pages "
            f"({deadline}s deadline)"
        )
        self.pages_done = pages_done
        self.page_count = page_count
        self.deadline = deadline


class TooManyPages(Exception):
    """El PDF tiene más páginas de las que el servicio acepta."""

    def __init__(self, page_count: int, max_pages: int) -> None:
        super().__init__(f"PDF has {page_count} pages, the maximum is {max_pages}")
        self.page_count = page_count
        self.max_pages = max_pages


@dataclass(frozen=True)
class PdfExtraction:
    """Result of extracting a PDF: its full text and number of pages."""

    content: str
    page_count: int


def extract_pdf_content(
    pdf_bytes: PdfBytes,
    *,
    deadline: float = 0.0,
    max_pages: int = 0,
) -> PdfExtraction:
    """Extract text and page count from PDF bytes.

    Args:
        pdf_bytes: El PDF. No se copia: puede ser un ``memoryview``.
        deadline: Segundos máximos de extracción. ``0`` = sin límite.
        max_pages: Páginas máximas aceptadas. ``0`` = sin límite.

    Raises:
        ValueError: If bytes are not a valid PDF.
        TooManyPages: Si el PDF supera ``max_pages``.
        ExtractionTimeout: Si la extracción pasa el ``deadline``.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except pymupdf.FileDataError as e:
        logger.warning("Invalid PDF bytes: size=%d", len(pdf_bytes))
        raise ValueError("Invalid PDF bytes provided") from e

    with doc:
        page_count = doc.page_count

        # Antes de gastar CPU: page_count sale del xref, es barato.
        if max_pages and page_count > max_pages:
            logger.warning("PDF rejected: pages=%d max_pages=%d", page_count, max_pages)
            raise TooManyPages(page_count, max_pages)

        corte = time.monotonic() + deadline if deadline else None
        paginas: list[str] = []
        for pagina in doc:
            if corte is not None and time.monotonic() > corte:
                logger.warning(
                    "PDF extraction stopped by deadline: "
                    "pages_done=%d page_count=%d deadline=%.1fs",
                    len(paginas),
                    page_count,
                    deadline,
                )
                raise ExtractionTimeout(len(paginas), page_count, deadline)
            paginas.append(pagina.get_text())

        extraction = PdfExtraction(content="\n".join(paginas), page_count=page_count)

    logger.info(
        "PDF text extraction completed: pages=%d chars=%d",
        extraction.page_count,
        len(extraction.content),
    )
    return extraction
