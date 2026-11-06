"""Web app: upload, library, reader API and static pages."""
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

from . import config, security, worker
from .db import ApiKey, Book, Bookmark, Chapter, Paragraph, ReadingState, SessionLocal, User, init_db
from .export.covers import STYLES, cover_html, cover_spec
from .providers import PROVIDERS, SHARED_TOKENS_PER_USER, test_key
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


SIGNUP_MODE = os.getenv("SIGNUP_MODE", "open")      # open | invite | closed
INVITE_CODE = os.getenv("INVITE_CODE", "")
PUBLIC_PATHS = ("/health", "/login", "/api/auth/", "/static/")
COOKIE = "session"


@app.middleware("http")
async def auth(request: Request, call_next):
    request.state.user_id = security.read_session(request.cookies.get(COOKIE))
    path = request.url.path
    if request.state.user_id is None and not path.startswith(PUBLIC_PATHS):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "سجّل الدخول أولاً"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


def current_user(request: Request) -> int:
    uid = request.state.user_id
    if uid is None:
        raise HTTPException(401, "سجّل الدخول أولاً")
    return uid


def _login_response(user_id: int, payload: dict) -> JSONResponse:
    resp = JSONResponse(payload)
    resp.set_cookie(COOKIE, security.make_session(user_id), max_age=security.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "1") == "1")
    return resp


def _user_json(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "is_admin": u.is_admin, "key_order": u.key_order,
            "cover_style": u.cover_style, "shared_tokens": u.shared_tokens or 0,
            "shared_limit": SHARED_TOKENS_PER_USER, "shared_available": bool(config.ANTHROPIC_API_KEY)}


@app.post("/api/auth/register")
async def register(request: Request):
    d = await request.json()
    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))
    if SIGNUP_MODE == "closed":
        raise HTTPException(403, "التسجيل مغلق حالياً")
    if SIGNUP_MODE == "invite" and d.get("invite", "") != INVITE_CODE:
        raise HTTPException(403, "رمز الدعوة غير صحيح")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "البريد الإلكتروني غير صحيح")
    if len(password) < 8:
        raise HTTPException(400, "كلمة المرور يجب أن تكون 8 أحرف على الأقل")
    with SessionLocal() as s:
        if s.query(User).filter(User.email == email).first():
            raise HTTPException(409, "هذا البريد مسجل مسبقاً، سجّل الدخول")
        first = s.query(User).count() == 0
        u = User(email=email, name=str(d.get("name", "")).strip()[:200],
                 password_hash=security.hash_password(password), is_admin=first)
        s.add(u)
        s.flush()
        if first:   # books uploaded before accounts existed belong to the first (admin) user
            s.query(Book).filter(Book.user_id.is_(None)).update({"user_id": u.id}, synchronize_session=False)
        s.commit()
        return _login_response(u.id, _user_json(u))


@app.post("/api/auth/login")
async def login(request: Request):
    d = await request.json()
    with SessionLocal() as s:
        u = s.query(User).filter(User.email == str(d.get("email", "")).strip().lower()).first()
        if not u or not security.verify_password(str(d.get("password", "")), u.password_hash):
            raise HTTPException(401, "البريد أو كلمة المرور غير صحيحة")
        return _login_response(u.id, _user_json(u))


@app.post("/api/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/auth/me")
def me(request: Request):
    uid = request.state.user_id
    if uid is None:
        return {"user": None, "signup": SIGNUP_MODE}
    with SessionLocal() as s:
        u = s.get(User, uid)
        return {"user": _user_json(u) if u else None, "signup": SIGNUP_MODE}


@app.put("/api/me")
async def update_me(request: Request, uid: int = Depends(current_user)):
    d = await request.json()
    with SessionLocal() as s:
        u = s.get(User, uid)
        if d.get("key_order") in ("shared_first", "mine_first", "mine_only"):
            u.key_order = d["key_order"]
        if d.get("cover_style") in STYLES:
            u.cover_style = d["cover_style"]
        s.commit()
        return _user_json(u)


# ---------------------------------------------------------------- API keys

def _key_json(k: ApiKey) -> dict:
    return {"id": k.id, "provider": k.provider, "provider_label": PROVIDERS[k.provider]["label"],
            "label": k.label, "last4": k.last4, "model_main": k.model_main, "model_fast": k.model_fast,
            "default_main": PROVIDERS[k.provider]["main"], "status": k.status, "status_note": k.status_note,
            "tokens_used": k.tokens_used or 0, "position": k.position}


@app.get("/api/keys")
def list_keys(uid: int = Depends(current_user)):
    with SessionLocal() as s:
        rows = s.query(ApiKey).filter(ApiKey.user_id == uid).order_by(ApiKey.position, ApiKey.id).all()
        return {"keys": [_key_json(k) for k in rows],
                "providers": [{"key": k, "label": v["label"], "main": v["main"], "fast": v["fast"]}
                              for k, v in PROVIDERS.items()]}


