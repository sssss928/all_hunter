"""OCR post-processing utilities for authorized fixture testing."""

from .pipeline import choose_consensus, normalize_ocr_text

__all__ = ["choose_consensus", "normalize_ocr_text"]
