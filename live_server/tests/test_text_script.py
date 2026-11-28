# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import unittest

from app.text_script import clean_transcript, ends_sentence, to_taiwan_traditional


class ToTaiwanTraditionalTest(unittest.TestCase):
    def test_preserves_already_traditional_and_converts_simplified(self) -> None:
        cases = [
            # Already-Traditional pinned characters are not rewritten.
            ("干擾", "干擾"),
            ("里長", "里長"),
            ("姓范", "姓范"),
            ("岳父", "岳父"),
            ("准許", "准許"),
            # Already-Traditional text keeps single characters s2tw would rewrite,
            # and still gets the Taiwan variants.
            ("姓游的同學去游泳", "姓游的同學去游泳"),
            ("樸素的朴先生", "樸素的朴先生"),
            ("長征與煙斗", "長征與煙斗"),
            ("台灣與臺灣", "台灣與臺灣"),
            ("爲什麼", "為什麼"),
            # Real Simplified phrases still convert through the phrase table.
            ("干扰", "干擾"),
            ("干活", "幹活"),
            ("主干", "主幹"),
            ("这里", "這裡"),
            ("后来我们去台北", "後來我們去臺北"),
            ("皇后的后面", "皇后的後面"),
            ("一只猫", "一隻貓"),
            ("面条和面包", "麵條和麵包"),
            ("咸丰年间", "咸豐年間"),
            ("准备", "準備"),
            ("丑时", "丑時"),
            # The adverb 只 ("only") stays 只 after a demonstrative or 是/有,
            # while the measure word still becomes 隻.
            ("这只有我们知道", "這只有我們知道"),
            ("还是只有一个", "還是只有一個"),
            ("那只会更麻烦", "那只會更麻煩"),
            ("还是只会说", "還是只會說"),
            ("这只是开始", "這只是開始"),
            ("還是只有一個", "還是只有一個"),
            ("那只會更麻煩", "那只會更麻煩"),
            ("還是只會說", "還是只會說"),
            ("这只猫", "這隻貓"),
            ("一只有毒的蛇", "一隻有毒的蛇"),
            ("这只鸟在叫", "這隻鳥在叫"),
            # Kana anywhere protects Japanese from conversion.
            ("これは学校です", "これは学校です"),
            ("対応する", "対応する"),
            ("", ""),
            ("hello 123", "hello 123"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(to_taiwan_traditional(raw), expected)


class CleanTranscriptTest(unittest.TestCase):
    def test_keeps_sentence_final_periods_and_strips_clause_punctuation(self) -> None:
        cases = [
            ("  你好。 ", "你好。"),
            ("你好，。", "你好。"),
            ("我們今天，", "我們今天"),
            ("That is all.", "That is all."),
            ("hello...", "hello"),
            ("hello, .", "hello."),
            ("...ok...", "...ok"),
            ("。", ""),
            ("  ", ""),
            ("wait!", "wait!"),
            ("有時候——", "有時候"),
            ("415-555-", "415-555"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(clean_transcript(raw), expected)

    def test_ends_sentence(self) -> None:
        self.assertTrue(ends_sentence("你好。"))
        self.assertTrue(ends_sentence("Hello."))
        self.assertTrue(ends_sentence("Wait! "))
        self.assertTrue(ends_sentence("Really?"))
        self.assertFalse(ends_sentence("hello..."))
        self.assertFalse(ends_sentence("我們今天，"))
        self.assertFalse(ends_sentence(""))


if __name__ == "__main__":
    unittest.main()
