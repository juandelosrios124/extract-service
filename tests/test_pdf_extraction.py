"""Tests for the PDF extraction use case."""

import pymupdf
import pytest

from extractor.services.pdf_extraction import extract_pdf_content


def test_extract_pdf_content_returns_content_and_page_count(sample_pdf_bytes):
    result = extract_pdf_content(sample_pdf_bytes)

    assert result.page_count == 1
    assert "Hola mundo desde el PDF de prueba" in result.content


def test_extract_pdf_content_counts_every_page():
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()

    assert extract_pdf_content(doc.tobytes()).page_count == 3


@pytest.mark.parametrize("pdf_bytes", [b"esto no es un pdf", b""])
def test_extract_pdf_content_with_invalid_bytes_raises_value_error(pdf_bytes):
    with pytest.raises(ValueError):
        extract_pdf_content(pdf_bytes)


def test_extract_pdf_content_does_not_mask_unexpected_errors(monkeypatch):
    def broken_open(*args, **kwargs):
        raise RuntimeError("fallo interno")

    monkeypatch.setattr("extractor.services.pdf_extraction.pymupdf.open", broken_open)

    with pytest.raises(RuntimeError, match="fallo interno"):
        extract_pdf_content(b"%PDF-1.4")
