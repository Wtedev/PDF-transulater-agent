"""Central settings, all overridable through environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
COVER_DIR = DATA_DIR / "covers"
for _d in (UPLOAD_DIR, OUTPUT_DIR, COVER_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or f"sqlite:///{DATA_DIR / 'books.db'}"
# Railway gives postgres:// or postgresql:// URLs; pin the driver to psycopg 3
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(_prefix):]
        break

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# MOCK_LLM=1 runs the whole pipeline without calling the API (for local testing).
MOCK_LLM = os.getenv("MOCK_LLM", "0") == "1"

# Each agent can use a different model. The reviewer defaults to a different
# model than the translator so it does not wave through its own mistakes.
MODEL_ROUTER = os.getenv("MODEL_ROUTER", "claude-haiku-5-5")
MODEL_OCR = os.getenv("MODEL_OCR", "claude-sonnet-5-5")
MODEL_GLOSSARY = os.getenv("MODEL_GLOSSARY", "claude-sonnet-5-5")
MODEL_TRANSLATOR = os.getenv("MODEL_TRANSLATOR", "claude-sonnet-5-5")
MODEL_REVIEWER = os.getenv("MODEL_REVIEWER", "claude-opus-5-5")
MODEL_EDITOR = os.getenv("MODEL_EDITOR", "claude-sonnet-5-5")
MODEL_CONSISTENCY = os.getenv("MODEL_CONSISTENCY", "claude-sonnet-5-5")

SEGMENT_WORDS = int(os.getenv("SEGMENT_WORDS", "900"))
REVIEW_PASS_SCORE = float(os.getenv("REVIEW_PASS_SCORE", "8"))
MAX_RETRANSLATIONS = int(os.getenv("MAX_RETRANSLATIONS", "2"))
CHAPTER_WORKERS = int(os.getenv("CHAPTER_WORKERS", "3"))
OCR_MIN_CONFIDENCE = float(os.getenv("OCR_MIN_CONFIDENCE", "0.75"))


FONT_DIR = BASE_DIR / "fonts"
