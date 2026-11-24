#!/usr/bin/env python3
"""Print what a session-start or capture hook reads before it calls the CLI.

Usage: hook_inputs.py HOOK [COWORK] < PAYLOAD

HOOK names the hook. COWORK is `1` under Cowork's project dir and empty
elsewhere, and only `ingest` reads it. On success the script exits 0 and
prints the hook's fields, one per line.

- session-start: the repository, then the session id
- sweep: the repository, then the transcript's directory
- ingest: the session id, the transcript path, then the repository
- memory: the session id, the memory directory, then the repository

The repository is empty when no `.semanticly` binds the project. Under Cowork
`ingest` leaves it empty, because a Cowork task names its repository in a
recall and `session_repo.py` reads it from the transcript.

It exits EXIT_STOP with one reason on stderr when the hook should call
nothing. Every hook's Python loads the bundled CLI through `load_cli`, so a
CLI that does not load costs one log line and never a traceback.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

sys.dont_write_bytecode = True

CLI_PATH = Path(__file__).resolve().parents[1] / "bin" / "semanticly"

# The hooks read this code. Any other failure is a crash.
EXIT_STOP = 3


class Stop(Exception):
    """A reason the hook calls nothing, worded for the hook log."""


def load_cli():
    """The bundled CLI as a module. A CLI that does not load raises Stop."""

    try:
        loader = SourceFileLoader("semanticly_cli", str(CLI_PATH))
        module = module_from_spec(spec_from_loader(loader.name, loader))
        loader.exec_module(module)
    except Exception as exc:
        detail = (str(exc).splitlines() or [""])[0]
        raise Stop(f"the bundled CLI did not load ({type(exc).__name__}: {detail}); "
                   "the hook did nothing") from exc
    return module


def read_payload(raw: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Each of `keys` from the hook payload, stripped, or "" when absent."""

    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    return {key: str(payload.get(key) or "").strip() for key in keys}


def bound_repo(cli) -> str:
    """The repository the project's `.semanticly` names, or "" when none does.

    The lookup and the file's rule are the CLI's own, so a malformed file is
    refused as the CLI refuses it and its content never reaches an argv.
    """

    try:
        marker = cli.find_semanticly(cli.project_root())
        return cli.read_semanticly(marker) if marker else ""
    except cli.CliError as exc:
        raise Stop(str(exc)) from exc


def require_session_and_transcript(payload: dict[str, str]) -> None:
    if not payload["session_id"] or not payload["transcript_path"]:
        raise Stop("the hook payload named no session and transcript; nothing captured")


def session_start(raw: str, cowork: bool) -> list[str]:
    repo = bound_repo(load_cli())
    return [repo, read_payload(raw, ("session_id",))["session_id"]]


def sweep(raw: str, cowork: bool) -> list[str]:
    repo = bound_repo(load_cli())
    transcript = read_payload(raw, ("transcript_path",))["transcript_path"]
    return [repo, os.path.dirname(transcript) if transcript else ""]


def ingest(raw: str, cowork: bool) -> list[str]:
    payload = read_payload(raw, ("session_id", "transcript_path"))
    require_session_and_transcript(payload)
    repo = "" if cowork else bound_repo(load_cli())
    return [payload["session_id"], payload["transcript_path"], repo]


def memory(raw: str, cowork: bool) -> list[str]:
    payload = read_payload(raw, ("session_id", "transcript_path"))
    require_session_and_transcript(payload)
    repo = bound_repo(load_cli())
    directory = os.path.join(os.path.dirname(payload["transcript_path"]), "memory")
    return [payload["session_id"], directory, repo]


HOOKS = {"session-start": session_start, "sweep": sweep, "ingest": ingest, "memory": memory}


def main(argv: list[str]) -> int:
    reader = HOOKS.get(argv[1] if len(argv) > 1 else "")
    if reader is None:
        print(f"usage: hook_inputs.py {{{','.join(HOOKS)}}} [COWORK]", file=sys.stderr)
        return 2
    try:
        fields = reader(sys.stdin.read(), argv[2:3] == ["1"])
    except Stop as stop:
        print(stop, file=sys.stderr)
        return EXIT_STOP
    sys.stdout.write("".join(field + "\n" for field in fields))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
