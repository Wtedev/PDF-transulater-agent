"""Background worker: a database-backed queue, one book at a time.

Books are long jobs (minutes to hours), so they never run inside a web
request. On startup, books that were interrupted mid-way are re-queued and
resume from their last saved segment.
"""
import logging
import threading
import time

from .db import Book, SessionLocal
from .pipeline.run import process_book

log = logging.getLogger("worker")
_wake = threading.Event()
IN_PROGRESS = ("analyzing", "translating", "exporting")


def wake():
    _wake.set()


def _next_book_id():
    with SessionLocal() as s:
        b = s.query(Book).filter(Book.status == "queued").order_by(Book.created_at).first()
        return b.id if b else None


def _loop():
    with SessionLocal() as s:
        s.query(Book).filter(Book.status.in_(IN_PROGRESS)).update({"status": "queued"},
                                                                    synchronize_session=False)
        s.commit()
    while True:
        try:
            book_id = _next_book_id()
            if book_id is None:
                _wake.wait(timeout=10)
                _wake.clear()
                continue
            log.info("processing book %s", book_id)
            process_book(book_id)
        except Exception:
            log.exception("worker loop error")
            time.sleep(5)


def start():
    t = threading.Thread(target=_loop, name="book-worker", daemon=True)
    t.start()
