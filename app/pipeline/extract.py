"""Extraction routes. Every route returns the same unified structure:

    [{"kind": "h1"|"h2"|"h3"|"p"|"quote"|"code"|"list", "text": str,
      "page": int, "conf": float}]

"h1" starts a new chapter. `conf` is the extraction confidence (1.0 for born
digital text, the OCR validation score for scanned pages). Downstream
translation never needs to know which route produced the blocks.
"""
import io
import logging
import re
import zipfile
from collections import Counter
from pathlib import Path

from .. import config
from .ocr import ocr_image
from .quality import text_quality

log = logging.getLogger("extract")

SENTENCE_END = re.compile(r"[.!?؟。:”\"')\]»…]\s*$")
PAGE_NUMBER = re.compile(r"^\s*[-–—]?\s*(\d{1,4}|[ivxlcdm]{1,6}|[٠-٩]{1,4})\s*[-–—]?\s*$", re.I)
CHAPTER_LINE = re.compile(
    r"^\s*(chapter|part|book|prologue|epilogue|introduction|preface|الفصل|الباب|الجزء|المقدمة|مقدمة|الخاتمة|تمهيد)\b",
    re.I)


class ProtectedFileError(Exception):
    pass


# ---------------------------------------------------------------- PDF (text)

def analyze_pdf(path: Path) -> list[dict]:
    """Per page: does it have a usable text layer? Drives text / scanned / mixed routing."""
    import pymupdf
    doc = pymupdf.open(path)
    if doc.needs_pass:
        raise ProtectedFileError("الملف محمي بكلمة مرور")
    pages = []
    for page in doc:
        text = page.get_text("text")
        has_images = bool(page.get_images(full=False))
        q = text_quality(text) if text.strip() else 0.0
        needs_ocr = len(text.strip()) < 40 and has_images or (len(text.strip()) >= 40 and q < 0.55)
        if len(text.strip()) < 40 and not has_images:
            needs_ocr = False          # genuinely blank / decorative page
        pages.append({"chars": len(text.strip()), "quality": round(q, 2),
                      "images": has_images, "needs_ocr": needs_ocr})
    return pages


def _pdf_page_blocks(page, body_size: float, page_no: int) -> list[dict]:
    data = page.get_text("dict")
    out = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        lines, sizes, bold_chars, chars, boxes = [], [], 0, 0, []
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            lines.append("".join(s["text"] for s in line["spans"]).strip())
            boxes.append(line["bbox"])
            for s in spans:
                n = len(s["text"].strip())
                sizes.append((s["size"], n))
                chars += n
                if s["flags"] & 16:
                    bold_chars += n
        if not lines:
            continue
        text = ""
        for ln in lines:                              # join lines, repair hyphenation
            if text.endswith("-") and ln[:1].islower():
                text = text[:-1] + ln
            else:
                text = (text + " " + ln).strip()
        size = max(sz for sz, _ in sizes)
        kind = "p"
        if len(text) < 150:
            if size >= body_size * 1.45 or (CHAPTER_LINE.match(text) and size >= body_size * 1.1):
                kind = "h1"
            elif size >= body_size * 1.15 or (bold_chars == chars and len(text) < 90 and not SENTENCE_END.search(text)):
                kind = "h2"
        out.append({"kind": kind, "text": text, "page": page_no, "conf": 1.0, "size": size,
                    "first": (boxes[0][0], boxes[0][2]), "last": (boxes[-1][0], boxes[-1][2]),
                    "x0": block["bbox"][0], "x1": block["bbox"][2],
                    "y0": block["bbox"][1], "y1": block["bbox"][3], "h": page.rect.height})
    return _merge_lines(out)


_RTL = re.compile(r"[\u0590-\u08FF]")


