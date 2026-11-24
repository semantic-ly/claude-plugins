#!/usr/bin/env python3
"""Print what one per-prompt recall needs, or say why the hook calls nothing.

Usage: prompt_inputs.py COWORK < PAYLOAD

COWORK is `1` under Cowork's project dir and empty elsewhere. On success the
script exits 0 and prints the repository, the session id, the notes line and
the query, one per line. The query comes last, so its own newlines arrive
whole. The notes line holds what the hook log should say even when the recall
succeeds, such as a cut prompt, and is empty when there is nothing to say.

It exits EXIT_STOP with one reason on stderr when the hook should call
nothing, a CLI that does not load included, and EXIT_OFF in silence when the
person turned per-prompt recall off. The config rule and the `.semanticly`
rule are the bundled CLI's, loaded and read through `hook_inputs.py`, and the
Cowork reader is `session_repo.py`'s.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

from hook_inputs import EXIT_STOP, Stop, bound_repo, load_cli, read_payload  # noqa: E402
from session_repo import session_repo  # noqa: E402

# `utterance_too_long`'s ceiling, in code points. A longer prompt is queried
# as its head, so a long paste is never refused.
MAX_QUERY_CHARS = 2000

# prompt-recall.sh reads this code and hook_inputs.EXIT_STOP. Any other
# failure is a crash.
EXIT_OFF = 4


def read_prompt(raw: str) -> tuple[str, str, str]:
    """The payload's session id, prompt and transcript path."""

    payload = read_payload(raw, ("session_id", "prompt", "transcript_path"))
    if not payload["session_id"] or not payload["prompt"]:
        raise Stop("the hook payload named no session and prompt; nothing recalled")
    return payload["session_id"], payload["prompt"], payload["transcript_path"]


def pick_repo(cli, cowork: bool, transcript: str) -> str:
    """The Cowork task's recalled repo, or the project's `.semanticly` repo."""

    if cowork:
        repo = session_repo(Path(transcript), cli.SEMANTICLY_FILE_RULE) if transcript else ""
        if not repo:
            raise Stop("this Cowork task named no repository in a recall; nothing recalled")
        return repo
    repo = bound_repo(cli)
    if not repo:
        raise Stop("no .semanticly at or above the project; nothing recalled")
    return repo


def cut_query(prompt: str) -> tuple[str, str]:
    """The prompt's first MAX_QUERY_CHARS code points, and a note when it was cut."""

    if len(prompt) <= MAX_QUERY_CHARS:
        return prompt, ""
    return (prompt[:MAX_QUERY_CHARS],
            f"the prompt was queried as its first {MAX_QUERY_CHARS} characters")


def main(argv: list[str]) -> int:
    try:
        cli = load_cli()
    except Stop as stop:
        print(stop, file=sys.stderr)
        return EXIT_STOP
    on, typo = cli.prompt_recall_setting()
    if not on:
        return EXIT_OFF
    notes = [typo] if typo else []
    try:
        session, prompt, transcript = read_prompt(sys.stdin.read())
        repo = pick_repo(cli, argv[1:2] == ["1"], transcript)
    except Stop as stop:
        print("; ".join([*notes, str(stop)]), file=sys.stderr)
        return EXIT_STOP
    query, cut = cut_query(prompt)
    if cut:
        notes.append(cut)
    sys.stdout.write("\n".join([repo, session, "; ".join(notes), query]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
