# This file is part of g0v/OpenTransLive.
# Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
# Licensed under the GNU AGPL v3.0
# See LICENSE for details.
"""Rebuild `usage_daily` history from the server logs.

The per-day rollup only starts accumulating when the heartbeat writes it, so a
deployment that adds usage reporting starts with empty charts even though the
room documents hold large cumulative counters. The counters themselves carry no
date, but `push_audio` has always logged a cumulative snapshot every 30s of
audio:

    2026-09-11 01:02:04 - app.scribe_manager - INFO - push_audio:184 -
    [audio_usage] session=<sid> bytes=966774 duration=30.2s chunks=118

Those lines are timestamped, so they reconstruct per-day usage: walk one
session's snapshots in order and the difference between consecutive cumulative
values is the audio that arrived in between. A snapshot smaller than its
predecessor means a fresh manager started counting from zero, so that value is
itself the delta.

Limits, by construction:

* Granularity is one log line, i.e. 30s of audio. Whatever a session streamed
  after its last snapshot is missing — see the `residual` report, and
  `--residual` to book it against the room's last activity day.
* Days whose log file has been rotated away cannot be recovered at all.
* Usage is attributed to the room's *current* primary owner, the only owner the
  data still records. A room that changed hands reports its whole history
  against the present owner.

Rows are written with `source: "log_backfill"` and only via `$setOnInsert`, so
the tool never overwrites a row the live heartbeat produced, and re-running it
is a no-op.

Usage (inside the app container, where `logs/` and MongoDB are reachable)::

    python -m app.usage_backfill                # report only
    python -m app.usage_backfill --apply        # write the rows
    python -m app.usage_backfill --apply --residual
"""
import argparse
import asyncio
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import rooms_collection, usage_daily_collection
from .scribe_manager import AUDIO_BYTES_PER_SEC

# Log timestamps are written in Taiwan time (see logger_config._TW_TZ); the
# rollup buckets by UTC day, so every parsed line is converted before bucketing.
_LOG_TZ = timezone(timedelta(hours=8))
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[audio_usage\] "
    r"session=(?P<sid>\S+) bytes=(?P<bytes>\d+) duration=\S+ chunks=(?P<chunks>\d+)"
)


