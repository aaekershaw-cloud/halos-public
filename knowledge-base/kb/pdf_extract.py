"""Enhanced PDF extraction with marker-pdf support"""

import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Check if marker-pdf is available
try:
    from marker.convert import convert_single_pdf
    from marker.models import load_all_models
    MARKER_AVAILABLE = True
    _marker_models = None
except (ImportError, TypeError) as e:
    # TypeError can occur on Python 3.9 due to surya-ocr using 3.10+ union syntax
    MARKER_AVAILABLE = False
    logger.debug(f"marker-pdf not available: {e}")


def extract_pdf_basic(pdf_path: Path) -> Dict[str, Any]:
    """
    Extract PDF using basic pypdf (text only).

    Args:
        pdf_path: Path to PDF file

    Returns:
        {
            'content': str,      # Extracted markdown content
            'pages': int,        # Number of pages
            'method': str,       # Extraction method used
            'metadata': dict     # PDF metadata
        }
    """
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    text_parts = []

    for i, page in enumerate(reader.pages):
        page_text = page.extract_text()
        if page_text.strip():
            text_parts.append(f"## Page {i+1}\n\n{page_text}")

    content = '\n\n'.join(text_parts)

    # Extract metadata
    metadata = {}
    if reader.metadata:
        for key, value in reader.metadata.items():
            if value:
                metadata[key.lstrip('/')] = str(value)

    return {
        'content': content,
        'pages': len(reader.pages),
        'method': 'pypdf',
        'metadata': metadata
    }


def extract_pdf_marker(pdf_path: Path, max_pages: Optional[int] = None) -> Dict[str, Any]:
    """
    Extract PDF using marker-pdf (high quality, OCR, tables, formatting).

    Args:
        pdf_path: Path to PDF file
        max_pages: Optional limit on pages to process (None = all)

    Returns:
        {
            'content': str,      # Extracted markdown content
            'pages': int,        # Number of pages
            'method': str,       # Extraction method used
            'metadata': dict,    # PDF metadata
            'images': list       # Extracted images info
        }

    Raises:
        ImportError: If marker-pdf not installed
        Exception: If extraction fails
    """
    global _marker_models

    if not MARKER_AVAILABLE:
        raise ImportError(
            "marker-pdf not installed. Install with: pip install marker-pdf"
        )

    # Load models on first use (lazy loading)
    if _marker_models is None:
        logger.info("Loading marker-pdf models (this may take a moment)...")
        _marker_models = load_all_models()

    # Convert PDF to markdown
    try:
        result = convert_single_pdf(
            str(pdf_path),
            _marker_models,
            max_pages=max_pages,
            langs=['en'],  # Can be made configurable
        )

        markdown_content = result['markdown']
        metadata = result.get('metadata', {})
        images = result.get('images', [])

        # Count pages from metadata or estimate from content
        pages = metadata.get('pages', len(markdown_content.split('---')) if '---' in markdown_content else 1)

        return {
            'content': markdown_content,
            'pages': pages,
            'method': 'marker-pdf',
            'metadata': metadata,
            'images': images
        }

    except Exception as e:
        logger.error(f"marker-pdf extraction failed: {e}")
        raise


def extract_pdf(
    pdf_path: Path,
    use_marker: bool = True,
    fallback_to_basic: bool = True,
    max_pages: Optional[int] = None
) -> Dict[str, Any]:
    """
    Extract PDF with automatic method selection.

    Tries marker-pdf first if available and requested, falls back to pypdf.

    Args:
        pdf_path: Path to PDF file
        use_marker: Prefer marker-pdf if available (default: True)
        fallback_to_basic: Fall back to pypdf if marker fails (default: True)
        max_pages: Optional limit on pages (marker-pdf only)

    Returns:
        {
            'content': str,      # Extracted markdown content
            'pages': int,        # Number of pages
            'method': str,       # Extraction method used
            'metadata': dict,    # PDF metadata
            'images': list       # Extracted images (marker only)
        }
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Try marker-pdf first if requested and available
    if use_marker and MARKER_AVAILABLE:
        try:
            logger.info(f"Extracting PDF with marker-pdf: {pdf_path.name}")
            return extract_pdf_marker(pdf_path, max_pages=max_pages)
        except Exception as e:
            if fallback_to_basic:
                logger.warning(
                    f"marker-pdf extraction failed, falling back to pypdf: {e}"
                )
            else:
                raise

    # Use basic pypdf extraction
    logger.info(f"Extracting PDF with pypdf: {pdf_path.name}")
    result = extract_pdf_basic(pdf_path)

    # Add empty images list for consistency
    result['images'] = []

    return result


def is_marker_available() -> bool:
    """Check if marker-pdf is installed and available."""
    return MARKER_AVAILABLE