def _requeue_waiting(uid: int):
    with SessionLocal() as s:
        n = s.query(Book).filter(Book.user_id == uid, Book.status == "no_credit") \
            .update({"status": "queued", "stage": "في الانتظار — سيكمل بمفتاحك"}, synchronize_session=False)
        s.commit()
    if n:
        worker.wake()
    return n


@app.post("/api/keys/test")
async def test_api_key(request: Request, uid: int = Depends(current_user)):
    d = await request.json()
    provider, key = d.get("provider"), str(d.get("key", "")).strip()
    if provider not in PROVIDERS or not key:
        raise HTTPException(400, "اختر المزود والصق المفتاح")
    ok, msg = test_key(provider, key, str(d.get("model", "")).strip())
    return {"ok": ok, "message": msg}


@app.post("/api/keys")
async def add_key(request: Request, uid: int = Depends(current_user)):
    d = await request.json()
    provider, key = d.get("provider"), str(d.get("key", "")).strip()
    if provider not in PROVIDERS or len(key) < 12:
        raise HTTPException(400, "اختر المزود والصق مفتاحاً صحيحاً")
    with SessionLocal() as s:
        pos = (s.query(ApiKey).filter(ApiKey.user_id == uid).count())
        k = ApiKey(user_id=uid, provider=provider, label=str(d.get("label", "")).strip()[:100],
                   secret=security.encrypt(key), last4=key[-4:], position=pos,
                   model_main=str(d.get("model_main", "")).strip()[:120],
                   model_fast=str(d.get("model_fast", "")).strip()[:120])
        s.add(k)
        s.commit()
        out = _key_json(k)
    out["resumed_books"] = _requeue_waiting(uid)
    return out


@app.patch("/api/keys/{key_id}")
async def update_key(key_id: int, request: Request, uid: int = Depends(current_user)):
    d = await request.json()
    with SessionLocal() as s:
        k = s.query(ApiKey).filter_by(id=key_id, user_id=uid).first()
        if not k:
            raise HTTPException(404, "المفتاح غير موجود")
        for f in ("label", "model_main", "model_fast"):
            if f in d:
                setattr(k, f, str(d[f]).strip()[:120])
        if d.get("reset_status"):
            k.status, k.status_note = "ok", ""
        if "position" in d:
            k.position = int(d["position"])
        s.commit()
        out = _key_json(k)
    if d.get("reset_status"):
        out["resumed_books"] = _requeue_waiting(uid)
    return out


