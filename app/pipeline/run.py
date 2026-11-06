"""Orchestrator: runs one book through every stage.

Stages are resumable: extracted paragraphs, the glossary and every finished
segment are saved, so a restart (deploy, crash, "retry" button) continues
where it stopped instead of paying for the same work twice.
"""
import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import func

from .. import config
from ..db import Book, Chapter, Paragraph, SessionLocal
from ..export.pdf import export_pdf
from ..providers import KeyChain, NoCreditLeft, current_chain
from . import agents, extract, router

log = logging.getLogger("pipeline")


def _update(book_id: int, **fields):
    with SessionLocal() as s:
        s.query(Book).filter(Book.id == book_id).update(fields)
        s.commit()


# ---------------------------------------------------------------- stage 1: route + extract

def _extract(book: Book) -> list[dict]:
    path = config.UPLOAD_DIR / book.stored_file
    kind = router.detect_format(path)
    info: dict = {"detected": kind}

    def prog(done, total):
        _update(book.id, progress=round(2 + 13 * done / max(total, 1), 1),
                stage=f"استخراج النص: صفحة {done} من {total}")

    if kind == "pdf":
        pages = extract.analyze_pdf(path)
        fmt = router.pdf_route(pages)
        info.update(pages=len(pages), ocr_pages=sum(p["needs_ocr"] for p in pages))
        _update(book.id, file_format=fmt, router_info=info,
                stage=f"المسار: {router.FORMAT_LABELS[fmt]}")
        blocks = extract.extract_pdf(path, pages, progress=prog)
    else:
        fmt = kind
        _update(book.id, file_format=fmt, router_info=info, stage=f"المسار: {router.FORMAT_LABELS[fmt]}")
        blocks = {"images": lambda: extract.extract_images(path, progress=prog),
                  "epub": lambda: extract.extract_epub(path),
                  "docx": lambda: extract.extract_docx(path),
                  "text": lambda: extract.extract_text(path)}[fmt]()
    if not blocks:
        raise ValueError("لم يتم العثور على نص قابل للترجمة في الملف")
    info["handwritten"] = any(b.get("handwritten") for b in blocks)
    info["low_confidence_blocks"] = sum(b["conf"] < config.OCR_MIN_CONFIDENCE for b in blocks)
    _update(book.id, router_info=info)
    return blocks


def _chapters_from_blocks(blocks: list[dict]) -> list[dict]:
    chapters, current = [], {"title": "", "blocks": []}
    for b in blocks:
        if b["kind"] == "h1":
            if current["blocks"]:
                chapters.append(current)
            current = {"title": b["text"], "blocks": [b]}
        else:
            current["blocks"].append(b)
    if current["blocks"]:
        chapters.append(current)
    # a book without chapter headings is split into sections of ~4000 words
    if len(chapters) == 1 and sum(len(b["text"].split()) for b in blocks) > 6000:
        chapters, words = [], 0
        current = {"title": "", "blocks": []}
        for b in blocks:
            current["blocks"].append(b)
            words += len(b["text"].split())
            if words > 4000 and b["kind"] == "p":
                chapters.append(current)
                current, words = {"title": "", "blocks": []}, 0
        if current["blocks"]:
            chapters.append(current)
    return chapters


def _store(book_id: int, blocks: list[dict]):
    with SessionLocal() as s:
        for ci, ch in enumerate(_chapters_from_blocks(blocks)):
            chapter = Chapter(book_id=book_id, index=ci, title_src=ch["title"])
            s.add(chapter)
            s.flush()
            seg, words = 0, 0
            for pi, b in enumerate(ch["blocks"]):
                n = len(b["text"].split())
                if words and words + n > config.SEGMENT_WORDS:
                    seg, words = seg + 1, 0
                words += n
                s.add(Paragraph(chapter_id=chapter.id, index=pi, segment=seg, kind=b["kind"],
                                src=b["text"], page=b["page"],
                                flagged=b["conf"] < config.OCR_MIN_CONFIDENCE or bool(b.get("handwritten"))))
        s.commit()


# ---------------------------------------------------------------- stage 3: translate a chapter