def _parse_snapshots(logs_dir: Path) -> dict[str, list[tuple[datetime, int, int]]]:
    """Return every logged cumulative snapshot per session, in time order."""
    snapshots: dict[str, list[tuple[datetime, int, int]]] = defaultdict(list)
    for path in sorted(logs_dir.glob("opentranslive_*.log")):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "[audio_usage]" not in line:
                    continue
                match = _LINE_RE.match(line)
                if not match:
                    continue
                moment = datetime.strptime(match["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_LOG_TZ)
                snapshots[match["sid"]].append(
                    (moment, int(match["bytes"]), int(match["chunks"]))
                )
    for runs in snapshots.values():
        runs.sort(key=lambda row: row[0])
    return snapshots


def _deltas_by_day(runs: list[tuple[datetime, int, int]]) -> dict[str, tuple[int, int]]:
    """Differentiate one session's cumulative snapshots into per-UTC-day totals."""
    per_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    prev_bytes = prev_chunks = 0
    for moment, total_bytes, total_chunks in runs:
        if total_bytes >= prev_bytes:
            delta_bytes = total_bytes - prev_bytes
            delta_chunks = max(total_chunks - prev_chunks, 0)
        else:
            # Counter went backwards: a new manager started from zero, so this
            # snapshot is the delta rather than a running total.
            delta_bytes, delta_chunks = total_bytes, total_chunks
        prev_bytes, prev_chunks = total_bytes, total_chunks
        if delta_bytes <= 0:
            continue
        day = moment.astimezone(timezone.utc).strftime("%Y-%m-%d")
        bucket = per_day[day]
        bucket[0] += delta_bytes
        bucket[1] += delta_chunks
    return {day: (values[0], values[1]) for day, values in per_day.items()}


def _hours(audio_bytes: int) -> str:
    return f"{audio_bytes / AUDIO_BYTES_PER_SEC / 3600:.1f}h"


async def backfill(logs_dir: Path, *, apply: bool, residual: bool) -> int:
    snapshots = _parse_snapshots(logs_dir)
    if not snapshots:
        print(f"No [audio_usage] lines found under {logs_dir}")
        return 1

    rooms = [
        room
        async for room in rooms_collection.find(
            {}, {"_id": 0, "sid": 1, "admin_email": 1, "audio_bytes": 1,
                 "audio_duration_secs": 1, "updated_at": 1}
        )
    ]
    # What the rollup already holds, so a re-run neither re-inserts a day nor
    # books a residual for audio some earlier run (or the live heartbeat)
    # already recorded. Without this the tool is only safe to run once.
    existing_days: set[tuple[str, str, str]] = set()
    recorded_by_sid: dict[str, int] = defaultdict(int)
    async for doc in usage_daily_collection.find(
        {}, {"_id": 0, "email": 1, "day": 1, "sid": 1, "audio_bytes": 1}
    ):
        existing_days.add((doc.get("email") or "", doc.get("day") or "", doc.get("sid") or ""))
        recorded_by_sid[doc.get("sid") or ""] += doc.get("audio_bytes") or 0

    now = datetime.now(timezone.utc)
    rows: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    per_owner: dict[str, int] = defaultdict(int)
    derived_total = 0
    skipped_days = 0
    residual_total = 0
    residual_booked = 0
    unowned_sids = 0
    unowned_bytes = 0
    logged_rooms = 0
    owned_rooms = 0

    # Driven by the room list, not by the log files: a room whose log files have
    # been rotated away still holds a cumulative counter, and it has to show up
    # in the residual instead of silently vanishing from the report.
    for room in sorted(rooms, key=lambda r: r.get("sid") or ""):
        sid = room.get("sid") or ""
        absolute = int(room.get("audio_bytes")
                       or round((room.get("audio_duration_secs") or 0) * AUDIO_BYTES_PER_SEC))
        runs = snapshots.get(sid, [])
        if not runs and absolute <= 0:
            continue
        email = (room.get("admin_email") or "").lower()
        if not email:
            # Ownership was released (see delete_session); nothing records who
            # streamed it, so it cannot be attributed to anybody.
            unowned_sids += 1
            unowned_bytes += absolute
            continue
        per_day = _deltas_by_day(runs)
        derived_total += sum(audio_bytes for audio_bytes, _ in per_day.values())
        owned_rooms += 1
        logged_rooms += 1 if runs else 0
        pending = 0
        for day, (audio_bytes, chunks) in per_day.items():
            if (email, day, sid) in existing_days:
                skipped_days += 1
                continue
            row = rows[(email, day, sid)]
            row[0] += audio_bytes
            row[1] += chunks
            pending += audio_bytes
        # What the room counted but neither the rollup nor a snapshot covers:
        # the tail of every run, and every day whose log file is already gone.
        gap = absolute - recorded_by_sid[sid] - pending
        if gap > 0:
            residual_total += gap
            if residual:
                last_active = room.get("updated_at") or now
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=timezone.utc)
                day = last_active.astimezone(timezone.utc).strftime("%Y-%m-%d")
                if (email, day, sid) not in existing_days:
                    rows[(email, day, sid)][0] += gap
                    residual_booked += gap
        per_owner[email] += pending + (gap if residual and gap > 0 else 0)

    days = sorted({day for _, day, _ in rows})
    print(f"rooms with audio: {owned_rooms + unowned_sids}"
          f"  rooms with log coverage: {logged_rooms}"
          f"  sessions in logs: {len(snapshots)}  new rows: {len(rows)}")
    if days:
        print(f"day range: {days[0]} .. {days[-1]}")
    print(f"reconstructed from logs: {_hours(derived_total)}"
          f"  (already in usage_daily: {skipped_days} days)")
    for email, audio_bytes in sorted(per_owner.items(), key=lambda kv: -kv[1]):
        print(f"  {email:40s} {_hours(audio_bytes):>8s} to add")
    if unowned_sids:
        print(f"unattributable (ownership released): {unowned_sids} rooms, "
              f"{_hours(unowned_bytes)}")
    print(f"counted by rooms but not recorded anywhere: {_hours(residual_total)}"
          + (f" → booked to each room's last activity day ({_hours(residual_booked)})"
             if residual else " (re-run with --residual to book it by last activity day)"))

    if not apply or not rows:
        print("\nnothing to write" if apply else
              "\nreport only — re-run with --apply to write these rows")
        return 0

    written = 0
    for (email, day, sid), (audio_bytes, chunks) in sorted(rows.items()):
        result = await usage_daily_collection.update_one(
            {"email": email, "day": day, "sid": sid},
            {"$setOnInsert": {
                "audio_bytes": audio_bytes,
                "audio_chunks": chunks,
                "updated_at": now,
                "source": "log_backfill",
            }},
            upsert=True,
        )
        written += 1 if result.upserted_id else 0
    print(f"\ninserted {written} rows, left {len(rows) - written} existing rows untouched")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--logs-dir", default="logs", type=Path,
                        help="directory holding opentranslive_YYYYMMDD.log files")
    parser.add_argument("--apply", action="store_true",
                        help="write the rows (default: report only)")
    parser.add_argument("--residual", action="store_true",
                        help="book audio the logs do not cover against each room's "
                             "last activity day, so per-day totals reconcile with the "
                             "room's cumulative counter")
    args = parser.parse_args()
    return asyncio.run(backfill(args.logs_dir, apply=args.apply, residual=args.residual))


if __name__ == "__main__":
    raise SystemExit(main())
