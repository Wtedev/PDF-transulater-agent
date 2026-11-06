"""The translation agents.

  glossary    -> reads the whole book (sampled) once: terms, names, style guide, summary
  translator  -> translates one segment (a few paragraphs) with glossary + previous context
  reviewer    -> compares source and translation, returns a score and a list of issues
                 (never rewrites). Low score sends the segment back to the translator.
  editor      -> fixes remaining issues and polishes Arabic without changing meaning
  consistency -> reads a finished chapter and unifies terms / transitions

Paragraphs travel as JSON lists of {"id", "text"} so alignment between source
and translation is never lost (the reader shows the original per paragraph).
"""
import json

from .. import config, llm
from .profiles import PROFILES, lang_name


def _context_block(glossary: dict, profile_key: str, src_lang: str) -> str:
    profile = PROFILES.get(profile_key, PROFILES["general"])
    terms = "\n".join(f"- {t.get('source')} => {t.get('target')}" + (f"  ({t['note']})" if t.get("note") else "")
                      for t in glossary.get("terms", []))
    return (f"BOOK CONTEXT (shared by all translation agents)\n"
            f"Source language: {lang_name(src_lang)}. Target language: Arabic (Modern Standard Arabic).\n"
            f"Content type: {profile['label']} — {profile['translator']}\n\n"
            f"Book summary:\n{glossary.get('summary', '')}\n\n"
            f"Style guide:\n{glossary.get('style', '')}\n\n"
            f"Glossary (mandatory, always use these renderings):\n{terms or '- (none)'}\n")


# ---------------------------------------------------------------- glossary agent

GLOSSARY_INSTRUCTIONS = """You are the terminology and context agent of a professional book translation team (target: Arabic).
Read the sample of the book and produce what every other translator must follow. Return JSON only:
{"summary": "5-8 sentence summary of the book in Arabic",
 "style": "style guide in Arabic: register, tone, how to handle dialogue, quotes, numbers, foreign terms",
 "terms": [{"source":"term/name as in the book","target":"fixed Arabic rendering","note":"optional"}]}
Include every recurring person/place name, invented term, and domain term (up to 150). Arabic renderings
of names must be phonetic and consistent."""


