"""Content-based file type detection.

Wraps Google's magika (https://github.com/google/magika) to classify a file by
its actual bytes rather than its extension. Falls back to extension-based
detection when magika isn't installed, so the KB keeps working if the optional
dependency is missing.
"""

import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

try:
    from magika import Magika
    _magika = Magika()
    _available = True
except ImportError:
    _magika = None
    _available = False
    logger.info("magika not installed — using extension-based fallback")


# Magika labels that should flow through the text ingest path
TEXT_LABELS = {
    "markdown", "txt", "text", "html", "xml", "rtf",
    "json", "yaml", "toml", "csv", "tsv", "ini",
    "python", "javascript", "typescript", "rust", "go", "ruby",
    "java", "kotlin", "swift", "scala", "c", "cpp", "csharp",
    "shell", "bash", "zsh", "powershell", "perl", "php", "r",
    "css", "scss", "sql", "dockerfile", "makefile",
    "latex", "tex", "rst", "asciidoc", "diff", "patch",
    "sourcecode", "log", "env", "gitconfig",
}

PDF_LABELS = {"pdf"}


def detect(filepath: Union[str, Path]) -> dict:
    """Classify a file for the ingest pipeline.

    Returns a dict with:
        label:  str   — magika label (e.g. "markdown", "pdf", "jpeg")
        mime:   str   — MIME type, or "" when unknown
        score:  float — confidence 0.0–1.0 (1.0 from extension fallback)
        kind:   str   — "text" | "pdf" | "binary" for routing
        source: str   — "magika" or "extension"
    """
    p = Path(filepath)

    if _available:
        try:
            r = _magika.identify_path(p)
            label = str(r.output.label)
            mime = str(r.output.mime_type) if r.output.mime_type else ""
            score = float(r.score)
            if label in PDF_LABELS:
                kind = "pdf"
            elif label in TEXT_LABELS:
                kind = "text"
            else:
                kind = "binary"
            return {"label": label, "mime": mime, "score": score,
                    "kind": kind, "source": "magika"}
        except Exception as e:
            logger.warning(f"magika failed on {p}: {e}; using extension fallback")

    return _detect_by_extension(p)


def _detect_by_extension(p: Path) -> dict:
    ext = p.suffix.lower()
    if ext == ".pdf":
        return {"label": "pdf", "mime": "application/pdf", "score": 1.0,
                "kind": "pdf", "source": "extension"}
    text_exts = {
        ".md", ".markdown", ".txt", ".rst", ".log",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".env",
        ".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go",
        ".rs", ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".hpp",
        ".cs", ".sh", ".bash", ".zsh", ".ps1", ".pl", ".php", ".r",
        ".css", ".scss", ".html", ".xml", ".sql", ".csv", ".tsv",
        ".tex", ".diff", ".patch",
    }
    if ext in text_exts:
        return {"label": ext.lstrip("."), "mime": "text/plain", "score": 1.0,
                "kind": "text", "source": "extension"}
    return {"label": "unknown", "mime": "", "score": 1.0,
            "kind": "binary", "source": "extension"}


def is_available() -> bool:
    """True if magika is installed; false if we're running on extension fallback."""
    return _available
