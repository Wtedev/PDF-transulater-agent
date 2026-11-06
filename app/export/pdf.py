"""Unified PDF template (WeasyPrint): cover, title page, contents, chapters.

Every book goes through the same template so all exports share one look.
The file is named after the title the user typed at upload.
"""
import html
import re
from pathlib import Path

from .. import config
from ..pipeline.profiles import PROFILES
from .covers import cover_html, cover_spec, display_title

TEMPLATE_CSS = (Path(__file__).parent / "book.css").read_text(encoding="utf-8")
COVER_CSS = (config.BASE_DIR / "static" / "covers.css").read_text(encoding="utf-8")


def safe_filename(title: str, author: str = "") -> str:
    name = f"{title} - {author}" if author else title
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name).strip()[:150] or "book"
    return f"{name}.pdf"


def _esc(t: str) -> str:
    return html.escape(t or "")


def _font_faces() -> str:
    faces = []
    for family, file, weight in (("Naskh", "NotoNaskhArabic.ttf", "400"), ("Naskh", "NotoNaskhArabic.ttf", "700"),
                                 ("Amiri", "Amiri-Regular.ttf", "400"),
                                 ("Amiri", "Amiri-Bold.ttf", "700"),
                                 ("Aref Ruqaa", "ArefRuqaa-Bold.ttf", "700"),
                                 ("Reem Kufi", "ReemKufi.ttf", "600"),
                                 ("IBM Plex Sans Arabic", "IBMPlexSansArabic-Regular.ttf", "400"),
                                 ("IBM Plex Sans Arabic", "IBMPlexSansArabic-Bold.ttf", "600"),
                                 ("IBM Plex Sans Arabic", "IBMPlexSansArabic-Bold.ttf", "700")):
        path = (config.FONT_DIR / file).resolve()
        faces.append(f"@font-face{{font-family:'{family}';src:url('file://{path}');font-weight:{weight};}}")
    return "\n".join(faces)


def build_html(book) -> str:
    accent = cover_spec(book)["accent"]
    title, author = _esc(display_title(book)), _esc(book.author)
    original = _esc(book.title) if display_title(book) != book.title else ""
    category = PROFILES.get(book.category, PROFILES["general"])["label"]

    if book.cover_file:
        cover_path = (config.COVER_DIR / book.cover_file).resolve()
        cover = f'<section class="cover image-cover"><img src="file://{cover_path}"></section>'
    else:
        cover = f'<section class="cover">{cover_html(cover_spec(book))}</section>'

    toc, body = [], []
    for ch in book.chapters:
        anchor = f"ch{ch.index}"
        paras = list(ch.paragraphs)
        heading = ch.title_tr or (paras[0].tr if paras and paras[0].kind == "h1" else "") or f"القسم {ch.index + 1}"
        toc.append(f'<li><a href="#{anchor}">{_esc(heading)}</a></li>')
        parts = [f'<section class="chapter" id="{anchor}"><h1 class="chapter-title">{_esc(heading)}</h1>']
        for p in paras:
            if p.kind == "h1":
                continue
            text = _esc(p.tr or p.src)
            if p.kind == "code":
                parts.append(f'<pre dir="ltr">{_esc(p.src)}</pre>')
            elif p.kind in ("h2", "h3"):
                parts.append(f"<{p.kind}>{text}</{p.kind}>")
            elif p.kind == "quote":
                parts.append(f"<blockquote>{text}</blockquote>")
            elif p.kind == "list":
                parts.append(f'<p class="li">{text}</p>')
            else:
                parts.append(f"<p>{text}</p>")
        parts.append("</section>")
        body.append("\n".join(parts))

    title_page = f"""<section class="title-page">
        <h1>{title}</h1>{f'<div class="tp-original" dir="auto">{original}</div>' if original else ""}<div class="tp-author">{author}</div>
        <div class="tp-note">ترجمة آلية مراجعة بوكلاء الذكاء الاصطناعي<br>ترجمة · تدقيق · تحرير · مراجعة اتساق</div>
      </section>"""
    contents = f'<section class="toc"><h1>المحتويات</h1><ol>{"".join(toc)}</ol></section>' if len(toc) > 1 else ""

    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
      <title>{title}</title><style>{_font_faces()}\n{TEMPLATE_CSS}\n{COVER_CSS}\n.cover .bc{{--u:1.48mm;width:148mm;height:210mm;aspect-ratio:auto;}}
      .chapter-title{{border-bottom-color:{accent}}} blockquote{{border-right-color:{accent}}}</style>
      </head><body><div class="running-title">{title}</div>{cover}{title_page}{contents}{''.join(body)}</body></html>"""


def ensure_title_ar(book):
    """Books translated before titles were translated get their Arabic title now."""
    import re as _re
    if _re.search(r"[\u0600-\u06FF]", book.title or "") or (book.glossary or {}).get("title_ar"):
        return
    try:
        from ..pipeline.agents import translate_title
        ar = translate_title(book.title)
        if ar:
            book.glossary = {**(book.glossary or {}), "title_ar": ar}
    except Exception:
        pass   # keep the original title rather than failing the export


def export_pdf(book) -> str:
    from weasyprint import HTML
    ensure_title_ar(book)
    out_dir = config.OUTPUT_DIR / str(book.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = safe_filename(display_title(book), book.author)
    for old in out_dir.glob("*.pdf"):
        old.unlink()
    HTML(string=build_html(book), base_url=str(config.BASE_DIR)).write_pdf(out_dir / name)
    return f"{book.id}/{name}"
