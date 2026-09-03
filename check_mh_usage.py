#!/usr/bin/env python3
"""
Check that every `mh ...` command written into a skill or agent file is real.

    python3 check_mh_usage.py ~/Projects/mellonhead/.claude

Workstream 2.6 rewrites the skills and agents to write through the CLI
instead of editing markdown. A skill that names a command that does not
exist, or gets its arguments wrong, fails silently: the agent runs it
mid-task, the write never lands, and nothing says so until someone notices
the record is behind. Prose cannot be type-checked, but an invocation can.

Every `mh` line found in the instructions is parsed by the real argparse
parser. Placeholders in angle brackets are substituted with plausible values
first, so `mh task status <key#id> <status>` is checked for shape rather than
rejected for not being a literal command.
"""

import argparse
import contextlib
import io
import os
import re
import shlex
import sys
from pathlib import Path

import mhcli

# `mh task done aba-academy#25`, in prose, a fenced block, or backticks.
INVOCATION = re.compile(r"(?<![\w./-])mh\s+([a-z][\w \-#<>|/\"'=.:{}]*)")

# What a placeholder stands for, so the shape can still be checked.
PLACEHOLDERS = {
    "key#id": "proj#1", "key#n": "proj#1", "task": "proj#1", "id": "1",
    "status": "waiting", "project": "proj", "project_key": "proj",
    "key": "proj", "week": "2026-08-31", "date": "2026-08-31",
    "day": "2026-08-31", "seq": "10", "n": "10", "title": "A title",
    "text": "Some text", "note": "Some text", "path": "some/path.md",
    "words": "some words", "load": "deep", "owner": "mq", "due": "2026-09-30",
}


def substitute(command):
    """Replace <placeholders> with something the parser will accept."""
    def swap(match):
        name = match.group(1).strip().lower().replace("-", "_")
        return PLACEHOLDERS.get(name, "placeholder")
    return re.sub(r"<([^>]+)>", swap, command)


def extract(text):
    """Every mh invocation in a file, with its line number."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip().lstrip("-*# ").lstrip("`")
        for match in INVOCATION.finditer(stripped):
            command = match.group(0)
            # Stop at prose punctuation that cannot be part of a command.
            command = re.split(r"\s+[—–]|\.\s|,\s|;\s|\)\s|`", command)[0]
            found.append((number, command.strip().rstrip(".,;:`)")))
    return found


def walk(parser, argv):
    """
    Follow argv down the subparser tree. Returns (leaf, remaining, bad_word).

    bad_word is set when a word should have named a subcommand and did not,
    which is the invented-command case worth reporting.
    """
    import argparse as _ap
    while argv:
        groups = next((a for a in parser._actions
                       if isinstance(a, _ap._SubParsersAction)), None)
        if groups is None:
            return parser, argv, None
        word = argv[0]
        if word.startswith("-"):
            return parser, argv, None
        if word not in groups.choices:
            return parser, argv, word
        parser = groups.choices[word]
        argv = argv[1:]
    return parser, argv, None


def check(command, parser):
    """
    Parse one invocation. Returns None if valid, else the reason.

    Prose names commands without their arguments — "close it with
    `mh task done`" is a reference, not a prescription — so a missing
    positional is not an error. What is worth catching is a subcommand that
    does not exist, or a value outside a fixed set. Flagging both alike made
    eleven of thirteen findings in the handoff brief false positives, which
    is how a checker gets ignored.
    """
    try:
        argv = shlex.split(substitute(command))
    except ValueError as exc:
        return f"unparseable: {exc}"
    if not argv or argv[0] != "mh":
        return None
    argv = argv[1:]
    if not argv:
        return "bare `mh` with no subcommand"

    leaf, rest, bad = walk(parser, argv)
    if bad:
        return f"no such command: `{bad}`"

    # The command path is real. If the invocation supplies every required
    # positional it is a prescription, so hand it to argparse, which validates
    # choices, type converters and options far better than reimplementing them
    # here. With fewer, it is prose naming a command and only the path matters.
    required = [a for a in leaf._actions
                if not a.option_strings and a.dest != "help"
                and a.nargs not in ("?", "*")]
    supplied = [w for w in rest if not w.startswith("-")]
    if len(supplied) < len(required):
        # Still worth catching a misspelled option in a partial reference.
        known = {o for a in leaf._actions for o in a.option_strings}
        for word in rest:
            if word.startswith("--") and word.split("=")[0] not in known:
                return f"unknown option: `{word.split('=')[0]}`"
        return None

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(argv)
    except SystemExit:
        return "not a valid mh command"
    except Exception as exc:                     # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Verify mh commands quoted in skills and agents are real")
    ap.add_argument("path", type=Path, nargs="?",
                    default=Path.home() / "Projects" / "mellonhead" / ".claude")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = args.path.expanduser()
    if not root.exists():
        print(f"nothing at {root}", file=sys.stderr)
        return 2

    parser = mhcli.build_parser()
    files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    problems, total = [], 0

    for path in files:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for number, command in extract(text):
            total += 1
            reason = check(command, parser)
            if reason:
                rel = path.relative_to(root) if root.is_dir() else path.name
                problems.append((rel, number, command, reason))

    if not problems:
        if not args.quiet:
            print(f"checked {total} mh invocation(s) across {len(files)} "
                  f"file(s): all valid")
        return 0

    print(f"{len(problems)} of {total} mh invocation(s) are not valid "
          f"commands:\n")
    for rel, number, command, reason in problems:
        print(f"  {rel}:{number}")
        print(f"    {command}")
        print(f"    -> {reason}\n")
    print("Run `mh --help`, or `mh task --help`, for the real surface.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
