# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import app
from app import scribe_manager
from app.scribe_manager import SCRIBE_MANAGERS, same_stt_language_codes


class ScribeModelSwitchTest(unittest.IsolatedAsyncioTestCase):
    def test_elevenlabs_defaults_to_v3_and_v2_uses_v2_realtime(self) -> None:
        v3 = SCRIBE_MANAGERS["elevenlabs"]("room", lambda *_a: None)
        v2 = SCRIBE_MANAGERS["elevenlabs-v2"]("room", lambda *_a: None)
        q3 = parse_qs(urlparse(v3._connect_target()[0]).query)
        q2 = parse_qs(urlparse(v2._connect_target()[0]).query)
        self.assertEqual(q3["model_id"], ["scribe_v3_live"])
        self.assertEqual(q2["model_id"], ["scribe_v2_realtime"])

    def test_same_stt_language_codes(self) -> None:
        self.assertTrue(same_stt_language_codes("elevenlabs", "elevenlabs-v2"))
        self.assertTrue(same_stt_language_codes("elevenlabs-v2", "elevenlabs"))
        self.assertFalse(same_stt_language_codes("elevenlabs", "gemini"))

    async def test_switching_between_scribe_models_keeps_detect_language(self) -> None:
        saved_lang = AsyncMock()
        saved_prov = AsyncMock()

        def make_req(provider: str):
            return SimpleNamespace(json=AsyncMock(return_value={"provider": provider}))

        with (
            patch.object(app, "_require_session_owner", AsyncMock(return_value=("u@e.com", {}))),
            patch.object(app, "_emit_session_settings_update", AsyncMock()),
            patch.dict(app.active_scribe_managers, {}, clear=True),
            patch("app.translation_service.get_session_stt_provider", AsyncMock(return_value="elevenlabs")),
            patch("app.translation_service.save_session_stt_provider", saved_prov),
            patch("app.translation_service.save_session_scribe_language", saved_lang),
        ):
            await app.update_session_stt_provider_endpoint(make_req("elevenlabs-v2"), "room1")
            saved_prov.assert_awaited_once_with(app.redis_client, "room1", "elevenlabs-v2")
            saved_lang.assert_not_awaited()

            await app.update_session_stt_provider_endpoint(make_req("gemini"), "room1")
            saved_lang.assert_awaited_once_with(app.redis_client, "room1", "")


class CapCutAndJoinTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for name, value in (
            ("_MAX_PARTIAL_TOKENS", 20),
            ("_MAX_SEGMENT_SECS", 25.0),
            ("_MIN_COMMIT_WORDS", 6),
            ("_COMMIT_TIMEOUT_SECS", 0.05),
        ):
            patcher = patch.object(scribe_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.out: list[dict] = []
        self.m = SCRIBE_MANAGERS["elevenlabs"]("room", lambda _sid, t: self.out.append(t))
        self.addAsyncCleanup(self.m.stop)

    async def test_cap_cut_commits_at_next_clause_end_and_keeps_tail_open(self) -> None:
        long_prefix = "今天我們來介紹這個開源即時翻譯系統的主要架構設計"
        await self.m.handle_transcript(long_prefix, True)
        self.assertTrue(self.m._cap_wait)
        self.assertFalse(self.m.should_commit)

        # Next partial adds a clause comma then keeps going: head commits through the
        # comma and tail opens as a new partial without closing the provider turn.
        await self.m.handle_transcript(long_prefix + "，接下來看示範", True)
        commits = [t["text"] for t in self.out if not t["partial"]]
        self.assertEqual(commits, [long_prefix])
        self.assertTrue(self.out[-1]["partial"])
        self.assertEqual(self.out[-1]["text"], "接下來看示範")
        self.assertIsNotNone(self.m._forced_key)

        # When the provider finally closes the turn at a sentence end, the committed
        # head is stripped and the remainder commits cleanly.
        await self.m.handle_transcript(
            long_prefix + "，接下來看示範怎麼在活動現場設定麥克風與字幕。",
            False,
        )
        commits = [t["text"] for t in self.out if not t["partial"]]
        self.assertEqual(commits[-1], "接下來看示範怎麼在活動現場設定麥克風與字幕。")
        self.assertIsNone(self.m._forced_key)
        self.assertIsNone(self.m.seg_start_time)

    async def test_cjk_short_commits_join_without_ascii_space(self) -> None:
        await self.m.handle_transcript("你好", False)
        await self.m.handle_transcript("世界我們一起來參加活動吧", False)
        commits = [t["text"] for t in self.out if not t["partial"]]
        self.assertEqual(commits, ["你好世界我們一起來參加活動吧"])

    async def test_commit_timeout_does_not_flush_while_partial_follows(self) -> None:
        await self.m.handle_transcript("你好", False)
        await self.m.handle_transcript("接下來", True)
        await asyncio.sleep(0.1)
        commits = [t["text"] for t in self.out if not t["partial"]]
        self.assertEqual(commits, [])
        await self.m.handle_transcript("接下來繼續往下介紹今天的議程。", False)
        commits = [t["text"] for t in self.out if not t["partial"]]
        self.assertEqual(commits, ["你好接下來繼續往下介紹今天的議程。"])

    async def test_unpunctuated_runaway_partial_triggers_hard_commit(self) -> None:
        # Above _MAX_PARTIAL_TOKENS (20), first waits for a clause boundary.
        prefix = "今天我們來介紹這個開源即時翻譯系統的主要架構設計"
        await self.m.handle_transcript(prefix, True)
        self.assertTrue(self.m._cap_wait)
        self.assertFalse(self.m.should_commit)

        # Past 2 * _MAX_PARTIAL_TOKENS without any punctuation, triggers hard commit.
        runaway = prefix + "以及活動現場的多語言字幕即時推流與校正流程示範說明"
        await self.m.handle_transcript(runaway, True)
        self.assertFalse(self.m._cap_wait)
        self.assertTrue(self.m.should_commit)


if __name__ == "__main__":
    unittest.main()
