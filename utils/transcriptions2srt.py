#!/usr/bin/env python3
"""Convert OpenTransLive transcription JSON to per-language SRT files."""

import json
import sys
from pathlib import Path


def seconds_to_srt_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds % 1) * 1000))
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(entries: list[tuple[float, float, str]]) -> str:
    lines = []
    for i, (start, end, text) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{seconds_to_srt_timestamp(start)} --> {seconds_to_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def transcriptions_to_srt(data: dict, output_dir: Path) -> None:
    transcriptions = data.get("transcriptions", [])
    if not transcriptions:
        print("No transcriptions found.", file=sys.stderr)
        return

    # Use the first transcription's start_time as t=0
    epoch_offset = transcriptions[0]["start_time"]

    # Collect entries per language: lang -> [(rel_start, rel_end, text)]
    lang_entries: dict[str, list[tuple[float, float, str]]] = {}

    for t in transcriptions:
        result = t.get("result") or {}
        translated = result.get("translated") or {}
        rel_start = t["start_time"] - epoch_offset
        rel_end = t["end_time"] - epoch_offset

        for lang, text in translated.items():
            lang_entries.setdefault(lang, []).append((rel_start, rel_end, text))

    sid = data.get("sid", "output")
    output_dir.mkdir(parents=True, exist_ok=True)

    for lang, entries in lang_entries.items():
        srt_content = build_srt(entries)
        out_path = output_dir / f"{sid}.{lang}.srt"
        out_path.write_text(srt_content, encoding="utf-8")
        print(f"Written: {out_path}")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.json> [output_dir]", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_dir = Path("output")

    data = json.loads(input_path.read_text(encoding="utf-8"))
    transcriptions_to_srt(data, output_dir)


if __name__ == "__main__":
    main()
