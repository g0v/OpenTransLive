# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
import asyncio
import json
import time
import unittest
from unittest import mock

from app import scribe_manager
from app.scribe_manager import SCRIBE_MANAGERS, _strip_forced_head

_WAIT = 0.05


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        await asyncio.Event().wait()

    async def close(self) -> None:
        pass


class ScribeForceCommitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for name, value in (("_FORCE_COMMIT_WAIT_SECS", _WAIT), ("_MIN_COMMIT_WORDS", 6)):
            patcher = mock.patch.object(scribe_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.out: list[dict] = []
        self.ws = _FakeSocket()
        self.m = SCRIBE_MANAGERS["elevenlabs"]("room", lambda _sid, t: self.out.append(t))
        self.m.is_running = True
        self.m.ws = self.ws
        self.m._last_audio_mono = time.monotonic()
        sender = asyncio.create_task(self.m.send_audio_loop())
        self.addCleanup(sender.cancel)
        self.addAsyncCleanup(self.m.stop)

    def _commits(self) -> list[str]:
        return [t["text"] for t in self.out if not t["partial"]]

    async def _wait_sent(self, count: int) -> None:
        async with asyncio.timeout(1):
            while len(self.ws.sent) < count:
                await asyncio.sleep(0.01)

    async def _force_and_let_provider_stay_silent(self) -> None:
        await self.m.handle_transcript("今天天氣很好", True)
        self.m.force_commit()
        await asyncio.sleep(_WAIT * 3)

    async def test_nothing_open_asks_nothing(self) -> None:
        self.m.force_commit()

        self.assertFalse(self.m.should_commit)
        self.assertIsNone(self.m._force_wait_task)
        self.assertEqual(self.out, [])

    async def test_provider_final_in_time_is_committed_however_short(self) -> None:
        await self.m.handle_transcript("天氣", True)
        # Audio still queued at the click carries the cut, not whatever comes next.
        await self.m.push_audio("AAAA")
        await self.m.push_audio("AAAA")
        self.m.force_commit()
        await self._wait_sent(2)
        self.assertEqual([f["commit"] for f in self.ws.sent], [False, True])
        self.assertEqual(self._commits(), [])

        await self.m.handle_transcript("天氣很好", False)
        self.assertEqual(self._commits(), ["天氣很好"])

        # The answer cancelled the fallback, and the next short final is held again.
        await asyncio.sleep(_WAIT * 3)
        await self.m.handle_transcript("好的", False)
        self.assertEqual(self._commits(), ["天氣很好"])
        self.assertTrue(self.out[-1]["partial"])
        self.assertEqual(self.out[-1]["text"], "好的")

    async def test_silent_provider_falls_back_to_the_partial(self) -> None:
        await self._force_and_let_provider_stay_silent()

        # The segment is committed: a flag no chunk has carried yet would cut the
        # provider's turn partway into the next sentence.
        self.assertFalse(self.m.should_commit)
        self.assertEqual(self._commits(), ["今天天氣很好"])
        self.assertEqual(self.m.last_partial_text, "")
        self.assertIsNone(self.m.seg_start_time)

    async def test_later_transcripts_drop_the_head_already_committed(self) -> None:
        await self._force_and_let_provider_stay_silent()

        await self.m.handle_transcript("今天天氣很好，我們出門", True)

        self.assertEqual(self.out[-1]["text"], "我們出門")
        self.assertTrue(self.out[-1]["partial"])

    async def test_late_final_is_held_not_committed_twice(self) -> None:
        await self._force_and_let_provider_stay_silent()
        await self.m.handle_transcript("今天天氣很好，我們出門", True)

        late = "今天天氣很好，我們出門去公園散步吧，大家一起來參加這個活動"
        await self.m.handle_transcript(late, False)
        self.assertEqual(self._commits(), ["今天天氣很好"])
        self.assertEqual(self.out[-1]["text"], "我們出門去公園散步吧，大家一起來參加這個活動")
        self.assertTrue(self.out[-1]["partial"])

        # That final closed the turn: the next one is taken whole, with the held text.
        await self.m.handle_transcript("今天天氣很好，我們下午三點在門口集合", False)
        commits = self._commits()
        self.assertEqual(len(commits), 2)
        self.assertTrue(commits[1].startswith("我們出門去公園"))
        self.assertTrue(commits[1].endswith("今天天氣很好，我們下午三點在門口集合"))

    async def test_buffered_short_commit_is_flushed_at_once(self) -> None:
        await self.m.handle_transcript("好的", False)
        timeout_task = self.m._commit_timeout_task

        self.m.force_commit()

        self.assertEqual(self._commits(), ["好的"])
        self.assertFalse(self.m.should_commit)
        self.assertIsNone(self.m._force_wait_task)
        await asyncio.sleep(0)
        self.assertTrue(timeout_task.cancelled())

    async def test_final_dropped_as_noise_still_answers_the_request(self) -> None:
        await self.m.handle_transcript("今天天氣很好", True)
        self.m.force_commit()

        await self.m.handle_transcript("。", False)  # the turn closed with nothing left
        await asyncio.sleep(_WAIT * 3)

        # The stale partial of that finished turn is not committed later, and the
        # next turn is not treated as repeating it.
        self.assertEqual(self._commits(), [])
        self.assertIsNone(self.m._forced_key)
        await self.m.handle_transcript("今天天氣很好，我們下午三點在門口集合", False)
        self.assertEqual(self._commits(), ["今天天氣很好，我們下午三點在門口集合"])

    async def test_flag_still_queued_when_the_provider_answers_is_not_sent(self) -> None:
        await self.m.handle_transcript("天氣", True)
        await self.m.push_audio("AAAA")
        await self.m.push_audio("AAAA")
        self.m.force_commit()
        # The provider's own pause detection answers before the flagged chunk is sent.
        await self.m.handle_transcript("天氣很好", False)
        await self._wait_sent(2)

        self.assertEqual(self._commits(), ["天氣很好"])
        self.assertEqual([f["commit"] for f in self.ws.sent], [False, False])


class StripForcedHeadTest(unittest.TestCase):
    def test_exact_prefix_ignores_punctuation_spacing_and_script(self) -> None:
        # The original text is kept from the cut on; CJK spacing is tidied away.
        self.assertEqual(_strip_forced_head("今天天气 很好， 我们 出门", "今天天氣很好"), "我们出门")

    def test_reworded_word_is_skipped_by_the_anchor(self) -> None:
        self.assertEqual(_strip_forced_head("大家早，今天天氣很好。出發", "大家好今天天氣很好"), "出發")

    def test_unrelated_transcript_is_left_alone(self) -> None:
        self.assertEqual(_strip_forced_head("我們出門吧", "今天天氣很好"), "我們出門吧")

    def test_latin_prefix(self) -> None:
        self.assertEqual(_strip_forced_head("Good morning, everyone here", "goodmorning"), "everyone here")

    def test_word_the_committed_text_cut_short_is_repeated_whole(self) -> None:
        self.assertEqual(_strip_forced_head("I'll see you tomorrow at noon", "illseeyouto"),
                         "tomorrow at noon")
        self.assertEqual(_strip_forced_head("I don't know", "idon"), "don't know")
        # The cut word may be the first one, or follow punctuation instead of a space.
        self.assertEqual(_strip_forced_head("Tomorrow at noon", "to"), "Tomorrow at noon")
        self.assertEqual(_strip_forced_head("OK,tomorrow then", "okto"), "tomorrow then")

    def test_script_without_spaces_keeps_the_cut(self) -> None:
        self.assertEqual(_strip_forced_head("สวัสดีครับ", "สวัสดี"), "ครับ")

    def test_anchor_matches_near_the_committed_end_not_a_later_repeat(self) -> None:
        self.assertEqual(
            _strip_forced_head("Good mornin everyone, I said good morning everyone",
                               "goodmorningeveryone"),
            "I said good morning everyone")

    def test_korean_keeps_its_spaces(self) -> None:
        self.assertEqual(_strip_forced_head("안녕하세요, 여러분 반갑습니다", "안녕하세요"),
                         "여러분 반갑습니다")


if __name__ == "__main__":
    unittest.main()