def _merge_lines(blocks: list[dict]) -> list[dict]:
    """Many PDFs store every line (or every few lines) as its own block. Glue a
    block to the previous one only when the paragraph clearly continues:
    same font size, a normal line gap, the previous block's last line runs to
    the full column width, and the next block's first line is not indented."""
    body = [b for b in blocks if b["kind"] == "p"]
    if not body:
        return blocks
    left = min(b["x0"] for b in body)
    right = max(b["x1"] for b in body)
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev["kind"] == "p" and b["kind"] == "p" and abs(prev["size"] - b["size"]) < 0.6:
            size = b["size"]
            gap = b["y0"] - prev["y1"]
            rtl = bool(_RTL.search(prev["text"][:80]))
            px0, px1 = prev["last"]
            bx0, bx1 = b["first"]
            if rtl:
                prev_full = px0 <= left + 2 * size
                indented = bx1 < right - 1.2 * size
            else:
                prev_full = px1 >= right - 2 * size
                indented = bx0 > left + 1.2 * size
            if -2 <= gap < 0.9 * size and prev_full and not indented:
                if prev["text"].endswith("-") and b["text"][:1].islower():
                    prev["text"] = prev["text"][:-1] + b["text"]
                else:
                    prev["text"] = prev["text"] + " " + b["text"]
                prev["last"] = b["last"]
                prev["y1"] = b["y1"]
                continue
        out.append(dict(b))
    return out


def _body_font_size(doc) -> float:
    counter = Counter()
    for i, page in enumerate(doc):
        if i > 40:
            break
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for s in line["spans"]:
                    counter[round(s["size"])] += len(s["text"].strip())
    return float(counter.most_common(1)[0][0]) if counter else 11.0


def _strip_running_headers(pages: list[list[dict]]) -> list[list[dict]]:
    """Drop page numbers and lines that repeat at the top/bottom of many pages.

    Text-layer blocks know their position; OCR blocks do not, so for those the
    first and last block of the page count as the page edge."""
    def is_edge(b, i, n):
        if "h" in b:
            return b["y0"] < b["h"] * 0.08 or b["y1"] > b["h"] * 0.92
        return (i == 0 or i == n - 1) and len(b["text"]) < 90

    edge = Counter()
    for blocks in pages:
        for i, b in enumerate(blocks):
            if is_edge(b, i, len(blocks)):
                edge[re.sub(r"\d+", "#", b["text"])] += 1
    repeated = {t for t, c in edge.items() if c >= max(3, len(pages) * 0.3)}
    cleaned = []
    for blocks in pages:
        keep = []
        for i, b in enumerate(blocks):
            at_edge = is_edge(b, i, len(blocks))
            if at_edge and (PAGE_NUMBER.match(b["text"]) or re.sub(r"\d+", "#", b["text"]) in repeated):
                continue
            keep.append(b)
        cleaned.append(keep)
    return cleaned


def _merge_across_pages(blocks: list[dict]) -> list[dict]:
    """A paragraph cut by a page break is glued back together."""
    out = []
    for b in blocks:
        prev = out[-1] if out else None
        if (prev and prev["kind"] == "p" and b["kind"] == "p" and b["page"] == prev["page"] + 1
                and not SENTENCE_END.search(prev["text"]) and b.get("first_on_page")):
            prev["text"] = (prev["text"][:-1] if prev["text"].endswith("-") else prev["text"] + " ") + b["text"]
            prev["conf"] = min(prev["conf"], b["conf"])
            continue
        out.append(b)
    return out


def extract_pdf(path: Path, page_info: list[dict], progress=None) -> list[dict]:
    """Handles text, scanned and mixed PDFs: the decision is made per page."""
    import pymupdf
    doc = pymupdf.open(path)
    body = _body_font_size(doc)
    per_page = []
    total = len(doc)
    for i, page in enumerate(doc):
        if page_info[i]["needs_ocr"]:
            pix = page.get_pixmap(dpi=200)
            blocks = ocr_image(pix.tobytes("png"), page_no=i + 1)
        else:
            blocks = _pdf_page_blocks(page, body, i + 1)
        if blocks:
            blocks[0]["first_on_page"] = True
        per_page.append(blocks)
        if progress:
            progress(i + 1, total)
    per_page = _strip_running_headers(per_page)
    flat = [b for blocks in per_page for b in blocks]
    return _clean(_merge_across_pages(flat))


# ---------------------------------------------------------------- images

