"""`wilbur status` -- read the authoritative run state, no model, no GPU.

Lists every `<sessions>/<id>.run.json`: pid, state, reason, since, last
progress, cwd -- verifying pid liveness. A file whose pid is dead reads as
`dead`. `--check` returns an exit code a script can act on:

  0  nothing working, or every working run progressed within
     `status_hung_minutes` (default 30)
  1  a `working` run with no progress past that, a `dead` file, or a
     user message unread past 2x `message_check_max_s` (default 60)
  2  UNKNOWN -- the sessions dir is unreadable

`--json` emits a JSON array for tools (watch.sh, the Command Center).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import config as _config
from . import inbox
from . import runstate


def _hung_minutes() -> int:
    try:
        return int(_config.Config.load().status_hung_minutes)
    except Exception:
        return 30


def _message_bound_s() -> float:
    try:
        return float(_config.Config.load().message_check_max_s)
    except Exception:
        return inbox.DEFAULT_BOUND_S


def _classify(rows: list[dict[str, Any]], hung_minutes: int,
              bound_s: float | None = None, report=None) -> int:
    """Exit code for `--check` over already-read rows (0/1). A live run with a
    user message unread past 2x `message_check_max_s` fails it too."""
    now = time.time()
    worst = 0
    bound = inbox.DEFAULT_BOUND_S if bound_s is None else bound_s
    for row in rows:
        if row.get("state") != runstate.DEAD:
            age = inbox.stale_unread(row, bound, now)
            if age is not None:
                worst = max(worst, 1)
                if report is not None:
                    report(f"{row.get('session_id', '?')}: user message unread for "
                           f"{age / 60:.1f} min (bound {bound:.0f}s)")
        state = row.get("state")
        if state == runstate.DEAD:
            worst = max(worst, 1)
            continue
        if state != runstate.WORKING:
            continue
        if hung_minutes <= 0:
            continue
        last = row.get("last_progress_at") or row.get("since") or now
        if now - last > hung_minutes * 60:
            worst = max(worst, 1)
    return worst


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no runs")
        return
    hdr = f"{'STATE':<16} {'REASON':<14} {'SINCE':<9} {'LAST':<9} {'PID':>8}  CWD"
    print(hdr)
    print("-" * len(hdr))
    for row in rows:
        state = row.get("state", "?")
        reason = row.get("reason") or "-"
        since = _fmt_ts(row.get("since"))
        last = _fmt_ts(row.get("last_progress_at"))
        pid = row.get("pid", "-")
        cwd = row.get("cwd", "")
        print(f"{state:<16} {reason:<14} {since:<9} {last:<9} {pid:>8}  {cwd}")


def _report(line: str) -> None:
    print(line, file=sys.stderr)


def status_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wilbur status",
        description="Show the authoritative state of every Wilbur run.")
    parser.add_argument("--check", action="store_true",
                        help="exit 0 (ok) / 1 (working-hung, dead, or a stale unread message) / 2 (unknown)")
    parser.add_argument("--json", action="store_true",
                        help="emit a JSON array for tools")
    args = parser.parse_args(argv)

    # UNKNOWN (2): the sessions dir itself is unreadable.
    # Read-only: `status` never creates the sessions dir (a missing dir is
    # simply "no runs").
    try:
        rows = runstate.read_all()
    except OSError:
        if args.json:
            print(json.dumps({"error": "sessions dir unreadable"}))
        else:
            print("sessions dir unreadable", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rows, indent=2))
        return _classify(rows, _hung_minutes(), _message_bound_s(), _report) if args.check else 0

    _print_table(rows)
    return _classify(rows, _hung_minutes(), _message_bound_s(), _report) if args.check else 0