@app.delete("/api/keys/{key_id}")
def delete_key(key_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        s.query(ApiKey).filter_by(id=key_id, user_id=uid).delete()
        s.commit()
    return {"ok": True}


def _book_json(b: Book, extra: dict | None = None) -> dict:
    spec = cover_spec(b)
    percent = (extra or {}).get("read_percent")
    d = {"id": b.id, "title": b.title, "author": b.author, "category": b.category,
         "category_label": PROFILES.get(b.category, {}).get("label", ""),
         "format": b.file_format, "format_label": FORMAT_LABELS.get(b.file_format, ""),
         "source_lang": b.source_lang, "status": b.status, "stage": b.stage,
         "progress": b.progress, "error": b.error, "flagged": b.flagged_count,
         "has_pdf": bool(b.pdf_file), "has_cover": bool(b.cover_file), "key_used": b.key_used or "",
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
def meta(uid: int = Depends(current_user)):
    with SessionLocal() as s:
        u = s.get(User, uid)
        return {"categories": [{"key": k, "label": v["label"]} for k, v in PROFILES.items()],
                "mock": config.MOCK_LLM, "cover_style": u.cover_style,
                "cover_styles": [{"key": k, "label": v} for k, v in STYLES.items()],
                "user": _user_json(u)}


@app.get("/api/books")
def list_books(uid: int = Depends(current_user)):
    with SessionLocal() as s:
        books = s.query(Book).filter(Book.user_id == uid).order_by(Book.created_at.desc()).all()
        ids = [b.id for b in books]
        states = {r.book_id: r for r in s.query(ReadingState).filter(ReadingState.book_id.in_(ids)).all()} if ids else {}
        out = []
        for b in books:
            st = states.get(b.id)
            out.append(_book_json(b, {"read_percent": st.percent if st else 0,
                                      "last_read": st.updated_at.isoformat() if st and st.updated_at else None}))
        return out


@app.post("/api/books")
async def upload_book(file: UploadFile = File(...), title: str = Form(...), author: str = Form(""),
                      source_lang: str = Form("auto"), category: str = Form("auto"),
                      rights: str = Form(""), cover: UploadFile | None = File(None),
                      uid: int = Depends(current_user)):
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
        b = Book(user_id=uid, title=title, author=author.strip(), source_lang=source_lang or "auto",
                 category="" if category == "auto" else category, original_filename=file.filename or "",
                 stored_file=stored, cover_file=cover_name, status="queued", stage="في الانتظار")
        s.add(b)
        s.commit()
        worker.wake()
        return _book_json(b)


def _get_book(s, book_id: int, uid: int) -> Book:
    b = s.get(Book, book_id)
    if not b or b.user_id != uid:
        raise HTTPException(404, "الكتاب غير موجود")
    return b


@app.get("/api/books/{book_id}")
def get_book(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
        return _book_json(b, {"router_info": b.router_info, "glossary_terms": len((b.glossary or {}).get("terms", []))})


@app.patch("/api/books/{book_id}")
async def rename_book(book_id: int, request: Request, uid: int = Depends(current_user)):
    data = await request.json()
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
        if data.get("title", "").strip():
            b.title = data["title"].strip()
        if "author" in data:
            b.author = data["author"].strip()
        s.commit()
        return _book_json(b)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
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
def retry_book(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
        if b.status not in ("failed", "no_credit"):
            raise HTTPException(409, "الكتاب ليس متوقفاً")
        b.status, b.error, b.stage = "queued", "", "في الانتظار"
        s.commit()
    worker.wake()
    return {"ok": True}


@app.get("/api/books/{book_id}/content")
def book_content(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
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
def download_pdf(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
        if not b.pdf_file:
            raise HTTPException(404, "ملف PDF غير جاهز بعد")
        path = config.OUTPUT_DIR / b.pdf_file
        name = Path(b.pdf_file).name
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.post("/api/books/{book_id}/export")
def re_export(book_id: int, uid: int = Depends(current_user)):
    """Regenerate the PDF (e.g. after the title was renamed)."""
    from .export.pdf import export_pdf
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
        if b.status not in ("ready", "needs_review"):
            raise HTTPException(409, "الكتاب لم يكتمل بعد")
        b.pdf_file = export_pdf(b)
        s.commit()
        return _book_json(b)


@app.get("/api/books/{book_id}/cover")
def get_cover(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        b = _get_book(s, book_id, uid)
        if not b.cover_file:
            raise HTTPException(404)
        return FileResponse(config.COVER_DIR / b.cover_file)


# ---------------------------------------------------------------- reading state & bookmarks

@app.get("/api/books/{book_id}/state")
def get_state(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        _get_book(s, book_id, uid)
        st = s.get(ReadingState, book_id)
        return {"paragraph_id": st.paragraph_id if st else None, "percent": st.percent if st else 0,
                "settings": (st.settings if st else None) or {}}


@app.put("/api/books/{book_id}/state")
async def put_state(book_id: int, request: Request, uid: int = Depends(current_user)):
    data = await request.json()
    with SessionLocal() as s:
        _get_book(s, book_id, uid)
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
def list_bookmarks(book_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        _get_book(s, book_id, uid)
        rows = s.query(Bookmark).filter(Bookmark.book_id == book_id).order_by(Bookmark.created_at).all()
        return [{"id": r.id, "paragraph_id": r.paragraph_id, "label": r.label,
                 "created_at": r.created_at.isoformat()} for r in rows]


@app.post("/api/books/{book_id}/bookmarks")
async def add_bookmark(book_id: int, request: Request, uid: int = Depends(current_user)):
    data = await request.json()
    with SessionLocal() as s:
        _get_book(s, book_id, uid)
        pid = int(data["paragraph_id"])
        existing = s.query(Bookmark).filter_by(book_id=book_id, paragraph_id=pid).first()
        if existing:
            return {"id": existing.id, "paragraph_id": pid, "label": existing.label}
        bm = Bookmark(book_id=book_id, paragraph_id=pid, label=str(data.get("label", ""))[:300])
        s.add(bm)
        s.commit()
        return {"id": bm.id, "paragraph_id": pid, "label": bm.label}


@app.delete("/api/books/{book_id}/bookmarks/{bm_id}")
def delete_bookmark(book_id: int, bm_id: int, uid: int = Depends(current_user)):
    with SessionLocal() as s:
        _get_book(s, book_id, uid)
        s.query(Bookmark).filter_by(book_id=book_id, id=bm_id).delete()
        s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- pages

@app.get("/")
def library_page():
    return FileResponse(STATIC / "library.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/keys")
def keys_page():
    return FileResponse(STATIC / "keys.html")


@app.get("/read/{book_id}")
def reader_page(book_id: int):
    return FileResponse(STATIC / "reader.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
