# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
"""Script detection and Chinese normalisation, shared by the transcription and
translation paths.

Scribe's detect language can be left on auto, in which case `language_code` is
empty and tells us nothing about what was actually spoken. Anything script-specific
— OpenCC, length thresholds, segment boundaries — has to judge the text itself,
because the setting is silent exactly when the session is multilingual.

Both OpenCC converters live here and are private: every conversion goes through the
functions below so the kana guard cannot be bypassed by reaching for the raw one.
"""
import re
from opencc import OpenCC

from .config import REALTIME_SETTINGS

_cc_s2tw = OpenCC('s2tw')
_cc_tw2s = OpenCC('tw2s')

# Hiragana + katakana + halfwidth katakana. Checked first in dominant_script:
# Japanese mixes kana with Han, so counting Han first would call every Japanese
# line Chinese.
_KANA_RE = re.compile(r'[぀-ヿｦ-ﾝ]')
# Syllables + conjoining jamo + compatibility jamo. Checked second for the same
# reason: Korean can carry Han hanja.
_HANGUL_RE = re.compile(r'[가-힣ᄀ-ᇿ㄰-㆏]')
# Everything else is decided by which script carries the most *content*, which is
# not the same as the most characters: one Han character is a morpheme while one
# Latin word takes about five letters. Comparing raw counts would call
# "開放原始碼 open source" Latin, so a Chinese talk sprinkled with English terms
# would look like it changed language every other sentence. Each script's
# character count is therefore divided by roughly how many characters one of its
# words takes. Punctuation, digits and whitespace match nothing here on purpose —
# they say nothing about language, and letting them vote would make a short
# numeric fragment look like a script of its own.
_COUNTED_SCRIPTS = (
    ("han", re.compile(r'[一-鿿㐀-䶿豈-﫿]'), 1),
    ("latin", re.compile(r'[A-Za-zÀ-ɏ]'), 5),
    ("cyrillic", re.compile(r'[Ѐ-ӿ]'), 5),
    ("thai", re.compile(r'[฀-๿]'), 3),
    ("arabic", re.compile(r'[؀-ۿ]'), 4),
)


def dominant_script(text: str) -> str:
    """Return the script that carries this text: kana, hangul, han, latin,
    cyrillic, thai, arabic, or "other" when nothing identifiable is present.

    Coarse on purpose. It answers "did the speaker switch to a different writing
    system", not "which language is this" — same-script pairs (English/Spanish)
    are indistinguishable here and must not be guessed at.
    """
    if not text:
        return "other"
    if _KANA_RE.search(text):
        return "kana"
    if _HANGUL_RE.search(text):
        return "hangul"
    winner, best = "other", 0.0
    for name, pattern, chars_per_word in _COUNTED_SCRIPTS:
        score = len(pattern.findall(text)) / chars_per_word
        if score > best:
            winner, best = name, score
    return winner


def should_force_traditional(language_code: str | None) -> bool:
    """Whether this session's transcripts get normalised to Traditional Chinese.

    Scribe and the correction stage both have to make this call, on the same
    setting, so it is defined once here rather than restated in each. On auto
    detect the setting says nothing about what was spoken, so the deployment-wide
    FORCE_OPENCC decides — and to_taiwan_traditional still spares any line that
    turns out to be Japanese.
    """
    if language_code and language_code.startswith("zh"):
        return True
    return not language_code and bool(REALTIME_SETTINGS.get("FORCE_OPENCC", False))


def to_taiwan_traditional(text: str) -> str:
    """Convert to Traditional Chinese (Taiwan), leaving Japanese untouched."""
    # Kana is the only reliable tell. Han-only text stays convertible on purpose:
    # running s2tw over Chinese-looking text is close to harmless, while converting
    # real Japanese (対→對, 学→學, 発→發) is the damage this guards against.
    if _KANA_RE.search(text):
        return text
    return _cc_s2tw.convert(text)


def to_simplified(text: str) -> str:
    """Convert to Simplified Chinese."""
    return _cc_tw2s.convert(text)
