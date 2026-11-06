"""One cover system for every book, so the library looks unified.

The same palette and layout are used by the library UI (CSS) and the PDF.
"""
import hashlib

# deep, bookish tones; text is always the cream color
PALETTE = [
    ("#1F3A5F", "#C9A227"),  # navy / gold
    ("#5B2333", "#E0B872"),  # wine / sand
    ("#22423A", "#D9C27A"),  # forest / brass
    ("#3B2F5C", "#E2B6A1"),  # indigo / rose
    ("#6B3E26", "#F0D08A"),  # leather / honey
    ("#2E3440", "#88C0D0"),  # slate / ice
    ("#3D5A40", "#E7CF9B"),  # sage / parchment
    ("#7A2E1F", "#F2C46D"),  # rust / amber
]
CREAM = "#F7F1E3"


def cover_colors(title: str) -> tuple[str, str]:
    h = int(hashlib.md5(title.encode("utf-8")).hexdigest(), 16)
    return PALETTE[h % len(PALETTE)]
