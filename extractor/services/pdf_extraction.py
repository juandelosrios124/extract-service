"""PDF text extraction use case.

SRP: only turns PDF bytes into text and a page count, entirely in memory.
Knows nothing about HTTP.
"""

import logging
from dataclasses import dataclass

import pymupdf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdfExtraction:
    """Result of extracting a PDF: its full text and number of pages."""

    content: str
    page_count: int


def extract_pdf_content(pdf_bytes: bytes) -> PdfExtraction:
    """Extract text and page count from PDF bytes.

    Raises:
        ValueError: If bytes are not a valid PDF.
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except pymupdf.FileDataError as e:
        logger.warning("Invalid PDF bytes: size=%d", len(pdf_bytes))
        raise ValueError("Invalid PDF bytes provided") from e

    with doc:
        extraction = PdfExtraction(
            content="".join(page.get_text() for page in doc),
            page_count=doc.page_count,
        )

    logger.info(
        "PDF text extraction completed: pages=%d chars=%d",
        extraction.page_count,
        len(extraction.content),
    )
    return extraction
