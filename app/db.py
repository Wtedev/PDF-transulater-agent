"""Database models.

Book -> Chapter -> Paragraph. Each paragraph keeps both the source text and the
translation, so the reader can show the original next to any paragraph and
bookmarks can anchor to a paragraph id (page numbers change with font size).
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Integer, String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    name = Column(String(200), default="")
    password_hash = Column(String(300), nullable=False)
    is_admin = Column(Boolean, default=False)
    key_order = Column(String(20), default="shared_first")   # shared_first | mine_first | mine_only
    shared_tokens = Column(Integer, default=0)               # tokens spent on the shared key
    cover_style = Column(String(20), default="calligraphic")
    created_at = Column(DateTime(timezone=True), default=now)


class ApiKey(Base):
    """A user's own provider key, encrypted at rest."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider = Column(String(20), nullable=False)            # anthropic | openai | openrouter
    label = Column(String(100), default="")
    secret = Column(Text, nullable=False)                    # Fernet-encrypted key
    last4 = Column(String(8), default="")
    model_main = Column(String(120), default="")             # empty = provider default
    model_fast = Column(String(120), default="")
    status = Column(String(20), default="ok")                # ok | no_credit | invalid
    status_note = Column(Text, default="")
    position = Column(Integer, default=0)
    tokens_used = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=now)


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    key_used = Column(String(200), default="")               # which key(s) did the work, for display
    title = Column(String(500), nullable=False)          # the name the user typed at upload
    author = Column(String(300), default="")
    source_lang = Column(String(20), default="auto")
    target_lang = Column(String(20), default="ar")
    category = Column(String(40), default="")            # content route: literary, scientific...
    file_format = Column(String(40), default="")         # extraction route: text_pdf, scanned_pdf...
    original_filename = Column(String(500), default="")
    stored_file = Column(String(500), default="")
    cover_file = Column(String(500), default="")
    pdf_file = Column(String(500), default="")
    status = Column(String(30), default="queued")         # queued, analyzing, translating, exporting, ready, needs_review, failed
    stage = Column(String(200), default="")              # human readable current step
    progress = Column(Float, default=0.0)                # 0..100
    error = Column(Text, default="")
    router_info = Column(JSON, default=dict)
    glossary = Column(JSON, default=dict)                # {terms: [...], style: "...", summary: "..."}
    flagged_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)

    chapters = relationship("Chapter", back_populates="book", cascade="all, delete-orphan",
                            order_by="Chapter.index")


class Chapter(Base):
    __tablename__ = "chapters"
    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), index=True)
    index = Column(Integer, nullable=False)
    title_src = Column(Text, default="")
    title_tr = Column(Text, default="")
    status = Column(String(20), default="pending")       # pending, done
    book = relationship("Book", back_populates="chapters")
    paragraphs = relationship("Paragraph", back_populates="chapter", cascade="all, delete-orphan",
                              order_by="Paragraph.index")


class Paragraph(Base):
    __tablename__ = "paragraphs"
    id = Column(Integer, primary_key=True)
    chapter_id = Column(Integer, ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    index = Column(Integer, nullable=False)
    segment = Column(Integer, default=0)                 # translation batch number within chapter
    kind = Column(String(20), default="p")               # p, h2, h3, quote, code, list
    src = Column(Text, default="")
    tr = Column(Text, default="")
    page = Column(Integer, default=0)                    # source page, for traceability
    score = Column(Float, nullable=True)                 # reviewer score of its segment
    flagged = Column(Boolean, default=False)             # low score after max retries / low OCR confidence
    chapter = relationship("Chapter", back_populates="paragraphs")


class Bookmark(Base):
    __tablename__ = "bookmarks"
    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), index=True)
    paragraph_id = Column(Integer, nullable=False)
    label = Column(String(300), default="")
    created_at = Column(DateTime(timezone=True), default=now)


class ReadingState(Base):
    __tablename__ = "reading_state"
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), primary_key=True)
    paragraph_id = Column(Integer, nullable=True)
    percent = Column(Float, default=0.0)
    settings = Column(JSON, default=dict)                # per-book reader settings
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


def _migrate():
    """create_all() never adds columns to existing tables; add the new ones here."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    wanted = {"books": [("user_id", "INTEGER"), ("key_used", "VARCHAR(200) DEFAULT ''")]}
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if not insp.has_table(table):
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db():
    Base.metadata.create_all(engine)
    _migrate()