def _natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def extract_images(path: Path, progress=None) -> list[dict]:
    """A single image or a ZIP of page photos, ordered by file name."""
    from PIL import Image, ImageOps
    images: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if re.search(r"\.(png|jpe?g|webp|heic|tiff?|bmp)$", name, re.I) and "__MACOSX" not in name:
                    images.append((name, z.read(name)))
    else:
        images.append((path.name, path.read_bytes()))
    images.sort(key=lambda x: _natural_key(x[0]))
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()          # iPhone photos
    except ImportError:
        pass
    per_page = []
    for i, (name, raw) in enumerate(images):
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        img.thumbnail((2400, 2400))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        blocks = ocr_image(buf.getvalue(), page_no=i + 1)
        if blocks:
            blocks[0]["first_on_page"] = True
        per_page.append(blocks)
        if progress:
            progress(i + 1, len(images))
    per_page = _strip_running_headers(per_page)
    return _clean(_merge_across_pages([b for blocks in per_page for b in blocks]))


# ---------------------------------------------------------------- EPUB

def extract_epub(path: Path) -> list[dict]:
    from bs4 import BeautifulSoup
    with zipfile.ZipFile(path) as z:
        if "META-INF/encryption.xml" in z.namelist():
            enc = z.read("META-INF/encryption.xml").decode("utf-8", "ignore")
            if "obfuscation" not in enc.lower() or enc.lower().count("<enc:encrypteddata") > 3:
                raise ProtectedFileError("الكتاب محمي بنظام DRM ولا يمكن قراءته")
    import ebooklib
    from ebooklib import epub
    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    out = []
    for idx, (item_id, _) in enumerate(book.spine):
        item = book.get_item_with_id(item_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        first = True
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "blockquote", "pre", "li"]):
            if el.find_parent(["blockquote", "li", "pre"]) and el.name == "p":
                continue
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            kind = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3", "blockquote": "quote",
                    "pre": "code", "li": "list"}.get(el.name, "p")
            if first and kind in ("h2", "h3") and len(text) < 150:
                kind = "h1"                                  # each spine file usually = a chapter
            first = False
            out.append({"kind": kind, "text": text, "page": idx + 1, "conf": 1.0})
    return _clean(out)


# ---------------------------------------------------------------- DOCX / TXT / MD

def extract_docx(path: Path) -> list[dict]:
    import docx
    d = docx.Document(str(path))
    out = []
    for para in d.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if style in ("title", "heading 1") or style.startswith("heading 1"):
            kind = "h1"
        elif style.startswith("heading 2"):
            kind = "h2"
        elif style.startswith("heading"):
            kind = "h3"
        elif "quote" in style:
            kind = "quote"
        elif "list" in style:
            kind = "list"
        else:
            kind = "p"
        out.append({"kind": kind, "text": text, "page": 0, "conf": 1.0})
    return _clean(out)


def extract_text(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    out, buf, in_code = [], [], False

    def flush():
        if buf:
            joined = " ".join(s.strip() for s in buf).strip()
            if joined:
                out.append({"kind": "p", "text": joined, "page": 0, "conf": 1.0})
            buf.clear()

    for line in text.splitlines():
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            if in_code:
                out.append({"kind": "code", "text": "", "page": 0, "conf": 1.0})
            continue
        if in_code:
            out[-1]["text"] += line + "\n"
            continue
        m = re.match(r"^(#{1,3})\s+(.+)", line)
        if m:
            flush()
            out.append({"kind": f"h{len(m.group(1))}", "text": m.group(2).strip(), "page": 0, "conf": 1.0})
        elif CHAPTER_LINE.match(line) and len(line.strip()) < 80:
            flush()
            out.append({"kind": "h1", "text": line.strip(), "page": 0, "conf": 1.0})
        elif not line.strip():
            flush()
        elif line.lstrip().startswith(("> ",)):
            flush()
            out.append({"kind": "quote", "text": line.lstrip()[2:].strip(), "page": 0, "conf": 1.0})
        else:
            buf.append(line)
    flush()
    return _clean(out)


# ---------------------------------------------------------------- shared

def _clean(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        text = b["text"] if b["kind"] == "code" else re.sub(r"\s+", " ", b["text"]).strip()
        if not text.strip():
            continue
        out.append({"kind": b["kind"], "text": text, "page": b.get("page", 0),
                    "conf": round(float(b.get("conf", 1.0)), 2)})
    return out
