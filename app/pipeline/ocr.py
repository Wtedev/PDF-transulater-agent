"""OCR route for scanned pages and photos, with validation.

Primary engine: Claude vision (handles Arabic, two columns, headings and
handwriting far better than classic OCR). Tesseract runs on every page as an
independent cross-check and as the engine in mock mode.

Validation score per page = combination of
  - the vision model's own confidence,
  - text quality heuristics (garbled words, broken glyphs),
  - agreement with Tesseract (word overlap).
Pages under OCR_MIN_CONFIDENCE are flagged for human review.
"""
import io
import logging
import re

from .. import config, llm
from .quality import text_quality

log = logging.getLogger("ocr")

OCR_INSTRUCTIONS = """You are a meticulous OCR agent for book pages.
Transcribe the page image exactly, in its original language. Do not translate, summarize or fix wording.
Skip running headers, footers and page numbers. Keep reading order (for two columns read the first column fully, then the second; for Arabic/Hebrew pages read right to left).
Return JSON only:
{"blocks":[{"kind":"h1|h2|h3|p|quote|list|code","text":"..."}],
 "confidence":0.0-1.0,
 "handwritten":true|false,
 "notes":"illegible parts, cut text, images with text, etc."}
Use "h1" only for a chapter title. Join lines of the same paragraph into one block.
If a word is illegible write [؟] in its place and lower the confidence."""


def _tesseract(png: bytes) -> tuple[str, float]:
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        data = pytesseract.image_to_data(img, lang="ara+eng", output_type=pytesseract.Output.DICT)
        words = [(w, float(c)) for w, c in zip(data["text"], data["conf"]) if w.strip() and float(c) >= 0]
        if not words:
            return "", 0.0
        conf = sum(c for _, c in words) / len(words) / 100
        text = pytesseract.image_to_string(img, lang="ara+eng")
        return text, conf
    except Exception as e:  # tesseract missing or failed: not fatal
        log.debug("tesseract unavailable: %s", e)
        return "", -1.0


def _overlap(a: str, b: str) -> float:
    wa = set(re.findall(r"\w{3,}", a.lower()))
    wb = set(re.findall(r"\w{3,}", b.lower()))
    if not wa or not wb:
        return -1.0
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


_CHAPTER = re.compile(r"^\s*(chapter|part|prologue|epilogue|introduction|preface|الفصل|الباب|الجزء|المقدمة|مقدمة|الخاتمة)\b", re.I)


def _paragraphs_from_plain(text: str) -> list[dict]:
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return [{"kind": "h1" if _CHAPTER.match(p) and len(p) < 90 else "p", "text": p} for p in paras if p]


def ocr_image(png: bytes, page_no: int) -> list[dict]:
    tess_text, tess_conf = _tesseract(png)

    def mock():
        return {"blocks": _paragraphs_from_plain(tess_text) or
                [{"kind": "p", "text": f"[نص مستخرج تجريبي من الصفحة {page_no}]"}],
                "confidence": tess_conf if tess_conf >= 0 else 0.5, "handwritten": False, "notes": "mock"}

    result = llm.call_json(agent="ocr", model=config.MODEL_OCR, instructions=OCR_INSTRUCTIONS,
                           user=f"Page {page_no}. Transcribe it.", images=[png], mock=mock)
    blocks = [b for b in result.get("blocks", []) if str(b.get("text", "")).strip()]
    full = "\n".join(b["text"] for b in blocks)

    model_conf = float(result.get("confidence", 0.7))
    quality = text_quality(full) if full else 0.0
    agreement = _overlap(full, tess_text) if not config.MOCK_LLM else -1.0
    score = 0.5 * model_conf + 0.3 * quality + 0.2 * (agreement if agreement >= 0 else model_conf)
    if result.get("handwritten"):
        score = min(score, 0.7)        # handwriting always goes to human review
    if full.count("[؟]") > 3:
        score = min(score, 0.6)
    log.info("page %s ocr score %.2f (model %.2f, quality %.2f, agree %.2f)",
             page_no, score, model_conf, quality, agreement)

    out = []
    for b in blocks:
        kind = b.get("kind", "p")
        out.append({"kind": kind if kind in ("h1", "h2", "h3", "p", "quote", "list", "code") else "p",
                    "text": b["text"], "page": page_no, "conf": round(score, 2),
                    "handwritten": bool(result.get("handwritten"))})
    return out
