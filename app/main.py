"""Web app: upload, library, reader API and static pages."""
import base64
import logging
import secrets
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

from . import config, worker
from .db import Book, Bookmark, Chapter, Paragraph, ReadingState, SessionLocal, init_db
from .export.covers import STYLES, cover_html, cover_spec, get_cover_style, set_cover_style
from .pipeline.profiles import PROFILES
from .pipeline.router import FORMAT_LABELS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = FastAPI(title="مترجم الكتب")
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_MB = 300


@app.on_event("startup")
def startup():
    init_db()
    worker.start()
    logging.getLogger("app").info("started (mock LLM: %s)", config.MOCK_LLM)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.APP_PASSWORD and request.url.path != "/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, pw = base64.b64decode(header[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, config.APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="books"'})
    return await call_next(request)


def _book_json(b: Book, extra: dict | None = None) -> dict:
    spec = cover_spec(b)
    percent = (extra or {}).get("read_percent")
    d = {"id": b.id, "title": b.title, "author": b.author, "category": b.category,
         "category_label": PROFILES.get(b.category, {}).get("label", ""),
         "format": b.file_format, "format_label": FORMAT_LABELS.get(b.file_format, ""),
         "source_lang": b.source_lang, "status": b.status, "stage": b.stage,
         "progress": b.progress, "error": b.error, "flagged": b.flagged_count,
         "has_pdf": bool(b.pdf_file), "has_cover": bool(b.cover_file),
         "colors": {"bg": spec["ground"], "accent": spec["accent"], "text": spec["ink"]},
         "cover_html": cover_html(spec, percent),
         "created_at": b.created_at.isoformat() if b.created_at else None,
         "updated_at": b.updated_at.isoformat() if b.updated_at else None}
    if extra:
        d.update(extra)
    return d


@app.get("/health")
def health():
    return {"ok": True, "mock": config.MOCK_LLM}


@app.get("/api/meta")
def meta():
    return {"categories": [{"key": k, "label": v["label"]} for k, v in PROFILES.items()],
            "mock": config.MOCK_LLM, "cover_style": get_cover_style(),
            "cover_styles": [{"key": k, "label": v} for k, v in STYLES.items()]}


@app.put("/api/settings")
async def put_settings(request: Request):
    data = await request.json()
    style = data.get("cover_style")
    if style not in STYLES:
        raise HTTPException(400, "نمط غير معروف")
    set_cover_style(style)
    return {"cover_style": style}


@app.get("/api/books")
def list_books():
    with SessionLocal() as s:
        states = {r.book_id: r for r in s.query(ReadingState).all()}
        books = s.query(Book).order_by(Book.created_at.desc()).all()
        out = []
        for b in books:
            st = states.get(b.id)
            out.append(_book_json(b, {"read_percent": st.percent if st else 0,
                                      "last_read": st.updated_at.isoformat() if st and st.updated_at else None}))
        return out


@app.post("/api/books")
async def upload_book(file: UploadFile = File(...), title: str = Form(...), author: str = Form(""),
                      source_lang: str = Form("auto"), category: str = Form("auto"),
                      rights: str = Form(""), cover: UploadFile | None = File(None)):
    title = title.strip()
    if not title:
        raise HTTPException(400, "اسم الكتاب مطلوب")
    if rights not in ("on", "true", "1"):
        raise HTTPException(400, "يجب تأكيد امتلاك حق ترجمة الكتاب أو أنه ملكية عامة")
    if category != "auto" and category not in PROFILES:
        raise HTTPException(400, "تصنيف غير معروف")

    ext = Path(file.filename or "").suffix.lower()[:10]
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = config.UPLOAD_DIR / stored
    size = 0
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"الحد الأقصى لحجم الملف {MAX_UPLOAD_MB} ميغابايت")
            f.write(chunk)

    cover_name = ""
    if cover is not None and cover.filename:
        data = await cover.read()
        if data:
            cover_name = f"{uuid.uuid4().hex}{Path(cover.filename).suffix.lower()[:6] or '.jpg'}"
            (config.COVER_DIR / cover_name).write_bytes(data)

    with SessionLocal() as s:
        b = Book(title=title, author=author.strip(), source_lang=source_lang or "auto",
                 category="" if category == "auto" else category, original_filename=file.filename or "",
                 stored_file=stored, cover_file=cover_name, status="queued", stage="في الانتظار")
        s.add(b)
        s.commit()
        worker.wake()
        return _book_json(b)


def _get_book(s, book_id: int) -> Book:
    b = s.get(Book, book_id)
    if not b:
        raise HTTPException(404, "الكتاب غير موجود")
    return b


@app.get("/api/books/{book_id}")
def get_book(book_id: int):
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        return _book_json(b, {"router_info": b.router_info, "glossary_terms": len((b.glossary or {}).get("terms", []))})


