"""One cover system for every book, with two styles the user can switch between.

calligraphic — deep dark ground, one accent colour, the Arabic title as a large
               calligraphic hero, the original title small and thin beneath it,
               small meta text in the four corners.
minimal      — monochrome on near-black, a small centred bilingual lockup
               (geometric Kufi Arabic over a bold Latin line) with a tiny
               uppercase descriptor.

`cover_spec` is the single source of truth: the library renders it with CSS,
the PDF renders it with the same proportions (sizes are in % of cover width).
"""
import hashlib
import re

CALLIGRAPHIC = [  # (ground, accent)
    ("#0B1E28", "#4A8BA8"),  # deep sea / teal
    ("#1A1410", "#C99A5B"),  # espresso / brass
    ("#141A12", "#8FA876"),  # forest night / sage
    ("#1D1220", "#B98AB8"),  # plum night / orchid
    ("#20100E", "#D07A5A"),  # oxblood / terracotta
    ("#0F1424", "#8C9BE0"),  # midnight / periwinkle
]
MINIMAL = [  # (ground, ink)
    ("#26221F", "#CFC9C1"),
    ("#1F2123", "#C8CCCF"),
    ("#232420", "#CDCBBF"),
    ("#2A2421", "#D2C6BA"),
]
BOLD_ACCENTS = ["#7EF0B8", "#C8F26A", "#FF8A6B", "#7CC8FF", "#F7D55C", "#E59CFF"]
CREAM = "#EDE6DA"
STYLES = {"calligraphic": "حروفي", "minimal": "هادئ", "bold": "جريء"}

CATEGORY_EN = {"literary": "Literature", "scientific": "Science", "technical": "Technology",
               "children": "Children", "religious": "Religion", "poetry": "Poetry", "legal": "Law & Finance",
               "self_help": "Leadership & Growth", "general": "Book"}
_AR = re.compile(r"[؀-ۿ]")


def get_cover_style(user_id: int | None = None) -> str:
    if user_id:
        from ..db import SessionLocal, User
        with SessionLocal() as s:
            u = s.get(User, user_id)
            if u and u.cover_style in STYLES:
                return u.cover_style
    return "calligraphic"


def _seed(title: str) -> int:
    return int(hashlib.md5(title.encode("utf-8")).hexdigest(), 16)


def _size(text: str, steps) -> float:
    n = len(text)
    for limit, size in steps:
        if n <= limit:
            return size
    return steps[-1][1]


def cover_spec(book, style: str | None = None) -> dict:
    style = style or get_cover_style(getattr(book, "user_id", None))
    title = (book.title or "").strip()
    title_ar = ((book.glossary or {}).get("title_ar") or "").strip()
    if _AR.search(title):
        ar, latin = title, ""
    else:
        ar, latin = title_ar, title
    if latin and ar and latin == ar:
        latin = ""
    from ..pipeline.profiles import PROFILES
    cat_ar = PROFILES.get(book.category, {}).get("label", "")
    cat_en = CATEGORY_EN.get(book.category, "Book")
    s = _seed(title)

    if style == "minimal":
        ground, ink = MINIMAL[s % len(MINIMAL)]
        hero = ar or latin
        return {"style": "minimal", "ground": ground, "ink": ink, "accent": ink,
                "ar": ar, "latin": latin if ar else "", "hero": hero, "hero_is_ar": bool(ar),
                "hero_size": _size(hero, [(8, 17.5), (14, 14), (22, 10.8), (32, 8.6), (999, 7)]),
                "latin_size": _size(latin, [(10, 8.5), (18, 6.4), (30, 4.8), (999, 3.8)]),
                "descriptor": cat_en.upper(), "author": book.author or "", "category": cat_ar}

    if style == "bold":
        accent = BOLD_ACCENTS[s % len(BOLD_ACCENTS)]
        hero = ar or latin
        return {"style": "bold", "ground": "#050505", "ink": "#F4F1EA", "accent": accent,
                "ar": ar, "latin": latin if ar else "", "hero": hero, "hero_is_ar": bool(ar),
                "hero_size": _size(hero, [(6, 33), (10, 26), (16, 19.5), (24, 15), (34, 12), (999, 9.8)])
                             * (1 if ar else 0.6),
                "latin_size": 2.6, "category": cat_ar, "author": book.author or "",
                "descriptor": cat_en.upper()}

    ground, accent = CALLIGRAPHIC[s % len(CALLIGRAPHIC)]
    hero = ar or latin
    return {"style": "calligraphic", "ground": ground, "ink": CREAM, "accent": accent,
            "ar": ar, "latin": latin if ar else "", "hero": hero, "hero_is_ar": bool(ar),
            "hero_size": _size(hero, [(6, 31), (10, 25), (16, 19), (24, 14.5), (34, 11.8), (999, 9.6)])
                         * (1 if ar else 0.62),
            "latin_size": _size(latin, [(10, 5.6), (20, 4.4), (32, 3.4), (999, 2.8)]),
            "category": cat_ar, "author": book.author or "", "descriptor": cat_en.upper()}


def cover_colors(title: str) -> tuple[str, str]:
    """Kept for callers that only need the two colours."""
    return CALLIGRAPHIC[_seed(title) % len(CALLIGRAPHIC)]


def _e(t) -> str:
    import html
    return html.escape(str(t or ""))


def cover_html(spec: dict, percent: float | None = None) -> str:
    """Markup shared by the library (inline) and the PDF. Sizes use --u = 1% of cover width."""
    st = spec["style"]
    hero_font = "hero-ar" if spec["hero_is_ar"] else "hero-lat"
    vars_ = (f"--ground:{spec['ground']};--ink:{spec['ink']};--accent:{spec['accent']};"
             f"--hs:{spec['hero_size']:.2f};--ls:{spec['latin_size']:.2f}")
    latin = f'<div class="bc-latin" dir="ltr">{_e(spec["latin"])}</div>' if spec["latin"] else ""
    hero = f'<div class="bc-hero {hero_font}">{_e(spec["hero"])}</div>'

    if st == "minimal":
        body = f"""<div class="bc-center"><div class="bc-lockup">
              <div class="bc-desc" dir="ltr">{_e(spec['descriptor'])} · TRANSLATED</div><div class="bc-titles">{hero}{latin}</div></div></div>
            <div class="bc-foot" dir="ltr">{_e(spec['author'])}</div>"""
    elif st == "bold":
        pct = max(0.0, min(100.0, percent or 0))
        body = f"""<div class="bc-corner tl" dir="ltr">AR · TRANSLATION<br>{_e(spec['descriptor'])}</div>
            <div class="bc-corner tr" dir="ltr">VOL<br>01</div>
            <div class="bc-center"><div class="bc-stage">{hero}
              <span class="bc-pill">{_e(spec['category'])}</span></div>{latin}</div>
            <div class="bc-corner bl" dir="ltr">{_e(spec['author'])}</div>
            <span class="bc-tag">#مترجم</span>
            <div class="bc-track"><span class="bc-mark" style="left:{pct:.1f}%"></span></div>"""
    else:  # calligraphic
        body = f"""<div class="bc-corner tr">#{_e(spec['category']).replace(' ', '_')}</div>
            <div class="bc-corner tl" dir="ltr">AR · TRANSLATION</div>
            <div class="bc-center">{hero}{latin}</div>
            <div class="bc-corner br">{_e(spec['author'])}</div>
            <div class="bc-corner bl">مكتبتي المترجمة</div>"""
    return f'<div class="bc bc-{st}" style="{vars_}">{body}</div>'
