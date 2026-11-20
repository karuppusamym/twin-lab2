"""Run Twin Lab collectors in one recoverable TwinLab2 workstation session."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


def save_json(path, value):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, indent=2), encoding="utf-8")
    pending.replace(path)


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


def start(args):
    folder = args.folder.resolve()
    if args.twin_lab is None:
        from agent import twinlab

        args.twin_lab = twinlab.vendor_root()
    checkout = args.twin_lab.resolve()
    if args.minutes <= 0:
        raise ValueError("Use a positive recording duration.")
    collector = checkout / "lab" / "labctl" / "labctl" / "cli.py"
    if not collector.is_file():
        raise ValueError("No Twin Lab collectors: run `python -m agent.twinlab fetch`, or name a checkout with --twin-lab.")
    pinned = checkout / "twinlab-pin.json"
    if pinned.is_file():  # the vendored, digest-checked copy of twinlab.lock.json's commit
        from agent import twinlab

        try:
            twinlab.verify()
        except twinlab.PinError as error:
            raise ValueError(str(error)) from error
        revision = json.loads(pinned.read_text(encoding="utf-8"))["commit"]
    else:
        revision = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True,
        ).strip()
        if subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"], text=True).strip():
            raise ValueError("Use a clean Twin Lab checkout so the recorded source revision is reproducible.")
    commands = {}
    if args.web:
        commands["web"] = ["evidence", "record", "capture", args.web, "--no-screenshots"]
    if args.cdp:
        commands["web"] = ["evidence", "attach", "capture", "--cdp", args.cdp,
                           "--minutes", str(args.minutes), "--no-screenshots"]
    desktop_targets = [("--process", process) for process in args.desktop]
    desktop_targets += [("--pid", str(pid)) for pid in args.desktop_pid]
    for index, (flag, value) in enumerate(desktop_targets, 1):
        commands[f"desktop-{index}"] = ["desktop", "attach", flag, value,
                                       "--max-minutes", str(args.minutes)]
    if not commands:
        raise ValueError("Select a browser or desktop collector.")
    folder.mkdir(parents=True, exist_ok=False)
    state = {"id": uuid.uuid4().hex, "upstream_commit": revision, "channels": [
        {"name": name, "status": "starting"} for name in commands
    ]}
    state_path = folder / "session.json"
    save_json(state_path, state)
    processes = []
    logs = []
    interrupted = False
    try:
        for channel in state["channels"]:
            name = channel["name"]
            log = (folder / f"{name}.log").open("wb")
            logs.append(log)
            bootstrap = (
                "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from labctl.cli import app; app()"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", bootstrap, str(collector.parent.parent),
                 *commands[name], "--out", str(folder / name)],
                cwd=checkout, stdout=log, stderr=subprocess.STDOUT,
            )
            processes.append((channel, proc))
            channel["status"] = "running"
            save_json(state_path, state)
        print(f"Recording session {state['id']}. Press Ctrl+C to stop.", flush=True)
        while any(proc.poll() is None for _, proc in processes):
            for channel, proc in processes:
                if channel["status"] == "running" and proc.poll() is not None:
                    channel.update(status="complete" if proc.returncode == 0 else "failed",
                                   exit_code=proc.returncode)
                    save_json(state_path, state)
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        for channel, proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
                channel["status"] = "interrupted"
            elif channel["status"] == "running":
                channel["status"] = "interrupted" if interrupted else (
                    "complete" if proc.returncode == 0 else "failed"
                )
            channel["exit_code"] = proc.returncode
        for log in logs:
            log.close()
        save_json(state_path, state)
        count = recover(folder)
    return 0 if count and all(ch["status"] != "failed" for ch in state["channels"]) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("start")
    launch.add_argument("folder", type=Path)
    launch.add_argument("--twin-lab", type=Path, default=None,
                        help="a clean Twin Lab checkout (default: the pinned copy in vendor/)")
    browser = launch.add_mutually_exclusive_group()
    browser.add_argument("--web", help="Open a fresh browser at this URL.")
    browser.add_argument("--cdp", help="Attach to the debug endpoint of an existing browser.")
    launch.add_argument("--desktop", action="append", default=[], help="Attach to a running executable.")
    launch.add_argument("--desktop-pid", type=int, action="append", default=[],
                        help="Attach to a specific running Windows process ID.")
    launch.add_argument("--minutes", type=float, default=30)
    resume = commands.add_parser("recover")
    resume.add_argument("folder", type=Path)
    args = parser.parse_args()
    try:
        return start(args) if args.command == "start" else (0 if recover(args.folder) else 1)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Capture failed: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
