#!/usr/bin/env python3
"""Print the repository a Cowork task's own `recall` named, or nothing.

Usage: session_repo.py TRANSCRIPT

A Cowork task has no `.semanticly`, so the prompt and capture hooks take its
repository from the transcript. It is the `input.repo` of the first `tool_use`
block that calls the Semantic.ly connector's `recall` tool with a well-formed
`org/slug`. The block's `name` must read `mcp__<server>__recall`, and the
server part must spell `semanticly` once punctuation and case are dropped, so
another server's `recall` tool names nothing.
The rule is the CLI's own `.semanticly` rule, loaded from the bundled CLI, so
the two never disagree on what a repository name is.

An unreadable transcript prints nothing and exits 0, and the read stops at
the first line that does not parse, as the CLI's slicer does. A CLI that does
not load exits hook_inputs.EXIT_STOP with one reason on stderr.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from hook_inputs import EXIT_STOP, Stop, load_cli  # noqa: E402

RECALL_TOOL = re.compile(r"mcp__(?P<server>.+)__recall")
CONNECTOR = "semanticly"


def is_recall_tool(name: object) -> bool:
    """Whether `name` is the Semantic.ly connector's `recall` tool."""

    found = RECALL_TOOL.fullmatch(name) if isinstance(name, str) else None
    if found is None:
        return False
    server = "".join(ch for ch in found.group("server").lower() if ch.isalnum())
    return CONNECTOR in server


def well_formed(repo: object, rule) -> bool:
    return isinstance(repo, str) and "\n" not in repo and bool(rule.fullmatch(repo))


def recall_repos(record: object):
    """Each `input.repo` a transcript row's Semantic.ly `recall` calls carry, in order."""

    message = record.get("message") if isinstance(record, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "tool_use"
                and is_recall_tool(block.get("name"))
                and isinstance(block.get("input"), dict)):
            yield block["input"].get("repo")


def session_repo(transcript: Path, rule) -> str:
    """The first well-formed repo a Semantic.ly `recall` call in `transcript` named, or ""."""

    try:
        with open(transcript, "rb") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return ""
                for repo in recall_repos(record):
                    if well_formed(repo, rule):
                        return repo
    except OSError:
        return ""
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 0
    try:
        cli = load_cli()
    except Stop as stop:
        print(stop, file=sys.stderr)
        return EXIT_STOP
    repo = session_repo(Path(argv[1]), cli.SEMANTICLY_FILE_RULE)
    if repo:
        print(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
