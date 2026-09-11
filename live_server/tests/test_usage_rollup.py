import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
from app import usage_backfill as app_backfill

_ONE_SECOND_CHUNK = base64.b64encode(b"\x00" * 32000).decode()  # 16kHz 16-bit mono
# What push_audio() actually counts for that chunk (base64: 4 chars per 3 bytes).
_CHUNK_BYTES = len(_ONE_SECOND_CHUNK) * 3 // 4


def _manager(sid: str = "room-a"):
    manager = app.SCRIBE_MANAGERS["elevenlabs"](sid, lambda *_args: None)
    manager.is_running = True
    return manager


class _AsyncDocs:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc
        return gen()


class _UsageCollection:
    """Stands in for usage_daily_collection: records writes, replays reads."""

    def __init__(self, docs=()):
        self.docs = list(docs)
        self.queries: list[dict] = []
        self.writes: list[tuple] = []

    def find(self, query, projection=None):
        self.queries.append(query)
        return _AsyncDocs(self.docs)

    async def update_one(self, filt, update, upsert=False):
        self.writes.append((filt, update, upsert))
        return SimpleNamespace(upserted_id=len(self.writes) if upsert else None)


class UsageWatermarkTest(unittest.IsolatedAsyncioTestCase):
    """The rollup stores increments, so audio must be billed exactly once."""

    async def test_each_chunk_is_billed_once(self):
        manager = _manager()
        for _ in range(5):
            await manager.push_audio(_ONE_SECOND_CHUNK)

        first = manager.flush_usage_delta()
        self.assertEqual(first["audio_bytes"], 5 * _CHUNK_BYTES)
        self.assertEqual(first["audio_chunks"], 5)
        self.assertIsNone(manager.flush_usage_delta())

        await manager.push_audio(_ONE_SECOND_CHUNK)
        second = manager.flush_usage_delta()
        self.assertEqual(second["audio_bytes"], _CHUNK_BYTES)
        self.assertEqual(second["audio_chunks"], 1)

    async def test_restored_audio_is_not_billed_again(self):
        manager = _manager()
        for _ in range(4):
            await manager.push_audio(_ONE_SECOND_CHUNK)
        persisted = manager.get_usage_stats()
        manager.flush_usage_delta()

        # A page refresh builds a new manager and restores the room's counters.
        restarted = _manager()
        restarted.restore_usage(persisted["audio_bytes"], persisted["audio_chunks"])
        self.assertIsNone(restarted.flush_usage_delta())

        await restarted.push_audio(_ONE_SECOND_CHUNK)
        self.assertEqual(restarted.flush_usage_delta()["audio_bytes"], _CHUNK_BYTES)
        self.assertEqual(restarted.get_usage_stats()["audio_duration_secs"], 5.0)

    async def test_rolled_back_delta_is_retried_with_later_audio(self):
        manager = _manager()
        await manager.push_audio(_ONE_SECOND_CHUNK)
        failed = manager.flush_usage_delta()

        # Audio keeps arriving while the failed write was in flight; the retry
        # must cover both the un-reserved delta and the newer bytes.
        await manager.push_audio(_ONE_SECOND_CHUNK)
        manager.rollback_usage_delta(failed)
        retry = manager.flush_usage_delta()
        self.assertEqual(retry["audio_bytes"], 2 * _CHUNK_BYTES)
        self.assertEqual(retry["audio_chunks"], 2)
        self.assertIsNone(manager.flush_usage_delta())


class HeartbeatUsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_bills_the_room_owner_not_the_lock_holder(self):
        sid = "room-a"
        room = {"sid": sid, "admin_email": "owner@example.com"}
        manager = _manager(sid)
        for _ in range(3):
            await manager.push_audio(_ONE_SECOND_CHUNK)
        usage = _UsageCollection()
        rooms = AsyncMock()

        with (
            patch.object(app, "_verify_session_lock_holder", AsyncMock(return_value=room)),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=2)),
            patch.object(app, "usage_daily_collection", usage),
            patch.object(app, "rooms_collection", rooms),
            patch.dict(app.active_scribe_managers, {sid: manager}, clear=True),
            patch.dict(app.active_translation_managers, {}, clear=True),
        ):
            response = await app.heartbeat(object(), sid)
            # A second heartbeat with no new audio must not write again.
            await app.heartbeat(object(), sid)

        self.assertEqual(response["audio_duration_secs"], 3.0)
        self.assertEqual(len(usage.writes), 1)
        filt, update, upsert = usage.writes[0]
        self.assertTrue(upsert)
        self.assertEqual(filt["email"], "owner@example.com")
        self.assertEqual(filt["sid"], sid)
        self.assertEqual(filt["day"], app._usage_day(datetime.now(timezone.utc)))
        self.assertEqual(update["$inc"]["audio_bytes"], 3 * _CHUNK_BYTES)
        self.assertEqual(update["$inc"]["audio_chunks"], 3)

    async def test_failed_rollup_is_billed_by_the_next_heartbeat(self):
        sid = "room-a"
        manager = _manager(sid)
        await manager.push_audio(_ONE_SECOND_CHUNK)
        usage = _UsageCollection()
        usage.update_one = AsyncMock(side_effect=RuntimeError("mongo down"))

        with (
            patch.object(app, "_verify_session_lock_holder",
                         AsyncMock(return_value={"sid": sid, "admin_email": "owner@example.com"})),
            patch.object(app, "_viewer_presence_op", AsyncMock(return_value=0)),
            patch.object(app, "usage_daily_collection", usage),
            patch.object(app, "rooms_collection", AsyncMock()),
            patch.dict(app.active_scribe_managers, {sid: manager}, clear=True),
            patch.dict(app.active_translation_managers, {}, clear=True),
        ):
            # The lock refresh the panel depends on must survive the write failure.
            response = await app.heartbeat(object(), sid)
            self.assertEqual(response["status"], "ok")

            # Mongo recovers; the dropped audio must not be silently forgiven.
            usage.update_one = _UsageCollection.update_one.__get__(usage)
            await manager.push_audio(_ONE_SECOND_CHUNK)
            await app.heartbeat(object(), sid)

        self.assertEqual(len(usage.writes), 1)
        self.assertEqual(usage.writes[0][1]["$inc"]["audio_bytes"], 2 * _CHUNK_BYTES)
        self.assertEqual(usage.writes[0][1]["$inc"]["audio_chunks"], 2)

    async def test_releasing_a_session_bills_the_outgoing_owner_and_drops_the_manager(self):
        sid = "room-a"
        room = {"sid": sid, "admin_email": "owner@example.com"}
        manager = _manager(sid)
        await manager.push_audio(_ONE_SECOND_CHUNK)
        usage = _UsageCollection()

        with (
            patch.object(app, "_require_session_primary_owner",
                         AsyncMock(return_value=("owner@example.com", room))),
            patch.object(app, "usage_daily_collection", usage),
            patch.object(app, "rooms_collection", AsyncMock()),
            patch.object(manager, "stop", AsyncMock()),
            patch.dict(app.active_scribe_managers, {sid: manager}, clear=True),
        ):
            await app.delete_session(object(), sid)
            # The next claimant must start from an empty manager slot.
            self.assertNotIn(sid, app.active_scribe_managers)

        self.assertEqual(len(usage.writes), 1)
        filt, update, _ = usage.writes[0]
        self.assertEqual(filt["email"], "owner@example.com")
        self.assertEqual(update["$inc"]["audio_bytes"], _CHUNK_BYTES)


class UsageReportTest(unittest.IsolatedAsyncioTestCase):
    async def test_report_fills_empty_buckets_and_counts_distinct_rooms(self):
        now = datetime.now(timezone.utc)
        today = app._usage_day(now)
        yesterday = app._usage_day(now - timedelta(days=1))
        old = app._usage_day(now - timedelta(days=40))  # outside the daily window
        secs = app.AUDIO_BYTES_PER_SEC
        usage = _UsageCollection([
            {"day": today, "sid": "room-a", "audio_bytes": 60 * secs},
            {"day": today, "sid": "room-b", "audio_bytes": 30 * secs},
            {"day": yesterday, "sid": "room-a", "audio_bytes": 10 * secs},
            {"day": old, "sid": "room-c", "audio_bytes": 90 * secs},
        ])

        with patch.object(app, "usage_daily_collection", usage):
            report = await app._usage_report("owner@example.com", 7, 3)

        self.assertEqual(len(report["daily"]), 7)
        self.assertEqual(len(report["monthly"]), 3)
        self.assertEqual(report["daily"][-1], {"day": today, "audio_secs": 90.0, "session_count": 2})
        self.assertEqual(report["daily"][-2],
                         {"day": yesterday, "audio_secs": 10.0, "session_count": 1})
        # Days with no usage are still present, so the chart has no gaps.
        self.assertEqual([d["audio_secs"] for d in report["daily"][:5]], [0.0] * 5)
        self.assertEqual(report["totals"]["today_secs"], 90.0)
        self.assertEqual(report["totals"]["range_secs"], 100.0)
        self.assertEqual(report["totals"]["range_sessions"], 2)
        # The 40-day-old row is outside the daily window but inside the monthly one.
        self.assertEqual(sum(m["audio_secs"] for m in report["monthly"]), 190.0)
        self.assertEqual(
            report["rooms"],
            [{"sid": "room-a", "audio_secs": 70.0, "active_days": 2, "last_day": today},
             {"sid": "room-b", "audio_secs": 30.0, "active_days": 1, "last_day": today}],
        )
        self.assertEqual(usage.queries[0]["email"], "owner@example.com")

    async def test_sub_second_increments_sum_before_rounding(self):
        """Per-row rounding would report 0s for two rows a room actually used."""
        today = app._usage_day(datetime.now(timezone.utc))
        usage = _UsageCollection([
            {"day": today, "sid": "room-a", "audio_bytes": 1599},
            {"day": today, "sid": "room-a", "audio_bytes": 1599},
        ])

        with patch.object(app, "usage_daily_collection", usage):
            report = await app._usage_report("owner@example.com", 1, 1)

        self.assertEqual(report["totals"]["today_secs"], 0.1)
        self.assertEqual(report["rooms"][0]["audio_secs"], 0.1)

    async def test_monthly_window_widens_the_scan(self):
        now = datetime.now(timezone.utc)
        usage = _UsageCollection()

        with patch.object(app, "usage_daily_collection", usage):
            await app._usage_report("owner@example.com", 7, 12)

        first_month = app._usage_month_keys(now, 12)[0]
        self.assertEqual(usage.queries[0]["day"]["$gte"], f"{first_month}-01")


