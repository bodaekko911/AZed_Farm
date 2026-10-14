"""How names are compared: one normalisation for the matcher and the vocabulary.

Lower-cased, punctuation to spaces, whitespace collapsed — and for Arabic, the
spellings that mean the same word made the same: أ إ آ ٱ → ا, ة → ه, ى → ي,
tashkeel and tatweel removed, Arabic-Indic digits to 0-9. "طماطم بلدى" and
"طَماطِم بلدي" are one name; so are "القاهرة" and "القاهره".

Both sides of every comparison go through this — the text being looked up and
the names (or aliases) it is compared with — so it has to stay a pure function.
"""
import re
from typing import Any

_LETTERS = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ة": "ه",
    "ى": "ي",
    "ـ": "",                                    # tatweel
    "٫": ".", "٬": ",",                          # Arabic decimal / thousands marks
    **{chr(0x0660 + d): str(d) for d in range(10)},   # ٠-٩
    **{chr(0x06F0 + d): str(d) for d in range(10)},   # ۰-۹ (Persian forms)
})
# Harakat, Quranic marks and superscript alef.
_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۜ۟-۪ۨ-ۭ]")
_NON_WORD = re.compile(r"[^\w؀-ۿ]+")
_SPACES = re.compile(r"\s+")


def normalize(text: Any) -> str:
    t = _TASHKEEL.sub("", str(text or "").lower()).translate(_LETTERS)
    return _SPACES.sub(" ", _NON_WORD.sub(" ", t)).strip()
