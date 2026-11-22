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
# Quoted arguments can hold any punctuation ("Pitched 11/23, card"); cut_prose
# then trims the prose that follows the command, outside quotes only.
INVOCATION = re.compile(r"(?<![\w./-])mh\s+([a-z][\w \-#<>|/\"'=.:{}(),;!?@+&%~*$]*)")

# What a placeholder stands for, so the shape can still be checked.
PLACEHOLDERS = {
    "key#id": "proj#1", "key#n": "proj#1", "task": "proj#1", "id": "1",
    "status": "waiting", "project": "proj", "project_key": "proj",
    "key": "proj", "week": "2026-08-31", "date": "2026-08-31",
    "day": "2026-08-31", "seq": "10", "n": "10", "title": "A title",
    "text": "Some text", "note": "Some text", "path": "some/path.md",
    "words": "some words", "load": "deep", "owner": "mq", "due": "2026-09-30",
    "person": "anushka",
    # the task-view verbs
    "agent": "iddy", "name": "iddy", "your_name": "iddy", "verdict": "pass",
    "v": "pass", "answer": "Some answer", "question": "Some question?",
    "who": "Anushka", "who_waits": "Anushka", "where": "slack",
    "source": "slack", "orca's_default": "Some answer",
    "what_you_would_do": "Some answer", "brief_path": "some/brief.md",
    "handback": "MQ", "reason": "Some reason", "expect": "some/path.md",
    "artifact": "some/path.md", "findings": "some/findings.md",
    # scheduling
    "done_when": "Some text", "what_done_means": "Some text",
    "ready": "2026-10-12", "gap": "3", "lane": "aba", "value": "1.5",
    "template": "course", "prefix": "Creating Documents", "course": "Creating Documents",
    "n=key#id": "6=proj#1",
}


TOKEN = re.compile(r"\x00(\d+)\x00")


def tokenize(command):
    """Replace each <placeholder> with a token that survives shlex as one word,
    so the value can be chosen once it is known which argument it fills."""
    names = []
    def swap(match):
        names.append(match.group(1))
        return f"\x00{len(names) - 1}\x00"
    return re.sub(r"<([^>]+)>", swap, command), names


def value_for(name, action=None):
    choices = list(getattr(action, "choices", None) or [])
    plain = substitute(f"<{name}>")
    if choices:
        return plain if plain in choices else str(choices[0])
    return plain


def fill(argv, names, leaf):
    """Fill tokens: a whole-word token takes a value its argument accepts."""
    positionals = [a for a in leaf._actions if not a.option_strings and a.dest != "help"]
    by_option = {o: a for a in leaf._actions for o in a.option_strings}
    out, pos, pending = [], 0, None
    for word in argv:
        action = None
        if pending is not None:
            action, pending = pending, None
        elif word.startswith("-"):
            opt = by_option.get(word.split("=")[0])
            if opt is not None and opt.nargs != 0 and "=" not in word \
                    and not isinstance(opt, argparse._StoreTrueAction):
                pending = opt
        else:
            action = positionals[pos] if pos < len(positionals) else None
            pos += 1
        m = TOKEN.fullmatch(word)
        if m:
            out.append(value_for(names[int(m.group(1))], action))
        else:
            out.append(TOKEN.sub(lambda m: substitute(f"<{names[int(m.group(1))]}>"), word))
    return out


def substitute(command):
    """Replace <placeholders> with something the parser will accept."""
    def swap(match):
        name = match.group(1).strip().lower().replace("-", "_")
        if name in PLACEHOLDERS:
            return PLACEHOLDERS[name]
        # `<pass|pass-with-notes|back>` spells out a choice list; the first
        # alternative is as good as any for checking the shape.
        if "|" in name:
            return match.group(1).split("|")[0].strip()
        return "placeholder"
    return re.sub(r"<([^>]+)>", swap, command)


# Prose punctuation, and shell plumbing (a redirect or pipe ends the mh part).
PROSE_END = re.compile(r"\s+[—–]|\.\s|,\s|;\s|\)\s|`|\s[<>|]\s|\s&&\s|\.$|,$|\)$")


