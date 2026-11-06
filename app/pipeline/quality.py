"""Heuristics that judge whether extracted text is usable.

Used twice: to decide if a "text" PDF page actually needs OCR (broken fonts
produce garbage such as (cid:12) or reversed Arabic presentation forms), and to
validate OCR output.
"""
import re
import unicodedata

_CID = re.compile(r"\(cid:\d+\)")
_WORD = re.compile(r"\w+", re.U)


def text_quality(text: str) -> float:
    """Return 0..1, where 1 means clean, readable text."""
    text = text.strip()
    if len(text) < 20:
        return 0.0 if not text else 0.5
    n = len(text)
    bad = len(_CID.findall(text)) * 8 + text.count("�") * 3
    presentation_forms = sum(1 for c in text if "ﭐ" <= c <= "﷿" or "ﹰ" <= c <= "﻿")
    control = sum(1 for c in text if unicodedata.category(c) in ("Cc", "Co", "Cn") and c not in "\n\t\r")
    letters = sum(1 for c in text if c.isalpha())
    score = 1.0
    score -= min(1.0, bad / n * 5)
    score -= min(0.6, presentation_forms / n * 3)   # Arabic shaped glyphs = broken extraction
    score -= min(0.6, control / n * 5)
    if letters / n < 0.45:
        score -= 0.3
    words = _WORD.findall(text)
    if words:
        avg = sum(len(w) for w in words) / len(words)
        if avg < 2.0 or avg > 14:                    # letters split apart or glued together
            score -= 0.35
        singles = sum(1 for w in words if len(w) == 1) / len(words)
        if singles > 0.4:
            score -= 0.3
    return max(0.0, min(1.0, score))


def detect_language(text: str) -> str:
    """Very small script-based language guess, used for routing and mock mode."""
    counts = {"ar": 0, "latin": 0, "cjk": 0, "cyrillic": 0, "hebrew": 0}
    for c in text[:5000]:
        o = ord(c)
        if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
            counts["ar"] += 1
        elif c.isascii() and c.isalpha() or 0x00C0 <= o <= 0x024F:
            counts["latin"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF:
            counts["cjk"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x0590 <= o <= 0x05FF:
            counts["hebrew"] += 1
    best = max(counts, key=counts.get)
    return {"latin": "en", "cjk": "zh", "cyrillic": "ru", "hebrew": "he"}.get(best, best) if counts[best] else "unknown"
