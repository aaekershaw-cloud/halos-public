# PDF Extraction

The knowledge base supports two methods for extracting text from PDFs:

## Method 1: marker-pdf (Recommended)

**High-quality extraction with:**
- OCR for scanned documents
- Table extraction and formatting
- Image extraction
- Proper markdown formatting
- Better handling of complex layouts

**Requirements:**
- Python 3.10+ (required for marker-pdf dependencies)

**Installation:**
```bash
pip install marker-pdf
```

**Note:** If you're on Python 3.9, marker-pdf will fail to import due to dependency compatibility. The system will automatically fall back to pypdf. Upgrade to Python 3.10+ to use marker-pdf.

**Usage:**
```bash
# Uses marker-pdf automatically if installed
python3 -m kb.cli ingest pdf document.pdf

# Force basic extraction (skip marker-pdf)
python3 -m kb.cli ingest pdf document.pdf --no-marker

# Limit pages (useful for large PDFs)
python3 -m kb.cli ingest pdf document.pdf --max-pages 50
```

## Method 2: pypdf (Fallback)

**Basic text extraction:**
- Text-only extraction
- No OCR (scanned documents won't work)
- No table formatting
- Fast and lightweight

**Automatic fallback:**
- If marker-pdf not installed
- If marker-pdf extraction fails
- If `--no-marker` flag is used

## Comparison

| Feature | marker-pdf | pypdf |
|---------|-----------|-------|
| Text extraction | ✓ | ✓ |
| OCR (scanned PDFs) | ✓ | ✗ |
| Table extraction | ✓ | ✗ |
| Image extraction | ✓ | ✗ |
| Formatting preservation | ✓ | ✗ |
| Speed | Slower (AI models) | Fast |
| Dependencies | Heavy (~2GB models) | Lightweight |

## When to Use Each

**Use marker-pdf when:**
- PDF contains tables or images
- PDF is scanned (no text layer)
- Layout preservation is important
- Quality is more important than speed

**Use pypdf when:**
- Simple text-only PDFs
- Storage/bandwidth is limited
- Speed is critical
- marker-pdf not available

## Configuration

The system automatically detects marker-pdf availability:

```python
from kb.pdf_extract import is_marker_available

if is_marker_available():
    print("marker-pdf is available")
else:
    print("Using pypdf fallback")
```

## Performance

**marker-pdf:**
- First run: ~30s (downloads models)
- Subsequent runs: ~5-10s per page
- Models: ~2GB disk space

**pypdf:**
- ~0.1s per page
- No external dependencies

## Troubleshooting

**marker-pdf not loading:**
```bash
# Check installation
python3 -c "import marker; print('Installed')"

# Reinstall
pip uninstall marker-pdf
pip install marker-pdf
```

**Out of memory:**
```bash
# Limit pages
python3 -m kb.cli ingest pdf large.pdf --max-pages 100
```

**Poor quality extraction:**
```bash
# Try different method
python3 -m kb.cli ingest pdf doc.pdf --no-marker
```

## Extracted Metadata

Both methods extract metadata stored in frontmatter:

```yaml
---
id: abc-123
source_file: /path/to/document.pdf
source_type: pdf
extraction_method: marker-pdf  # or pypdf
pages: 42
images_extracted: 5  # marker-pdf only
pdf_metadata:
  Author: John Doe
  Title: Important Document
  CreationDate: 2024-01-01
---
```

## Examples

**Basic usage:**
```bash
python3 -m kb.cli ingest pdf research-paper.pdf
```

**Public document:**
```bash
python3 -m kb.cli ingest pdf whitepaper.pdf --classification public
```

**Large PDF (limit pages):**
```bash
python3 -m kb.cli ingest pdf huge-manual.pdf --max-pages 200
```

**Force basic extraction:**
```bash
python3 -m kb.cli ingest pdf simple.pdf --no-marker
```

**Confidential document:**
```bash
python3 -m kb.cli ingest pdf financial-report.pdf --classification confidential
```

## Integration with Compilation

After ingestion, PDFs are compiled into articles:

```bash
# Find the raw file ID
python3 -m kb.cli list raw

# Compile to article
python3 -m kb.cli compile file <raw-file-id> --auto-approve
```

The LLM will structure the extracted text into a proper knowledge base article.
