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
import json
import os
import re
import tempfile
import unicodedata

import opencc
from opencc import OpenCC

from .config import REALTIME_SETTINGS

# Characters that are current Taiwanese Traditional in their own right, but that
# OpenCC lists late among the several Traditional forms one Simplified character can
# take. Whenever no phrase rule covers the word, the single-character rule picks that
# first form and rewrites text that was already correct: 干擾 → 幹擾, 里長 → 裡長,
# 姓范 → 姓範. s2tw assumes its input really is Simplified, and both scribe and the
# LLMs hand us Traditional often enough that this fires constantly.
#
# The fix is the one OpenCC's maintainers recommend for this (BYVoid/OpenCC#165):
# convert these characters to themselves, ahead of the single-character table. The
# phrase table is still consulted first — mmseg takes the longest match — so genuinely
# Simplified 干扰/干活/主干/这里/准备 still become 干擾/幹活/主幹/這裡/準備.
#
# A character earns its place here only if it is common in Taiwanese Traditional and
# its Simplified uses are covered by the phrase table. 后 and 于 are deliberately left
# out: 然后/由于 chopped into a partial leave a bare Simplified character behind too
# often to be worth it. 游, 征, 占 and 采 are left out because the phrase table already
# gets them right, so listing them would add risk and fix nothing.
_INHERITED_CHARS = "干里范岳准托丑咸"


def _build_s2tw() -> OpenCC:
    """s2tw with _INHERITED_CHARS pinned to themselves.

    OpenCC only takes a config as a file on disk, and it resolves the dictionaries a
    config names relative to the process, not to the config — so both files have to be
    written out with absolute paths rather than shipped in the repo.
    """
    data_dir = os.path.join(os.path.dirname(opencc.__file__), 'clib', 'share', 'opencc')
    out_dir = os.path.join(tempfile.gettempdir(), 'opentranslive-opencc')
    os.makedirs(out_dir, exist_ok=True)

    inherited = os.path.join(out_dir, 'Inherited.txt')
    with open(inherited, 'w', encoding='utf-8') as f:
        f.writelines(f"{c}\t{c}\n" for c in _INHERITED_CHARS)

    # Mirrors the stock s2tw.json, with the pinned characters inserted between the
    # phrase table and the single-character table.
    config = os.path.join(out_dir, 's2tw_inherited.json')
    with open(config, 'w', encoding='utf-8') as f:
        json.dump({
            "name": "Simplified to Traditional (Taiwan), keeping inherited characters",
            "segmentation": {
                "type": "mmseg",
                "dict": {"type": "ocd2", "file": os.path.join(data_dir, 'STPhrases.ocd2')},
            },
            "conversion_chain": [
                {"dict": {"type": "group", "dicts": [
                    {"type": "ocd2", "file": os.path.join(data_dir, 'STPhrases.ocd2')},
                    {"type": "text", "file": inherited},
                    {"type": "ocd2", "file": os.path.join(data_dir, 'STCharacters.ocd2')},
                ]}},
                {"dict": {"type": "ocd2", "file": os.path.join(data_dir, 'TWVariants.ocd2')}},
            ],
        }, f)
    return OpenCC(config)


_cc_s2tw = _build_s2tw()
_cc_tw2s = OpenCC('tw2s')

# Hiragana + katakana + halfwidth katakana. Checked first in dominant_script:
# Japanese mixes kana with Han, so counting Han first would call every Japanese
# line Chinese.
_KANA_RE = re.compile(r'[぀-ヿｦ-ﾝ]')
# Syllables + conjoining jamo + compatibility jamo. Checked second for the same
# reason: Korean can carry Han hanja.
_HANGUL_RE = re.compile(r'[가-힣ᄀ-ᇿ㄰-㆏]')
# Roughly how many characters one word of each script takes. Character counts are
# not comparable across scripts — one Han character is a morpheme while one Latin
# word takes about five letters — so everything that wants "how much was said"
# divides by these instead of counting characters or tokens. Punctuation, digits
# and whitespace match nothing here on purpose: they say nothing about language,
# and letting them vote would make a short numeric fragment look like a script of
# its own.
_CHARS_PER_WORD = (
    ("kana", _KANA_RE, 2),
    ("hangul", _HANGUL_RE, 3),
    # Ext A, URO, then the compatibility ideographs. That last range starts at
    # U+F900, a character visually identical to the ordinary U+8C48 — this range was
    # written with the U+8C48 one, which silently widened it to U+8C48-U+FAFF and
    # swallowed all of Hangul and Yi. Keep the endpoints escaped so they stay legible.
    ("han", re.compile('[\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff]'), 1),
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
    # The two checks above already returned if any kana or hangul was present, so
    # those entries necessarily score 0 here and cannot win — no need to skip them.
    winner, best = "other", 0.0
    for name, pattern, chars_per_word in _CHARS_PER_WORD:
        score = len(pattern.findall(text)) / chars_per_word
        if score > best:
            winner, best = name, score
    return winner


def approx_word_count(text: str) -> float:
    """Roughly how many words this text carries, on a scale that means the same
    thing in every script.

    "好" and "Okay" both count as about one word, where a character count would
    make them differ fivefold and a token count about fourfold. Callers that need
    to ask "was enough said here" use this; token counts are for questions that
    really are about tokens, such as prompt size.

    Coarse on purpose, and it over-counts mixed Japanese slightly because kanji
    and the kana inflecting them are each counted (食べる scores ~2). Nothing here
    needs better than that.
    """
    # Decomposed Hangul is two or three conjoining jamo per syllable, all of which
    # _HANGUL_RE matches, so counting without composing first triples Korean.
    text = unicodedata.normalize("NFC", text)
    return sum(len(pattern.findall(text)) / chars_per_word
               for _, pattern, chars_per_word in _CHARS_PER_WORD)


# The primary subtag each provider uses for Mandarin: "zho" (ElevenLabs, ISO 639),
# "cmn" (Gemini, BCP-47), and the plain "zh". Cantonese ("yue") is deliberately
# absent: yue-Hant-HK is already Traditional, and s2tw would rewrite its Hong Kong
# variants into Taiwanese ones.
_MANDARIN_SUBTAGS = frozenset({"zh", "zho", "cmn"})


def should_force_traditional(language_code: str | None) -> bool:
    """Whether this session's transcripts get normalised to Traditional Chinese.

    Scribe and the correction stage both have to make this call, on the same
    setting, so it is defined once here rather than restated in each. On auto
    detect the setting says nothing about what was spoken, so the deployment-wide
    FORCE_OPENCC decides — and to_taiwan_traditional still spares any line that
    turns out to be Japanese.

    It stays on for a code that already transcribes as Traditional (cmn-Hant-TW):
    exempting it would make the output script depend on which Mandarin locale was
    picked, and s2tw still catches the stray Simplified character.
    """
    if language_code and language_code.lower().split("-")[0] in _MANDARIN_SUBTAGS:
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