class UsageBackfillTest(unittest.IsolatedAsyncioTestCase):
    """Historical usage is reconstructed from the cumulative [audio_usage] log lines."""

    def _write_log(self, name: str, lines: list[str]) -> None:
        (self.logs / name).write_text("".join(
            f"{ts} - app.scribe_manager - INFO - push_audio:184 - [audio_usage] "
            f"session={sid} bytes={raw} duration=0.0s chunks={chunks}\n"
            for ts, sid, raw, chunks in (line.split("|") for line in lines)
        ), encoding="utf-8")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_cumulative_snapshots_become_per_utc_day_deltas(self):
        # Log timestamps are Taiwan time, so 06:30 on the 3rd is still the 2nd in UTC.
        self._write_log("opentranslive_20260602.log", [
            "2026-06-02 23:00:00|room-a|1000|10",
            "2026-06-02 23:30:00|room-a|2500|25",
            "2026-06-03 06:30:00|room-a|4000|40",   # 2026-06-02 22:30 UTC
            "2026-06-03 09:00:00|room-a|4500|45",   # 2026-06-03 01:00 UTC
        ])
        snapshots = app_backfill._parse_snapshots(self.logs)
        self.assertEqual(
            app_backfill._deltas_by_day(snapshots["room-a"]),
            {"2026-06-02": (4000, 40), "2026-06-03": (500, 5)},
        )

    def test_counter_restart_is_read_as_a_delta_not_a_total(self):
        self._write_log("opentranslive_20260602.log", [
            "2026-06-02 12:00:00|room-a|5000|50",
            "2026-06-02 13:00:00|room-a|900|9",   # fresh manager, counting from zero
            "2026-06-02 13:30:00|room-a|1800|18",
        ])
        snapshots = app_backfill._parse_snapshots(self.logs)
        self.assertEqual(app_backfill._deltas_by_day(snapshots["room-a"]),
                         {"2026-06-02": (6800, 68)})

    async def test_apply_never_overwrites_a_live_row(self):
        self._write_log("opentranslive_20260602.log", [
            "2026-06-02 12:00:00|room-a|3200|32",
        ])
        usage = _UsageCollection()
        rooms = _AsyncDocs([{"sid": "room-a", "admin_email": "owner@example.com",
                             "audio_bytes": 3200}])

        with (
            patch.object(app_backfill, "usage_daily_collection", usage),
            patch.object(app_backfill, "rooms_collection",
                         type("_Rooms", (), {"find": lambda *_a, **_k: rooms})()),
        ):
            await app_backfill.backfill(self.logs, apply=True, residual=False)

        self.assertEqual(len(usage.writes), 1)
        filt, update, upsert = usage.writes[0]
        self.assertTrue(upsert)
        self.assertEqual(filt, {"email": "owner@example.com", "day": "2026-06-02",
                                "sid": "room-a"})
        # $setOnInsert only: a day the heartbeat already recorded must not be replaced.
        self.assertEqual(set(update), {"$setOnInsert"})
        self.assertEqual(update["$setOnInsert"]["audio_bytes"], 3200)
        self.assertEqual(update["$setOnInsert"]["source"], "log_backfill")

    async def test_second_run_writes_nothing_and_books_no_residual(self):
        """Re-running after a deploy must not re-bill what is already recorded."""
        self._write_log("opentranslive_20260602.log", [
            "2026-06-02 12:00:00|room-a|3200|32",
        ])
        # State left by the first run (or by the live heartbeat).
        usage = _UsageCollection([{"email": "owner@example.com", "day": "2026-06-02",
                                   "sid": "room-a", "audio_bytes": 3200}])
        rooms = _AsyncDocs([{"sid": "room-a", "admin_email": "owner@example.com",
                             "audio_bytes": 3200,
                             "updated_at": datetime(2026, 6, 5, tzinfo=timezone.utc)}])

        with (
            patch.object(app_backfill, "usage_daily_collection", usage),
            patch.object(app_backfill, "rooms_collection",
                         type("_Rooms", (), {"find": lambda *_a, **_k: rooms})()),
        ):
            await app_backfill.backfill(self.logs, apply=True, residual=True)

        self.assertEqual(usage.writes, [])


if __name__ == "__main__":
    unittest.main()
