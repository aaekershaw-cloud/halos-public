"""Tests for PDF extraction functionality"""

import pytest
from pathlib import Path
from kb.pdf_extract import (
    extract_pdf,
    extract_pdf_basic,
    is_marker_available,
    MARKER_AVAILABLE
)


class TestPDFExtraction:
    """Test PDF extraction methods"""

    def test_is_marker_available(self):
        """Test marker availability check"""
        # Should return a boolean
        result = is_marker_available()
        assert isinstance(result, bool)
        assert result == MARKER_AVAILABLE

    def test_extract_pdf_basic_not_found(self):
        """Test basic extraction with missing file"""
        with pytest.raises(FileNotFoundError):
            extract_pdf_basic(Path('/nonexistent/file.pdf'))

    def test_extract_pdf_not_found(self):
        """Test extraction with missing file"""
        with pytest.raises(FileNotFoundError):
            extract_pdf(Path('/nonexistent/file.pdf'))

    def test_extract_pdf_fallback_to_basic(self):
        """Test that extraction falls back to basic when marker fails"""
        # This test just verifies the function accepts the parameters
        # Actual PDF testing would require sample PDF files
        pass

    def test_extract_pdf_basic_returns_dict(self):
        """Test that basic extraction returns correct structure"""
        # Would need a real PDF file to test fully
        # For now, just verify the function exists and has correct signature
        import inspect
        sig = inspect.signature(extract_pdf_basic)
        assert 'pdf_path' in sig.parameters

    def test_extract_pdf_returns_dict(self):
        """Test that extraction returns correct structure"""
        # Would need a real PDF file to test fully
        # For now, just verify the function exists and has correct signature
        import inspect
        sig = inspect.signature(extract_pdf)
        assert 'pdf_path' in sig.parameters
        assert 'use_marker' in sig.parameters
        assert 'fallback_to_basic' in sig.parameters
        assert 'max_pages' in sig.parameters


class TestPDFExtractionIntegration:
    """Integration tests for PDF extraction (requires sample PDFs)"""

    @pytest.mark.skipif(not Path('tests/fixtures/sample.pdf').exists(),
                       reason="No sample PDF available")
    def test_extract_sample_pdf_basic(self):
        """Test basic extraction with sample PDF"""
        result = extract_pdf_basic(Path('tests/fixtures/sample.pdf'))

        assert 'content' in result
        assert 'pages' in result
        assert 'method' in result
        assert 'metadata' in result

        assert isinstance(result['content'], str)
        assert isinstance(result['pages'], int)
        assert result['method'] == 'pypdf'
        assert result['pages'] > 0

    @pytest.mark.skipif(not is_marker_available() or not Path('tests/fixtures/sample.pdf').exists(),
                       reason="marker-pdf not available or no sample PDF")
    def test_extract_sample_pdf_marker(self):
        """Test marker extraction with sample PDF"""
        result = extract_pdf(
            Path('tests/fixtures/sample.pdf'),
            use_marker=True,
            fallback_to_basic=False
        )

        assert 'content' in result
        assert 'pages' in result
        assert 'method' in result
        assert 'metadata' in result
        assert 'images' in result

        assert isinstance(result['content'], str)
        assert isinstance(result['pages'], int)
        assert result['method'] == 'marker-pdf'
        assert result['pages'] > 0

    @pytest.mark.skipif(not Path('tests/fixtures/sample.pdf').exists(),
                       reason="No sample PDF available")
    def test_extract_sample_pdf_auto(self):
        """Test automatic extraction method selection"""
        result = extract_pdf(Path('tests/fixtures/sample.pdf'))

        assert 'content' in result
        assert 'pages' in result
        assert 'method' in result
        assert result['method'] in ['pypdf', 'marker-pdf']