def build_glossary(blocks: list[dict], profile_key: str, src_lang: str) -> dict:
    texts = [b["text"] for b in blocks if b["kind"] != "code"]
    # spread the sample across the whole book, chapter openings first
    heads = [i for i, b in enumerate(blocks) if b["kind"] == "h1"]
    picked, budget = [], 60000
    for i in heads:
        chunk = " ".join(b["text"] for b in blocks[i:i + 6])
        picked.append(chunk)
    step = max(1, len(texts) // 80)
    picked += texts[::step]
    sample = "\n".join(picked)[:budget]

    def mock():
        return {"summary": "ملخص تجريبي للكتاب (وضع الاختبار).",
                "style": "عربية فصحى معاصرة واضحة.", "terms": []}

    result = llm.call_json(agent="glossary", model=config.MODEL_GLOSSARY,
                           instructions=GLOSSARY_INSTRUCTIONS,
                           user=f"Content type: {PROFILES[profile_key]['label']}. Source language: {lang_name(src_lang)}.\n\nBOOK SAMPLE:\n{sample}",
                           mock=mock, max_tokens=12000)
    result.setdefault("terms", [])
    return result


# ---------------------------------------------------------------- translator

TRANSLATOR_INSTRUCTIONS = """You are a senior book translator into Arabic.
Translate every paragraph you receive, faithfully and completely: no omissions, no additions, no summaries.
Follow the book context, the glossary and the style guide exactly.
Paragraph kinds: h1/h2/h3 are headings (translate concisely), code is never translated (copy it as is),
quote is a quotation, list is a list item.
Return JSON only: {"paragraphs":[{"id": <same id>, "text": "Arabic translation"}]} with exactly the same ids, same order."""


def _mock_translation(paras):
    return {"paragraphs": [{"id": p["id"], "text": p["text"] if p["kind"] == "code" else f"[ترجمة تجريبية] {p['text']}"}
                           for p in paras]}


def translate(paras: list[dict], ctx: str, previous: list[str], feedback: list[dict] | None = None,
              draft: list[dict] | None = None) -> dict[int, str]:
    user = ""
    if previous:
        user += "PREVIOUS TRANSLATED PARAGRAPHS (for continuity, do not translate again):\n" + "\n".join(previous) + "\n\n"
    user += "PARAGRAPHS TO TRANSLATE:\n" + json.dumps(
        [{"id": p["id"], "kind": p["kind"], "text": p["text"]} for p in paras], ensure_ascii=False)
    if feedback:
        user += ("\n\nA reviewer rejected your previous draft. Fix ALL these issues:\n" +
                 json.dumps(feedback, ensure_ascii=False) +
                 "\n\nPrevious draft:\n" + json.dumps(draft, ensure_ascii=False))
    result = llm.call_json(agent="translator", model=config.MODEL_TRANSLATOR,
                           instructions=TRANSLATOR_INSTRUCTIONS, cached_context=ctx, user=user,
                           mock=lambda: _mock_translation(paras), max_tokens=16000)
    return _aligned(paras, result)


def _aligned(paras, result) -> dict[int, str]:
    got = {int(p["id"]): str(p.get("text", "")) for p in result.get("paragraphs", []) if "id" in p}
    missing = [p["id"] for p in paras if not got.get(p["id"], "").strip()]
    if missing:
        raise ValueError(f"missing paragraphs in model output: {missing}")
    return {p["id"]: got[p["id"]] for p in paras}


# ---------------------------------------------------------------- reviewer

REVIEWER_INSTRUCTIONS = """You are the quality reviewer of a book translation into Arabic.
Compare each translated paragraph with its source. You do NOT rewrite; you only report.
Check: mistranslation, omission, addition, wrong number/name, glossary violations, untranslated text, broken Arabic.
%s
Return JSON only:
{"score": 0-10 (10 = publishable),
 "issues":[{"id": paragraph id, "type":"mistranslation|omission|addition|glossary|number|fluency|other",
            "severity":"critical|major|minor", "source_excerpt":"...", "problem":"...", "suggestion":"..."}]}
Score below 8 if there is any critical issue or more than two major issues."""


def review(paras: list[dict], translation: dict[int, str], ctx: str, profile_key: str) -> dict:
    pairs = [{"id": p["id"], "kind": p["kind"], "source": p["text"], "translation": translation[p["id"]]}
             for p in paras]
    result = llm.call_json(agent="reviewer", model=config.MODEL_REVIEWER,
                           instructions=REVIEWER_INSTRUCTIONS % PROFILES[profile_key]["reviewer"],
                           cached_context=ctx, user=json.dumps(pairs, ensure_ascii=False),
                           mock=lambda: {"score": 9, "issues": []}, max_tokens=6000)
    result["score"] = float(result.get("score", 0))
    result["issues"] = result.get("issues", []) or []
    return result


# ---------------------------------------------------------------- editor

EDITOR_INSTRUCTIONS = """You are the Arabic editor of a translated book.
Your job: fix the reviewer's remaining issues, then polish the Arabic so it reads as if originally written in Arabic:
correct grammar and punctuation, natural word order, smooth links between sentences and with the previous paragraphs.
Editing freedom for this book: %s (none = only fix listed issues and grammar; low = minimal wording changes;
medium = free rephrasing while keeping every piece of meaning).
NEVER change meaning, never drop or add information, never touch code, keep glossary renderings.
Return JSON only: {"paragraphs":[{"id": <same id>, "text": "final Arabic"}]} with exactly the same ids."""


def edit(paras: list[dict], translation: dict[int, str], issues: list[dict], ctx: str,
         profile_key: str, previous: list[str]) -> dict[int, str]:
    payload = [{"id": p["id"], "kind": p["kind"], "source": p["text"], "translation": translation[p["id"]]}
               for p in paras]
    user = ""
    if previous:
        user += "PREVIOUS PARAGRAPHS (context only):\n" + "\n".join(previous) + "\n\n"
    user += "PARAGRAPHS:\n" + json.dumps(payload, ensure_ascii=False)
    if issues:
        user += "\n\nREVIEWER ISSUES STILL TO FIX:\n" + json.dumps(issues, ensure_ascii=False)
    result = llm.call_json(agent="editor", model=config.MODEL_EDITOR,
                           instructions=EDITOR_INSTRUCTIONS % PROFILES[profile_key]["editor_freedom"],
                           cached_context=ctx, user=user,
                           mock=lambda: {"paragraphs": [{"id": i, "text": t} for i, t in translation.items()]},
                           max_tokens=16000)
    return _aligned(paras, result)


# ---------------------------------------------------------------- chapter consistency

CONSISTENCY_INSTRUCTIONS = """You are the final consistency checker for one translated chapter (Arabic).
Read the whole chapter. Find: the same term or name rendered differently, glossary violations,
contradictions, repeated sentences, abrupt transitions between paragraphs.
Return ONLY the paragraphs that must change, with their full corrected text:
{"changes":[{"id": paragraph id, "text":"corrected full paragraph", "reason":"..."}]}
If nothing needs changing return {"changes":[]}. Never change meaning."""


def consistency(chapter_paras: list[dict], ctx: str) -> dict[int, str]:
    """chapter_paras: [{id, kind, tr}] — processed in windows to stay within limits."""
    changes: dict[int, str] = {}
    window, size = [], 0
    windows = []
    for p in chapter_paras:
        window.append(p)
        size += len(p["tr"])
        if size > 24000:
            windows.append(window)
            window, size = [], 0
    if window:
        windows.append(window)
    for w in windows:
        result = llm.call_json(agent="consistency", model=config.MODEL_CONSISTENCY,
                               instructions=CONSISTENCY_INSTRUCTIONS, cached_context=ctx,
                               user=json.dumps([{"id": p["id"], "kind": p["kind"], "text": p["tr"]} for p in w],
                                               ensure_ascii=False),
                               mock=lambda: {"changes": []}, max_tokens=12000)
        valid = {p["id"] for p in w if p["kind"] != "code"}
        for c in result.get("changes", []):
            try:
                pid = int(c["id"])
            except (KeyError, ValueError, TypeError):
                continue
            if pid in valid and str(c.get("text", "")).strip():
                changes[pid] = c["text"]
    return changes


def context_for(book_glossary: dict, profile_key: str, src_lang: str) -> str:
    return _context_block(book_glossary, profile_key, src_lang)
