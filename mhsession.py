#!/usr/bin/env python3
"""
mhsession — which Claude conversation is this process running inside?

Every `mh` write is stamped with the conversation that made it, so a task
page can list "conversations on this task" without anyone linking them by
hand. Asking every skill to pass a session id would be one more convention
nobody follows; reading it off the process is free and cannot be forgotten.

Three sources, each optional:

  ITERM_SESSION_ID      the shell inherits it from iTerm ("w0t1p0:<uuid>");
                        the uuid half is what the dashboard navigates by
  the process tree      walk ppid upwards to a `claude` process and read
                        ~/.claude/sessions/<pid>.json, the file the dashboard
                        already reads for sessionId, cwd, kind and name
  MH_ACTOR              the sweep scripts export it so a scheduled run is
                        attributed even when a prompt forgets --actor

Failure at any step is silent and leaves the field None. Nothing else about
a write changes when the session is unknown.
"""

import json
import os
import subprocess
from pathlib import Path

MAX_DEPTH = 8

EMPTY = {"session_id": None, "iterm_id": None, "cwd": None, "kind": None,
         "name": None, "actor": None, "pid": None}


def _parent_of(pid):
    """(ppid, command name) for a pid, via one ps call. None if it is gone."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split(None, 1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), parts[1].strip()
    except ValueError:
        return None


def _is_claude(command):
    name = command.split()[0] if command else ""
    return name == "claude" or name.endswith("/claude")


def find_claude_pid(start_pid=None, parent_of=_parent_of, max_depth=MAX_DEPTH):
    """
    The nearest ancestor that is a `claude` process, or None.

    `parent_of` is injectable so a test can hand in a fake process table.
    Starts from the parent rather than this process: mh itself is never
    claude, and the shim exec()s python so there is no bash in between.
    """
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(max_depth):
        if not pid or pid <= 1:
            return None
        info = parent_of(pid)
        if info is None:
            return None
        ppid, command = info
        if _is_claude(command):
            return pid
        pid = ppid
    return None


def read_session_file(pid, sessions_dir=None):
    """~/.claude/sessions/<pid>.json, or None."""
    base = Path(sessions_dir) if sessions_dir else Path.home() / ".claude" / "sessions"
    path = base / f"{pid}.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def iterm_id_from_env(env=None):
    """'w0t1p0:E7B7...' -> 'E7B7...', the id AppleScript reports for the tab."""
    raw = (env if env is not None else os.environ).get("ITERM_SESSION_ID", "")
    if not raw:
        return None
    return raw.rsplit(":", 1)[-1].strip() or None


def current_session(env=None, start_pid=None, parent_of=_parent_of,
                    sessions_dir=None):
    """
    {session_id, iterm_id, cwd, kind, name, actor, pid}, any of them None.

    Every argument exists for tests; callers pass nothing.
    """
    env = os.environ if env is None else env
    result = dict(EMPTY)
    result["iterm_id"] = iterm_id_from_env(env)
    result["actor"] = (env.get("MH_ACTOR") or "").strip().lower() or None

    try:
        pid = find_claude_pid(start_pid=start_pid, parent_of=parent_of)
    except Exception:                                  # noqa: BLE001
        pid = None
    if pid:
        result["pid"] = pid
        data = read_session_file(pid, sessions_dir) or {}
        result["session_id"] = data.get("sessionId") or None
        result["cwd"] = data.get("cwd") or None
        result["kind"] = data.get("kind") or None
        result["name"] = data.get("name") or None
    if result["session_id"] and not result["kind"]:
        result["kind"] = "unknown"
    return result


def describe(info):
    """One line, for `mh session show` and the SessionStart hook."""
    if not info.get("session_id") and not info.get("iterm_id"):
        return "no Claude session detected (plain terminal, or not under claude)"
    bits = []
    if info.get("session_id"):
        bits.append(f"session {info['session_id']}")
    if info.get("name"):
        bits.append(f"name {info['name']}")
    if info.get("kind"):
        bits.append(info["kind"])
    if info.get("iterm_id"):
        bits.append(f"iterm {info['iterm_id']}")
    if info.get("actor"):
        bits.append(f"actor {info['actor']}")
    if info.get("cwd"):
        bits.append(f"cwd {info['cwd']}")
    return "  ".join(bits)


if __name__ == "__main__":
    import time
    t0 = time.perf_counter()
    info = current_session()
    ms = (time.perf_counter() - t0) * 1000
    print(describe(info))
    print(f"({ms:.1f} ms)")