def cut_prose(command):
    """Cut at the first prose punctuation that is outside quotes, so a comma
    inside `--link "Your post, M/D"` is part of the argument."""
    quote = None
    for i, ch in enumerate(command):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        m = PROSE_END.match(command, i)
        if m:
            return command[:i]
    return command


def extract(text):
    """Every mh invocation in a file, with its line number."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip().lstrip("-*# ").lstrip("`")
        for match in INVOCATION.finditer(stripped):
            command = match.group(0)
            # Stop at prose punctuation that cannot be part of a command.
            command = cut_prose(command)
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
    tokenized, names = tokenize(command)
    try:
        argv = shlex.split(tokenized)
    except ValueError as exc:
        return f"unparseable: {exc}"
    if not argv or argv[0] != "mh":
        return None
    argv = argv[1:]
    if not argv:
        return "bare `mh` with no subcommand"

    head = [TOKEN.sub(lambda m: substitute(f"<{names[int(m.group(1))]}>"), w) for w in argv]
    leaf, rest, bad = walk(parser, head)
    if bad:
        return f"no such command: `{bad}`"
    # Now that the command is known, give each placeholder a value its
    # argument accepts (`<status>` is a task status for `mh task`, an idea
    # status for `mh content idea`).
    rest = fill(argv[len(argv) - len(rest):], names, leaf)
    argv = argv[:len(argv) - len(rest)] + rest
    argv = [TOKEN.sub(lambda m: substitute(f"<{names[int(m.group(1))]}>"), w) for w in argv]

    # The command path is real. If the invocation supplies every required
    # positional it is a prescription, so hand it to argparse, which validates
    # choices, type converters and options far better than reimplementing them
    # here. With fewer, it is prose naming a command and only the path matters.
    required = [a for a in leaf._actions
                if not a.option_strings and a.dest != "help"
                and a.nargs not in ("?", "*")]

    # A word after a value-taking option belongs to that option, not to a
    # positional. Counting "capture" in `--source capture` as a positional
    # made a partial reference look complete, and it was then rejected for a
    # missing argument prose never supplies.
    takes_value = {o for a in leaf._actions for o in a.option_strings
                   if a.nargs != 0 and not isinstance(a, __import__("argparse")._StoreTrueAction)}
    supplied, skip = [], False
    for word in rest:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in takes_value and "=" not in word
            continue
        supplied.append(word)
    if len(supplied) < len(required):
        # Still worth catching a misspelled option in a partial reference.
        known = {o for a in leaf._actions for o in a.option_strings}
        for word in rest:
            if word.startswith("--") and word.split("=")[0] not in known:
                return f"unknown option: `{word.split('=')[0]}`"
        return None

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parsed = parser.parse_args(argv)
    except SystemExit:
        return "not a valid mh command"
    except Exception as exc:                     # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    # What argparse cannot say (a task needs --load unless it is a proposal)
    # is checked by the same function main() runs.
    try:
        mhcli.check_args(parsed)
    except mhcli.CliError as exc:
        return str(exc)
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

    where = root if root.is_dir() else root.parent
    if not problems:
        if total == 0:
            # A clean zero and a real pass read identically at a glance, and
            # scanning the wrong directory is the likeliest cause. Say so.
            print(f"no mh commands found in {where}")
            print("Nothing was checked. If that is unexpected, the path is "
                  "probably wrong: pass one explicitly, or set MELLONHEAD_ROOT.")
            return 0
        if not args.quiet:
            print(f"checked {total} mh invocation(s) across {len(files)} "
                  f"file(s) in {where}: all valid")
        return 0

    print(f"{len(problems)} of {total} mh invocation(s) in {where} are not "
          f"valid commands:\n")
    for rel, number, command, reason in problems:
        print(f"  {rel}:{number}")
        print(f"    {command}")
        print(f"    -> {reason}\n")
    print("Run `mh --help`, or `mh task --help`, for the real surface.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
