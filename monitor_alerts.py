#!/usr/bin/env python3
"""One-process alert stream for Claude's Monitor tool: follows the newest
quoter log and data/watch_losses.log, and checks that the bot and the watcher
are alive. Prints alert lines only.

Replaces a shell pipeline whose background loops and tails were left running
after each monitor session expired (66 loops / 133 tails had piled up by
2026-10-05), and which made `pgrep` liveness checks match the wrong processes.
Liveness here matches the real interpreter command lines, not text that
appears inside other commands.

    python3 -u monitor_alerts.py --minutes 29
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import time

QUOTER_RE = re.compile(r"Traceback|OVER-OFFERED|HARD STOP|SelfTrade|STOP-LOSS|overrides unreadable|"
                       r"stopped after|reached --max-hours|FAILED \([3-9]|API ERROR|OUTAGE SAFE-MODE")
# Per-race refusals are routine at the engine's resting cap (a couple of deep
# rungs per pass); alert only when a pass total is large.
PASS_REFUSED_RE = re.compile(r"^pass \d+:.*!! (\d+) REFUSED")
WATCH_RE = re.compile(r"LOSS|UNDERWATER|EXIT SHORT|QUOTER DOWN|STOP-LOSS")


def alive(pattern: str) -> bool:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return any(pid != str(os.getpid()) for pid in out)


def follow(path: str, pos: int) -> tuple[list[str], int]:
    try:
        with open(path) as f:
            f.seek(pos)
            lines = f.readlines()
            return lines, f.tell()
    except OSError:
        return [], pos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=29)
    args = ap.parse_args()
    qlog = max(glob.glob("data/quoter[0-9]*.log"), key=os.path.getmtime)
    wlog = "data/watch_losses.log"
    qpos = os.path.getsize(qlog)
    wpos = os.path.getsize(wlog) if os.path.exists(wlog) else 0
    end = time.time() + args.minutes * 60
    last_live = 0.0
    while time.time() < end:
        lines, qpos = follow(qlog, qpos)
        for l in lines:
            m = PASS_REFUSED_RE.search(l)
            if QUOTER_RE.search(l) or (m and int(m.group(1)) >= 10):
                print(l.rstrip()[:220], flush=True)
        lines, wpos = follow(wlog, wpos)
        for l in lines:
            if WATCH_RE.search(l):
                print(l.rstrip()[:220], flush=True)
        if time.time() - last_live > 120:
            last_live = time.time()
            if not alive(r"^/Library/Frameworks/Python\.framework/.*Python -u run_quoter\.py"):
                print("QUOTER DOWN", flush=True)
            if not alive(r"^/Library/Frameworks/Python\.framework/.*Python -u watch_losses\.py"):
                print("WATCHER DOWN", flush=True)
        time.sleep(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
