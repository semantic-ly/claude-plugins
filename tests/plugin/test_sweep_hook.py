"""Tests for `hooks/sweep.sh`, the plugin's async `SessionStart` sweep.

Every test runs the real hook as a subprocess against the real bundled CLI,
which speaks Streamable HTTP to `fakes.FakeMount`. The token sits in the fake
keychain, whose directory leads the hook's `PATH`. The hook's stdout, stderr
and the mount's recorded calls are what is asserted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from test_session_start import PLUGIN, SESSION_START, SessionStartTestCase, context, fakes, line

SWEEP = PLUGIN / "hooks" / "sweep.sh"
COWORK_PROJECT_DIR = "/private/var/empty"
ACCEPTED = {"status": "accepted", "accepted_through": "u2", "batch": "ib_1"}


def record(uuid: str, kind: str, content: str) -> dict:
    return {"uuid": uuid, "type": kind, "sessionId": "x", "cwd": "/Users/someone",
            "message": {"role": kind, "content": content}}


class SweepTestCase(SessionStartTestCase):
    """The session-start harness, aimed at the sweep and its transcript directory."""

    def setUp(self):
        super().setUp()
        self.transcripts = self.home / "transcripts"
        self.transcripts.mkdir()
        self.mount.script["ingest"] = [fakes.tool_result("cursor", {"cursor": None}),
                                       fakes.tool_result("Accepted.", ACCEPTED)]

    def session_file(self, session: str, *records: dict) -> Path:
        path = self.transcripts / f"{session}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def payload(self) -> str:
        return json.dumps({
            "session_id": "live", "transcript_path": str(self.transcripts / "live.jsonl"),
            "cwd": str(self.project), "hook_event_name": "SessionStart", "source": "startup",
        })

    def run_sweep(self, **env) -> subprocess.CompletedProcess:
        return self.run_hook(SWEEP, **env)

    def ingest_calls(self) -> list[dict]:
        return [c["arguments"] for c in self.tool_calls() if c["name"] == "ingest"]


class Sweep(SweepTestCase):
    def test_the_sweep_runs_over_the_transcripts_directory(self):
        """Drives `sweep.sh`'s locate step, then `ingest --sweep` over the payload's directory."""

        self.bind()
        self.store_token()
        self.session_file("older", record("a1", "user", "one"))
        result = self.run_sweep()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        calls = self.ingest_calls()
        self.assertEqual([c["session_ref"] for c in calls], ["older", "older"])
        self.assertEqual({c["source_surface"] for c in calls}, {"claude-code"})
        self.assertEqual(calls[1]["kind"], "session")
        self.assertEqual(len(calls[1]["slice"]), 1)

    def test_cowork_runs_no_sweep(self):
        """Drives the Cowork guard. The copy's lookup finds a binding, so only the guard stops it."""

        self.bind()
        self.store_token()
        self.session_file("older", record("a1", "user", "one"))
        self.plugin_root = self.plugin_that_finds_the_binding_anywhere()
        result = self.run_hook(self.plugin_root / "hooks" / "sweep.sh",
                               CLAUDE_PROJECT_DIR=COWORK_PROJECT_DIR)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])

    def test_an_unbound_project_sweeps_nothing_and_says_nothing(self):
        self.store_token()
        self.session_file("older", record("a1", "user", "one"))
        result = self.run_sweep()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])

    def test_a_malformed_file_is_said_once_and_never_sent(self):
        (self.project / ".semanticly").write_text("slt_not_a_repo\nsecond\n", encoding="utf-8")
        self.store_token()
        result = self.run_sweep()
        self.assertIn("semanticly[sweep]:", result.stderr)
        self.assertNotIn("slt_not_a_repo", result.stderr)
        self.assertEqual(self.mount.requests, [])


class SweepExitsZero(SweepTestCase):
    """`run_hook` asserts exit 0 and the one-line ceiling on every case."""

    def test_a_refusal_ending_the_sweep_exits_0_with_one_line(self):
        self.bind()
        self.store_token()
        self.session_file("older", record("a1", "user", "one"))
        self.mount.script["ingest"] = fakes.tool_result(
            "no", {"error": {"code": "insufficient_level", "message": "Read only."}},
            is_error=True)
        result = self.run_sweep()
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("semanticly[sweep]:", result.stderr)

    def test_no_stored_token_exits_0_with_one_line(self):
        self.bind()
        self.session_file("older", record("a1", "user", "one"))
        result = self.run_sweep()
        self.assertIn("No token for", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)

    def test_a_payload_with_no_transcript_exits_0_with_one_line(self):
        self.bind()
        self.store_token()
        result = subprocess.run(
            [str(SWEEP)], input="not json", env=self.env(), cwd=str(self.home),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("transcript", result.stderr)
        self.assertEqual(self.mount.requests, [])

    def test_a_cli_that_is_not_executable_exits_0_with_one_line(self):
        copied = self.home / "plugin"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__"))
        (copied / "bin" / "semanticly").chmod(0o644)
        self.plugin_root = copied
        self.bind()
        result = self.run_hook(copied / "hooks" / "sweep.sh")
        self.assertIn("no runnable CLI", result.stderr)


class FallbackLog(SweepTestCase):
    """Folded Minor A. A failed `mktemp` leaves `/dev/null` as the log, and no trap removes it."""

    def shims(self) -> tuple[Path, Path]:
        """A `mktemp` that fails and an `rm` that records its arguments."""

        directory = self.home / "shims"
        directory.mkdir()
        log = self.home / "rm.log"
        (directory / "mktemp").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (directory / "rm").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n', encoding="utf-8")
        for name in ("mktemp", "rm"):
            (directory / name).chmod(0o755)
        return directory, log

    def path_with(self, directory: Path) -> str:
        return f"{directory}{os.pathsep}{self.env()['PATH']}"

    def test_the_sweep_never_removes_the_fallback_log(self):
        self.bind()
        self.store_token()
        directory, log = self.shims()
        self.run_sweep(PATH=self.path_with(directory))
        self.assertNotIn("/dev/null", log.read_text(encoding="utf-8") if log.exists() else "")

    def test_session_start_never_removes_the_fallback_log(self):
        self.bind()
        self.store_token()
        self.answer(context([line("a1", "one")]))
        directory, log = self.shims()
        result = self.run_hook(SESSION_START, PATH=self.path_with(directory))
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        self.assertNotIn("/dev/null", log.read_text(encoding="utf-8") if log.exists() else "")


class EmptyNoBranchMessage(SweepTestCase):
    """Folded Minor B. A `no_branch` with no message makes the encoder emit nothing."""

    def test_session_start_emits_nothing_for_a_message_less_no_branch(self):
        self.bind()
        self.store_token()
        self.mount.script["recall"] = fakes.tool_result(
            "no_branch", {"error": {"code": "no_branch"}}, is_error=True)
        result = self.run_hook(SESSION_START)
        self.assertNotIn('"systemMessage": ""', result.stdout)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
