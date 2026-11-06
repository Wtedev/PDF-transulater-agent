"""Router agent: decides the two routes for an uploaded book.

1. Format route (how to extract) — decided by code: real file type from its
   bytes, then a per-page scan for PDFs.
2. Content route (how to translate) — decided by an LLM on a text sample.
"""
import logging
import zipfile
from pathlib import Path

from .. import config, llm
from .profiles import PROFILES
from .quality import detect_language

log = logging.getLogger("router")

FORMAT_LABELS = {
    "text_pdf": "PDF نصي",
    "scanned_pdf": "PDF مصوّر",
    "mixed_pdf": "PDF مختلط",
    "images": "صور صفحات",
    "epub": "EPUB",
    "docx": "Word",
    "text": "نص / Markdown",
}


def detect_format(path: Path) -> str:
    head = path.read_bytes()[:4096]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "mimetype" in names and z.read("mimetype").strip() == b"application/epub+zip":
                return "epub"
            if "word/document.xml" in names:
                return "docx"
            if any(n.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".heic")) for n in names):
                return "images"
        raise ValueError("ملف مضغوط غير مدعوم: يجب أن يحتوي على صور صفحات")
    if head[:3] == b"\xff\xd8\xff" or head.startswith(b"\x89PNG") or head[:4] in (b"RIFF", b"II*\x00", b"MM\x00*") \
            or head[4:12] in (b"ftypheic", b"ftypmif1"):
        return "images"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError("صيغة Word القديمة (.doc) غير مدعومة، احفظ الملف كـ .docx")
    try:
        head.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        if head.count(b"\x00") == 0:
            return "text"
    raise ValueError("صيغة الملف غير معروفة")


def pdf_route(page_info: list[dict]) -> str:
    content_pages = [p for p in page_info if p["chars"] or p["images"]]
    if not content_pages:
        return "scanned_pdf"
    ocr = sum(p["needs_ocr"] for p in content_pages)
    if ocr == 0:
        return "text_pdf"
    if ocr >= len(content_pages) * 0.9:
        return "scanned_pdf"
    return "mixed_pdf"


CLASSIFY_INSTRUCTIONS = """You are the routing agent of a book translation system.
From the text sample, identify the book. Return JSON only:
{"content_type": one of %s,
 "source_language": ISO 639-1 code,
 "layout_notes": "tables, equations, code, footnotes, poetry... if present",
 "title_guess": "book title if visible, else empty",
 "author_guess": "author if visible, else empty",
 "confidence": 0.0-1.0}
Types: literary=novels/stories; scientific=academic/science/medicine; technical=programming/IT manuals;
children=children books & comics; religious=religious texts; poetry=poetry collections;
legal=law/finance/contracts; self_help=self development/business/popular non-fiction; general=anything else."""


def classify_content(blocks: list[dict]) -> dict:
    # sample from beginning, middle and end so a long preface does not mislead
    texts = [b["text"] for b in blocks if b["kind"] != "code"]
    n = len(texts)
    picks = texts[:25] + texts[n // 2: n // 2 + 15] + texts[-10:] if n > 50 else texts
    sample = "\n".join(picks)[:12000]
    lang = detect_language(sample)

    def mock():
        return {"content_type": "general", "source_language": lang, "layout_notes": "",
                "title_guess": "", "author_guess": "", "confidence": 0.6}

    result = llm.call_json(agent="router", model=config.MODEL_ROUTER,
                           instructions=CLASSIFY_INSTRUCTIONS % list(PROFILES),
                           user=f"Text sample:\n\n{sample}", mock=mock, max_tokens=800)
    if result.get("content_type") not in PROFILES:
        result["content_type"] = "general"
    result.setdefault("source_language", lang)
    return result