def _with_retries(fn, what: str, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except NoCreditLeft:
            raise
        except Exception as e:
            if i == attempts - 1:
                raise
            log.warning("%s failed (%s), retrying", what, e)
            time.sleep(4 * (i + 1))


def _translate_chapter(book_id: int, chapter_id: int, ctx: str, profile: str, totals: dict):
    with SessionLocal() as s:
        paras = s.query(Paragraph).filter(Paragraph.chapter_id == chapter_id).order_by(Paragraph.index).all()
        segments: dict[int, list[Paragraph]] = {}
        for p in paras:
            segments.setdefault(p.segment, []).append(p)
        previous: list[str] = []
        for seg_no in sorted(segments):
            seg = segments[seg_no]
            if all(p.tr for p in seg):                      # finished in an earlier run
                previous = [p.tr for p in seg][-2:]
                continue
            items = [{"id": p.id, "kind": p.kind, "text": p.src} for p in seg]

            draft = _with_retries(lambda: agents.translate(items, ctx, previous), "translate")
            verdict = _with_retries(lambda: agents.review(items, draft, ctx, profile), "review")
            tries = 0
            while verdict["score"] < config.REVIEW_PASS_SCORE and tries < config.MAX_RETRANSLATIONS:
                tries += 1
                draft_list = [{"id": i, "text": t} for i, t in draft.items()]
                draft = _with_retries(lambda: agents.translate(items, ctx, previous, verdict["issues"], draft_list),
                                      "retranslate")
                verdict = _with_retries(lambda: agents.review(items, draft, ctx, profile), "review")
            remaining = [i for i in verdict["issues"] if i.get("severity") in ("critical", "major")] \
                if verdict["score"] >= config.REVIEW_PASS_SCORE else verdict["issues"]
            final = _with_retries(lambda: agents.edit(items, draft, remaining, ctx, profile, previous), "edit")

            low = verdict["score"] < config.REVIEW_PASS_SCORE
            for p in seg:
                p.tr = final[p.id]
                p.score = verdict["score"]
                p.flagged = p.flagged or low
            s.commit()
            previous = [final[p.id] for p in seg][-2:]

            totals["done"] += sum(len(p.src) for p in seg)
            pct = 20 + 70 * totals["done"] / max(totals["all"], 1)
            _update(book_id, progress=round(pct, 1), stage=f"الترجمة والتدقيق: {round(pct)}%")

        chapter = s.get(Chapter, chapter_id)
        if chapter.status != "done":
            rows = [{"id": p.id, "kind": p.kind, "tr": p.tr} for p in paras]
            changes = _with_retries(lambda: agents.consistency(rows, ctx), "consistency")
            for p in paras:
                if p.id in changes:
                    p.tr = changes[p.id]
            if paras and paras[0].kind == "h1":
                chapter.title_tr = paras[0].tr
            chapter.status = "done"
            s.commit()


# ---------------------------------------------------------------- main entry

def process_book(book_id: int):
    with SessionLocal() as s:
        book = s.get(Book, book_id)
        if not book:
            return
        has_paragraphs = s.query(func.count(Paragraph.id)).join(Chapter).filter(Chapter.book_id == book_id).scalar()

    chain = KeyChain.for_user(book.user_id)
    token = current_chain.set(chain)
    try:
        _update(book_id, status="analyzing", error="", stage="تحليل الملف وتحديد المسار", progress=1)
        if not has_paragraphs:
            blocks = _extract(book)
            _update(book_id, stage="تحديد نوع المحتوى", progress=16)
            cls = router.classify_content(blocks)
            with SessionLocal() as s:
                b = s.get(Book, book_id)
                b.router_info = {**(b.router_info or {}), "classification": cls}
                if not b.category or b.category == "auto":
                    b.category = cls["content_type"]
                if b.source_lang in ("", "auto"):
                    b.source_lang = cls.get("source_language", "en")
                if not b.author and cls.get("author_guess"):
                    b.author = cls["author_guess"]
                s.commit()
            _store(book_id, blocks)
        else:
            blocks = None

        with SessionLocal() as s:
            book = s.get(Book, book_id)
            if not book.glossary:
                _update(book_id, status="translating", stage="بناء قاموس المصطلحات ودليل الأسلوب", progress=17)
                if blocks is None:
                    blocks = [{"kind": p.kind, "text": p.src}
                              for c in book.chapters for p in c.paragraphs]
                book.glossary = agents.build_glossary(blocks, book.category, book.source_lang, book.title)
                s.commit()
            ctx = agents.context_for(book.glossary, book.category, book.source_lang)
            chapter_ids = [c.id for c in book.chapters]
            all_chars = sum(len(p.src) for c in book.chapters for p in c.paragraphs)
            done_chars = sum(len(p.src) for c in book.chapters for p in c.paragraphs if p.tr)
            profile = book.category

        _update(book_id, status="translating", stage="الترجمة والتدقيق", progress=20)
        totals = {"all": all_chars, "done": done_chars}
        with ThreadPoolExecutor(max_workers=config.CHAPTER_WORKERS) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _translate_chapter, book_id, cid, ctx, profile, totals)
                       for cid in chapter_ids]
            for f in futures:
                f.result()

        _update(book_id, status="exporting", stage="إخراج ملف PDF بالقالب الموحد", progress=92)
        with SessionLocal() as s:
            book = s.get(Book, book_id)
            pdf_name = export_pdf(book)
            flagged = s.query(func.count(Paragraph.id)).join(Chapter).filter(
                Chapter.book_id == book_id, Paragraph.flagged.is_(True)).scalar()
            book.pdf_file = pdf_name
            book.flagged_count = flagged
            book.status = "needs_review" if flagged else "ready"
            book.stage = f"جاهز — {flagged} فقرة تحتاج مراجعة" if flagged else "جاهز"
            book.progress = 100
            s.commit()
    except NoCreditLeft:
        _update(book_id, status="no_credit", error="",
                stage="متوقف: انتهى رصيد المفاتيح المتاحة — أضف مفتاحك من صفحة المفاتيح وسيكمل من حيث توقف")
    except extract.ProtectedFileError as e:
        _update(book_id, status="failed", error=str(e), stage="فشل")
    except Exception as e:
        log.exception("book %s failed", book_id)
        _update(book_id, status="failed", error=f"{type(e).__name__}: {e}"[:2000], stage="فشل — يمكن إعادة المحاولة")
    finally:
        if chain.used:
            with SessionLocal() as s:
                b = s.get(Book, book_id)
                if b:
                    prev = [x for x in (b.key_used or "").split(" ← ") if x]
                    b.key_used = " ← ".join(dict.fromkeys(prev + chain.used))[:200]
                    s.commit()
        current_chain.reset(token)
