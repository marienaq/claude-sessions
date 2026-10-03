#!/usr/bin/env python3
"""
mhbackup — a daily off-machine copy of the task store.

tasks.db, tasks.json and tasks-audit.log are gitignored (the content repo's
remote is public), so without this nothing leaves the laptop. Writes
tasks-YYYY-MM-DD.db and tasks-audit-YYYY-MM-DD.log into a folder that a sync
client carries off the machine (Google Drive), and keeps the newest KEEP days.

The snapshot goes through SQLite's backup API on a read-only connection, not
a file copy: the store runs in WAL mode, so the .db file alone can be missing
recent writes, and a copy taken mid-write can be torn. Read-only also means
this never runs open_store()'s schema migrations against the live store.

Never point a sync client at the live tasks.db; only finished snapshots go
into the synced folder.

Run: python3 mhbackup.py --dest <dir>     (or MH_BACKUP_DIR=<dir>)
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

KEEP = 30
SNAPSHOT = re.compile(r"^tasks-(\d{4}-\d{2}-\d{2})\.db$")


class BackupError(Exception):
    pass


def snapshot(db_path, out_path):
    """Copy db_path into out_path consistently, and check the copy."""
    src = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(out_path))
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
            tasks = dst.execute("SELECT count(*) FROM tasks").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        raise BackupError(f"snapshot failed integrity_check: {ok}")
    return tasks


def place(src, dest):
    """Copy src to dest via a temp name, so a sync client never sees half a file."""
    tmp = dest.with_name(f".{dest.name}.partial")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def prune(dest_dir, keep=KEEP):
    """Drop snapshots (and their audit logs) beyond the newest `keep` days."""
    days = sorted((m.group(1) for p in dest_dir.iterdir()
                   if (m := SNAPSHOT.match(p.name))), reverse=True)
    removed = []
    for day in days[keep:]:
        for name in (f"tasks-{day}.db", f"tasks-audit-{day}.log"):
            p = dest_dir / name
            if p.exists():
                p.unlink()
                removed.append(name)
    return removed


def backup(root, dest_dir, today=None, keep=KEEP):
    root = Path(root).expanduser()
    dest_dir = Path(dest_dir).expanduser()
    db = root / "operations" / "tasks.db"
    audit = root / "operations" / "tasks-audit.log"
    if not db.exists():
        raise BackupError(f"no task store at {db}")
    # Not mkdir: if Drive is signed out or unmounted, writing a local folder
    # at the same path would look like a backup and be none.
    if not dest_dir.is_dir():
        raise BackupError(f"backup folder {dest_dir} does not exist "
                          "(is Google Drive running and signed in?)")
    day = (today or date.today()).isoformat()

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "tasks.db"
        tasks = snapshot(db, local)
        place(local, dest_dir / f"tasks-{day}.db")
    if audit.exists():
        place(audit, dest_dir / f"tasks-audit-{day}.log")
    return tasks, prune(dest_dir, keep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", default=os.environ.get("MH_BACKUP_DIR"),
                    help="folder to write into (default $MH_BACKUP_DIR)")
    ap.add_argument("--root", default=os.environ.get(
        "MELLONHEAD_ROOT", str(Path.home() / "Projects" / "mellonhead")))
    ap.add_argument("--keep", type=int, default=KEEP)
    args = ap.parse_args(argv)
    if not args.dest:
        ap.error("--dest or MH_BACKUP_DIR is required")

    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        tasks, removed = backup(args.root, args.dest, keep=args.keep)
    except (BackupError, sqlite3.Error, OSError) as e:
        print(f"{stamp} backup FAILED: {e}", file=sys.stderr)
        return 1
    print(f"{stamp} backed up {tasks} tasks to {args.dest}"
          + (f"; pruned {', '.join(removed)}" if removed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
