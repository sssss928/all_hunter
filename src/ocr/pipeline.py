"""OCR normalization and confidence helpers for authorized test fixtures."""

from __future__ import annotations

from collections import Counter
import re
from typing import Iterable


def normalize_ocr_text(value: object, *, alphanumeric_only: bool = True) -> str:
    """Normalize fixture OCR output to uppercase text without whitespace."""

    text = re.sub(r"\s+", "", str(value or "")).upper()
    if alphanumeric_only:
        text = re.sub(r"[^0-9A-Z]", "", text)
    return text


def choose_consensus(
    candidates: Iterable[object],
    *,
    minimum_votes: int = 2,
) -> tuple[str, float]:
    """Return the normalized majority result and a simple confidence ratio."""

    normalized = [
        normalize_ocr_text(candidate)
        for candidate in candidates
    ]
    normalized = [candidate for candidate in normalized if candidate]
    if not normalized:
        return "", 0.0
    value, votes = Counter(normalized).most_common(1)[0]
    confidence = votes / len(normalized)
    if votes < max(1, minimum_votes):
        return "", confidence
    return value, confidence
