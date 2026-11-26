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

# Dual-script characters in OpenCC's STCharacters.ocd2 whose value list includes
# themselves, just not as the first (default) entry. Any Han character outside this
# set that STCharacters rewrites cannot occur in Traditional text, so its presence
# tells us the input actually is Simplified and needs the full character table.
_DUAL_SCRIPT_ST_CHARS = (
    "㐹万丑丰于云亘仆价伙余佣党准几凶划占厂厘台叶吁吃后咨咸唇夸尸岩岳帘干广征愿托挂据斗朴杠杰极柜涂涌淀游熏皂确种筑胜腊腌膻苹范荐虫蚝蜡蝎跖辟适郁采里"
)
_DROP_DUAL_SCRIPT = str.maketrans("", "", _DUAL_SCRIPT_ST_CHARS)


def _build_converters() -> tuple[OpenCC, OpenCC, OpenCC]:
    """Build s2tw (with _INHERITED_CHARS pinned), phrase-only s2tw, and STCharacters.

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

    st_phrases = {"type": "ocd2", "file": os.path.join(data_dir, 'STPhrases.ocd2')}
    st_chars = {"type": "ocd2", "file": os.path.join(data_dir, 'STCharacters.ocd2')}
    tw_variants = {"type": "ocd2", "file": os.path.join(data_dir, 'TWVariants.ocd2')}

    # Mirrors the stock s2tw.json, with the pinned characters inserted between the
    # phrase table and the single-character table.
    s2tw_config = os.path.join(out_dir, 's2tw_inherited.json')
    with open(s2tw_config, 'w', encoding='utf-8') as f:
        json.dump({
            "name": "Simplified to Traditional (Taiwan), keeping inherited characters",
            "segmentation": {"type": "mmseg", "dict": st_phrases},
            "conversion_chain": [
                {"dict": {"type": "group", "dicts": [
                    st_phrases,
                    {"type": "text", "file": inherited},
                    st_chars,
                ]}},
                {"dict": tw_variants},
            ],
        }, f)

    # Phrase table + Taiwan variants only, for text that is already Traditional.
    phrases_config = os.path.join(out_dir, 's2tw_phrases.json')
    with open(phrases_config, 'w', encoding='utf-8') as f:
        json.dump({
            "name": "Simplified phrases to Traditional (Taiwan), without STCharacters",
            "segmentation": {"type": "mmseg", "dict": st_phrases},
            "conversion_chain": [
                {"dict": st_phrases},
                {"dict": tw_variants},
            ],
        }, f)

    st_chars_config = os.path.join(out_dir, 'st_characters.json')
    with open(st_chars_config, 'w', encoding='utf-8') as f:
        json.dump({
            "name": "Single-character Simplified to Traditional check",
            "segmentation": {"type": "mmseg", "dict": st_chars},
            "conversion_chain": [{"dict": st_chars}],
        }, f)

    return OpenCC(s2tw_config), OpenCC(phrases_config), OpenCC(st_chars_config)


_cc_s2tw, _cc_s2tw_phrases, _cc_st_chars = _build_converters()
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


def _has_simplified_only(text: str) -> bool:
    """Whether text contains a character STCharacters never maps to itself."""
    if not text or max(text) < "\u2e80":
        return False
    probe = text.translate(_DROP_DUAL_SCRIPT)
    return bool(probe) and _cc_st_chars.convert(probe) != probe


def to_taiwan_traditional(text: str) -> str:
    """Convert to Traditional Chinese (Taiwan), leaving Japanese and pure Traditional untouched."""
    # Kana is the only reliable tell for Japanese. Text without any Simplified-only
    # character skips STCharacters so already-Traditional characters (游, 朴, 征, 斗,
    # 台) aren't rewritten while STPhrases (干活→幹活) and TWVariants (爲→為) still apply.
    if not text or _KANA_RE.search(text):
        return text
    converter = _cc_s2tw if _has_simplified_only(text) else _cc_s2tw_phrases
    return converter.convert(text)


def ends_sentence(text: str) -> bool:
    """Whether text ends on sentence-final punctuation ('。', '！', '？', '!', '?', or a single '.')."""
    s = text.strip()
    if not s:
        return False
    if s[-1] == ".":
        return len(s) < 2 or s[-2] != "."
    return s[-1] in "。！？!?"


def clean_transcript(text: str) -> str:
    """Trim whitespace and trailing clause/hesitation punctuation while keeping a
    sentence-final '。' or single '.'. Empty result means no spoken content."""
    s = text.strip()
    body = s.rstrip(",.。，—- ").strip()
    if not body:
        return ""
    tail = s[len(body):].strip()
    if tail.endswith("。"):
        return body + "。"
    if tail.endswith(".") and not tail[:-1].endswith("."):
        return body + "."
    return body


def to_simplified(text: str) -> str:
    """Convert to Simplified Chinese."""
    return _cc_tw2s.convert(text)