@app.patch("/api/books/{book_id}")
async def rename_book(book_id: int, request: Request):
    data = await request.json()
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        if data.get("title", "").strip():
            b.title = data["title"].strip()
        if "author" in data:
            b.author = data["author"].strip()
        s.commit()
        return _book_json(b)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int):
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        if b.status in worker.IN_PROGRESS:
            raise HTTPException(409, "لا يمكن حذف كتاب قيد المعالجة")
        for p in (config.UPLOAD_DIR / b.stored_file, config.COVER_DIR / b.cover_file if b.cover_file else None):
            if p and p.is_file():
                p.unlink()
        out = config.OUTPUT_DIR / str(b.id)
        if out.is_dir():
            for f in out.iterdir():
                f.unlink()
            out.rmdir()
        s.query(Bookmark).filter(Bookmark.book_id == book_id).delete()
        s.query(ReadingState).filter(ReadingState.book_id == book_id).delete()
        s.delete(b)
        s.commit()
    return {"ok": True}


@app.post("/api/books/{book_id}/retry")
def retry_book(book_id: int):
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        if b.status != "failed":
            raise HTTPException(409, "الكتاب ليس في حالة فشل")
        b.status, b.error, b.stage = "queued", "", "في الانتظار"
        s.commit()
    worker.wake()
    return {"ok": True}


@app.get("/api/books/{book_id}/content")
def book_content(book_id: int):
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        chapters = []
        for ch in b.chapters:
            paras = [{"id": p.id, "k": p.kind, "t": p.tr, "s": p.src, "f": p.flagged} for p in ch.paragraphs]
            title = ch.title_tr or (paras[0]["t"] if paras and paras[0]["k"] == "h1" else "") \
                or ch.title_src or f"القسم {ch.index + 1}"
            chapters.append({"id": ch.id, "index": ch.index, "title": title, "done": ch.status == "done",
                             "paragraphs": paras})
        return {"book": _book_json(b), "chapters": chapters,
                "glossary": (b.glossary or {}).get("terms", [])}


@app.get("/api/books/{book_id}/pdf")
def download_pdf(book_id: int):
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        if not b.pdf_file:
            raise HTTPException(404, "ملف PDF غير جاهز بعد")
        path = config.OUTPUT_DIR / b.pdf_file
        name = Path(b.pdf_file).name
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.post("/api/books/{book_id}/export")
def re_export(book_id: int):
    """Regenerate the PDF (e.g. after the title was renamed)."""
    from .export.pdf import export_pdf
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        if b.status not in ("ready", "needs_review"):
            raise HTTPException(409, "الكتاب لم يكتمل بعد")
        b.pdf_file = export_pdf(b)
        s.commit()
        return _book_json(b)


@app.get("/api/books/{book_id}/cover")
def get_cover(book_id: int):
    with SessionLocal() as s:
        b = _get_book(s, book_id)
        if not b.cover_file:
            raise HTTPException(404)
        return FileResponse(config.COVER_DIR / b.cover_file)


# ---------------------------------------------------------------- reading state & bookmarks

@app.get("/api/books/{book_id}/state")
def get_state(book_id: int):
    with SessionLocal() as s:
        st = s.get(ReadingState, book_id)
        return {"paragraph_id": st.paragraph_id if st else None, "percent": st.percent if st else 0,
                "settings": (st.settings if st else None) or {}}


@app.put("/api/books/{book_id}/state")
async def put_state(book_id: int, request: Request):
    data = await request.json()
    with SessionLocal() as s:
        _get_book(s, book_id)
        st = s.get(ReadingState, book_id) or ReadingState(book_id=book_id)
        if "paragraph_id" in data:
            st.paragraph_id = data["paragraph_id"]
        if "percent" in data:
            st.percent = float(data["percent"])
        if "settings" in data and isinstance(data["settings"], dict):
            st.settings = data["settings"]
        s.add(st)
        s.commit()
    return {"ok": True}


@app.get("/api/books/{book_id}/bookmarks")
def list_bookmarks(book_id: int):
    with SessionLocal() as s:
        rows = s.query(Bookmark).filter(Bookmark.book_id == book_id).order_by(Bookmark.created_at).all()
        return [{"id": r.id, "paragraph_id": r.paragraph_id, "label": r.label,
                 "created_at": r.created_at.isoformat()} for r in rows]


@app.post("/api/books/{book_id}/bookmarks")
async def add_bookmark(book_id: int, request: Request):
    data = await request.json()
    with SessionLocal() as s:
        _get_book(s, book_id)
        pid = int(data["paragraph_id"])
        existing = s.query(Bookmark).filter_by(book_id=book_id, paragraph_id=pid).first()
        if existing:
            return {"id": existing.id, "paragraph_id": pid, "label": existing.label}
        bm = Bookmark(book_id=book_id, paragraph_id=pid, label=str(data.get("label", ""))[:300])
        s.add(bm)
        s.commit()
        return {"id": bm.id, "paragraph_id": pid, "label": bm.label}


@app.delete("/api/books/{book_id}/bookmarks/{bm_id}")
def delete_bookmark(book_id: int, bm_id: int):
    with SessionLocal() as s:
        s.query(Bookmark).filter_by(book_id=book_id, id=bm_id).delete()
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- pages

@app.get("/")
def library_page():
    return FileResponse(STATIC / "library.html")


@app.get("/read/{book_id}")
def reader_page(book_id: int):
    return FileResponse(STATIC / "reader.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
