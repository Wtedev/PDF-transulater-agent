"""Content routes: the same agents, tuned per kind of book."""

PROFILES = {
    "literary": {
        "label": "أدبي",
        "translator": "Literary translation. Preserve voice, tone, rhythm and imagery. Dialogue must sound natural in Arabic. Keep character names exactly as in the glossary.",
        "editor_freedom": "medium",
        "reviewer": "Check meaning, tone, dialogue attribution and name consistency.",
    },
    "scientific": {
        "label": "علمي وأكاديمي",
        "translator": "Scientific translation. Terminology accuracy comes first; use the established Arabic scientific term and add the English term in parentheses on first use. Do not translate equations, symbols, units or reference lists.",
        "editor_freedom": "low",
        "reviewer": "Check every technical term, number, unit and claim against the source. Any change in a number is a critical error.",
    },
    "technical": {
        "label": "تقني وبرمجي",
        "translator": "Technical translation. Never translate code, commands, file names, APIs or UI strings; keep them in Latin script. Common technical terms may stay in English in parentheses.",
        "editor_freedom": "low",
        "reviewer": "Check that code, commands and identifiers are untouched and instructions are still correct.",
    },
    "children": {
        "label": "أطفال",
        "translator": "Children's book. Simple, warm, rhythmic Modern Standard Arabic suited to young readers; short sentences.",
        "editor_freedom": "medium",
        "reviewer": "Check meaning, simplicity and age-appropriate wording.",
    },
    "religious": {
        "label": "ديني",
        "translator": "Religious text. Do NOT translate Quran verses or hadith: write the original Arabic text if it is identifiable, otherwise keep a faithful translation marked [ترجمة المعنى]. Be literal and respectful; no paraphrasing of doctrine.",
        "editor_freedom": "low",
        "reviewer": "Flag any verse/hadith that was re-translated instead of quoted, and any doctrinal shift in meaning (critical).",
    },
    "poetry": {
        "label": "شعر",
        "translator": "Poetry. Interpretive translation that keeps line breaks, imagery and as much rhythm as possible. Keep one output paragraph per input stanza.",
        "editor_freedom": "medium",
        "reviewer": "Check imagery and meaning; do not demand literalness.",
    },
    "legal": {
        "label": "قانوني ومالي",
        "translator": "Legal/financial translation. Strictly literal and precise; keep defined terms consistent; never soften obligations (shall/must/may).",
        "editor_freedom": "none",
        "reviewer": "Any change of obligation, number, date, party or defined term is critical.",
    },
    "self_help": {
        "label": "تطوير ذات وأعمال",
        "translator": "Popular non-fiction. Smooth, engaging, reader-friendly Modern Standard Arabic; adapt idioms to natural Arabic equivalents.",
        "editor_freedom": "medium",
        "reviewer": "Check meaning and that idioms were adapted, not translated literally.",
    },
    "general": {
        "label": "عام",
        "translator": "Clear, accurate, natural Modern Standard Arabic.",
        "editor_freedom": "medium",
        "reviewer": "Check meaning, omissions and fluency.",
    },
}

LANG_NAMES = {"ar": "Arabic", "en": "English", "fr": "French", "de": "German", "es": "Spanish",
              "tr": "Turkish", "zh": "Chinese", "ru": "Russian", "ja": "Japanese", "ur": "Urdu",
              "fa": "Persian", "it": "Italian", "he": "Hebrew"}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)
