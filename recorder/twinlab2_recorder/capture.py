"""Rebuild the import file of a capture folder from its channel logs.

    python -m twinlab2_recorder.capture recover <folder>

A capture folder holds `session.json` and one `<channel>/steps.jsonl` per collector; `recover`
merges them into `<folder>/steps.jsonl` in time order, skipping an incomplete last line. The
recorder (TwinLabRecorder) records sessions itself; this is for folders written by the earlier
`capture.py start`, which ran Twin Lab's whole command line and was removed when TwinLab2
stopped depending on Twin Lab.
"""

import argparse
import json
from pathlib import Path
import sys


def recover(folder):
    state = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    events = []
    for index, channel in enumerate(state["channels"]):
        source = folder / channel["name"] / "steps.jsonl"
        if not source.is_file():
            continue
        contents = source.read_bytes()
        lines = contents.splitlines(keepends=True)
        seen = set()
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                if line_number == len(lines) and not line.endswith(b"\n"):
                    print(f"{channel['name']}: ignored an incomplete final line.", file=sys.stderr)
                    break
                raise
            if not isinstance(row, dict):
                raise ValueError(f"{channel['name']}: expected an event object on line {line_number}.")
            seq = row.get("seq")
            if type(seq) is not int or not 0 <= seq < 1_000_000_000 or seq in seen:
                raise ValueError(f"{channel['name']}: invalid or repeated sequence {seq!r}.")
            seen.add(seq)
            events.append({
                "seq": index * 1_000_000_000 + seq,
                "session_id": state["id"], "channel": channel["name"], "source_seq": seq,
                "ts": row.get("ts"), "url": row.get("url"), "action": row.get("action"),
                "surface": row.get("surface"), "process": row.get("process"),
            })
    output = folder / "steps.jsonl"
    pending = output.with_suffix(".tmp")
    events.sort(key=lambda row: (str(row["ts"] or ""), row["seq"]))
    with pending.open("w", encoding="utf-8", newline="\n") as stream:
        for row in events:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    pending.replace(output)
    print(f"{len(events)} events in {output}")
    return len(events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resume = commands.add_parser("recover", help="rebuild steps.jsonl from a capture folder's channel logs")
    resume.add_argument("folder", type=Path)
    args = parser.parse_args()
    try:
        return 0 if recover(args.folder) else 1
    except (OSError, ValueError) as error:
        parser.exit(1, f"Capture failed: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
