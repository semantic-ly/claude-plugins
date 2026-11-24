"""Tests for `hooks/session-start.sh`, the plugin's `SessionStart` hook.

Every test runs the real hook as a subprocess against the real bundled CLI,
which speaks Streamable HTTP to `fakes.FakeMount`. The token sits in the fake
keychain, whose directory leads the hook's `PATH`, so the CLI reads it through
its own keychain road and never reaches the real one. The wire bytes the mount
serves are the setup, and the hook's stdout and stderr are what is asserted.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "semanticly"
SESSION_START = PLUGIN / "hooks" / "session-start.sh"
CLI_PATH = PLUGIN / "bin" / "semanticly"

sys.path.insert(0, str(REPO_ROOT / "tests" / "cli"))
import fakes  # noqa: E402


def load_cli():
    loader = SourceFileLoader("semanticly_plugin_cli", str(CLI_PATH))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


cli = load_cli()

REPO = "acme/support"
SESSION = "sess-start-1"
COWORK_PROJECT_DIR = "/private/var/empty"
UNBOUND = "Semantic.ly isn't bound to this project; run `/semanticly:init`."
READ_ONLY = f"You hold read on `{REPO}`, so this session's conversation is not captured."
CLI_NO_BRANCH = ("This connection has no branch for `acme/support`. Choose one at "
                 "http://console.test/acme/support/-/connections, or run `semanticly init` "
                 "in a terminal.")
WRITES_URL = "http://console.test/acme/support/memory?branch=team%2Fsupport"
HOSTILE_FACT = (
    'The service replies {"ok": true} on \\healthy, and\n'
    'the operator said "ship it" — mind the trailing backslash \\'
)


def line(sha: str, body: str, label: str = "standing") -> dict:
    """One `/context` line as the server prints it."""

    return {"n": 1, "layer": "standing", "label": label, "display_id": "F-1",
            "sha": sha, "body": body, "repo": REPO}


def writes(awaiting: int = 0, quota: int = 0, proposals: int = 0,
           url: str = WRITES_URL) -> dict:
    return {"awaiting_review": awaiting, "waiting_on_quota": quota,
            "open_proposals": proposals, "url": url}


def context(lines: list[dict], *, level: str = "write",
            recent_writes: dict | None = None) -> dict:
    """A `/context` body, which `recall` relays as its structured content."""

    body = {"lines": lines, "text": "\n".join(f"[standing] {row['body']}" for row in lines),
            "level": level}
    if recent_writes is not None:
        body["recent_writes"] = recent_writes
    return body


def closed_port_origin() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class SessionStartTestCase(unittest.TestCase):
    """Runs the hook the way Claude Code runs it, with nothing inherited."""

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        self.project = self.home / "project"
        self.project.mkdir()
        self.keychain = fakes.keychain_shim(self.home / "keychain-bin")
        self.plugin_root = PLUGIN
        self.mount = fakes.FakeMount({"recall": fakes.tool_result("", context([]))})
        self.addCleanup(self.mount.close)
        self.origin = self.mount.url

    def bind(self, repo: str = REPO) -> None:
        (self.project / ".semanticly").write_text(repo + "\n", encoding="utf-8")

    def store_token(self, repo: str = REPO, token: str = "slt_test") -> None:
        """Writes the fake keychain's entry under the account the CLI composes."""

        account = cli.keychain_account(repo, self.origin)
        state = {"entries": {f"{cli.KEYCHAIN_SERVICE}\n{account}": token}, "calls": []}
        self.keychain.store.write_text(json.dumps(state), encoding="utf-8")

    def plugin_that_finds_the_binding_anywhere(self) -> Path:
        """A plugin copy whose CLI looks for `.semanticly` from the project.

        Under Cowork's `/private/var/empty` no file can be bound, so the
        lookup alone would end a hook there. This copy makes the lookup
        succeed, which leaves the hook's own Cowork guard as the only thing
        that can stop it.
        """

        copied = self.home / "plugin"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__"))
        source = (copied / "bin" / "semanticly").read_text(encoding="utf-8")
        probe = "    return Path(override) if override else Path.cwd()\n"
        assert probe in source
        patched = source.replace(probe, f"    return Path({str(self.project)!r})\n")
        (copied / "bin" / "semanticly").write_text(patched, encoding="utf-8")
        return copied

    def answer(self, body: dict) -> None:
        self.mount.script["recall"] = fakes.tool_result(body.get("text", ""), body)

    def env(self, **overrides) -> dict:
        """A clean environment. `None` removes a key."""

        base = {
            "PATH": f"{self.keychain.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "SEMANTICLY_HOME": str(self.home / "semanticly"),
            "CLAUDE_PLUGIN_ROOT": str(self.plugin_root),
            "CLAUDE_PLUGIN_OPTION_API_URL": self.origin,
            "CLAUDE_PROJECT_DIR": str(self.project),
        }
        for key, value in overrides.items():
            if value is None:
                base.pop(key, None)
            else:
                base[key] = value
        return base

    def payload(self) -> str:
        return json.dumps({
            "session_id": SESSION, "transcript_path": str(self.home / "t.jsonl"),
            "cwd": str(self.project), "hook_event_name": "SessionStart", "source": "startup",
        })

    def run_hook(self, script: Path = SESSION_START, **env) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(script)], input=self.payload(), env=self.env(**env), cwd=str(self.home),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stderr.splitlines()), 1, result.stderr)
        return result

    def output(self, result: subprocess.CompletedProcess) -> dict:
        return json.loads(result.stdout)

    def tool_calls(self) -> list[dict]:
        return [r["body"]["params"] for r in self.mount.requests
                if r["method"] == "POST" and (r["body"] or {}).get("method") == "tools/call"]


class Unbound(SessionStartTestCase):
    """Production path: the hook's `.semanticly` lookup from `CLAUDE_PROJECT_DIR`."""

    def test_an_unbound_project_gets_one_system_message_and_no_context(self):
        self.store_token()
        result = self.run_hook()
        self.assertEqual(self.output(result), {"systemMessage": UNBOUND})
        self.assertEqual(self.mount.requests, [])

    def test_cowork_gets_no_reminder(self):
        """Drives the guard on the unbound branch. Without it the hook emits the reminder."""

        self.store_token()
        result = self.run_hook(CLAUDE_PROJECT_DIR=COWORK_PROJECT_DIR)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.mount.requests, [])

    def test_the_nearest_file_above_the_project_dir_binds_it(self):
        self.bind()
        nested = self.project / "src" / "deep"
        nested.mkdir(parents=True)
        self.store_token()
        self.answer(context([line("a1", "Refunds run 30 days.")]))
        result = self.run_hook(CLAUDE_PROJECT_DIR=str(nested))
        self.assertIn("Refunds run 30 days.",
                      self.output(result)["hookSpecificOutput"]["additionalContext"])

    def test_a_malformed_file_is_said_on_stderr_and_never_sent(self):
        (self.project / ".semanticly").write_text("slt_not_a_repo_at_all\nsecond\n",
                                                  encoding="utf-8")
        self.store_token()
        result = self.run_hook()
        self.assertEqual(result.stdout, "")
        self.assertIn(".semanticly", result.stderr)
        self.assertNotIn("slt_not_a_repo_at_all", result.stderr)
        self.assertEqual(self.mount.requests, [])


class Request(SessionStartTestCase):
    """Production path: the hook's argv to the CLI, read off the mount's recording."""

    def test_the_request_is_an_empty_query_with_standing_and_no_budget(self):
        self.bind()
        self.store_token()
        self.run_hook()
        calls = self.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {"name": "recall", "arguments": {
            "repo": REPO, "standing": True, "session_ref": SESSION}})
        init = next(r for r in self.mount.requests
                    if (r["body"] or {}).get("method") == "initialize")
        self.assertEqual(init["body"]["params"]["clientInfo"]["name"], "claude-code")
        self.assertEqual(init["headers"]["authorization"], "Bearer slt_test")

    def test_the_session_file_records_the_level_and_the_standing_ids(self):
        self.bind()
        self.store_token()
        self.answer(context([line("a1", "one"), line("b2", "two")]))
        self.run_hook()
        path = self.home / "semanticly" / "checkpoints" / f"{SESSION}.injected.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((recorded["repo"], recorded["level"], recorded["standing"]),
                         (REPO, "write", ["a1", "b2"]))

    def test_no_environment_variable_names_the_repo_or_the_budget(self):
        text = SESSION_START.read_text(encoding="utf-8")
        for name in ("SEMANTICLY_REPO", "SEMANTICLY_SESSION_BUDGET", "--token-budget"):
            self.assertNotIn(name, text)


class Output(SessionStartTestCase):
    """Production path: the hook's one encoder, fed by the real CLI's `--json`."""

    def setUp(self):
        super().setUp()
        self.bind()
        self.store_token()

    def test_the_block_goes_to_additional_context_and_the_receipt_to_system_message(self):
        self.answer(context([line("a1", "Refunds run 30 days.")],
                            recent_writes=writes(awaiting=3, quota=2, proposals=1)))
        out = self.output(self.run_hook())
        self.assertEqual(sorted(out), ["hookSpecificOutput", "systemMessage"])
        inner = out["hookSpecificOutput"]
        self.assertEqual(inner["hookEventName"], "SessionStart")
        header, text = inner["additionalContext"].split("\n\n", 1)
        self.assertIn(f"`{REPO}`", header)
        self.assertIn("UNREVIEWED marks a fact on this connection's bound branch that "
                      "nobody has reviewed", header)
        self.assertEqual(text, "[standing] Refunds run 30 days.")
        self.assertEqual(out["systemMessage"],
                         "3 facts on `team/support` await review · 2 slices waiting on quota"
                         f" · 1 open proposal — {WRITES_URL}")

    def test_a_count_of_one_reads_in_the_singular(self):
        self.answer(context([], recent_writes=writes(awaiting=1, quota=1, proposals=2)))
        out = self.output(self.run_hook())
        self.assertEqual(out["systemMessage"],
                         "1 fact on `team/support` awaits review · 1 slice waiting on quota"
                         f" · 2 open proposals — {WRITES_URL}")

    def test_a_block_with_no_lines_adds_no_context(self):
        self.answer(context([], recent_writes=writes(quota=2)))
        out = self.output(self.run_hook())
        self.assertEqual(out, {"systemMessage": f"2 slices waiting on quota — {WRITES_URL}"})

    def test_a_zero_receipt_says_nothing(self):
        self.answer(context([line("a1", "one")], recent_writes=writes()))
        out = self.output(self.run_hook())
        self.assertEqual(list(out), ["hookSpecificOutput"])

        self.answer(context([], recent_writes=writes()))
        self.assertEqual(self.run_hook().stdout, "")

    def test_no_branch_is_relayed_verbatim_and_nothing_injected(self):
        envelope = {"error": {"code": "no_branch", "message": CLI_NO_BRANCH}}
        self.mount.script["recall"] = fakes.tool_result(
            f"no_branch: {CLI_NO_BRANCH}", envelope, is_error=True)
        result = self.run_hook()
        self.assertEqual(self.output(result), {"systemMessage": CLI_NO_BRANCH})
        self.assertFalse((self.home / "semanticly" / "checkpoints").exists())

    def test_a_read_level_session_says_capture_is_off(self):
        """Review Focus 1. The block still lands, and the person is told."""

        self.answer(context([line("a1", "Refunds run 30 days.")], level="read"))
        out = self.output(self.run_hook())
        self.assertIn("Refunds run 30 days.", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(out["systemMessage"], READ_ONLY)

    def test_a_fact_holding_quotes_newlines_and_backslashes_survives(self):
        self.answer(context([line("dead", HOSTILE_FACT)]))
        context_text = self.output(self.run_hook())["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context_text.endswith("[standing] " + HOSTILE_FACT))


class ExitsZero(SessionStartTestCase):
    """Each case breaks one thing the hook depends on. It exits 0 with one line.

    `run_hook` asserts the exit status and the one-line ceiling on every case.
    """

    def assert_one_line(self, result, needle: str):
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("semanticly[session-start]:", result.stderr)
        self.assertIn(needle, result.stderr)

    def test_a_revoked_token_exits_0_with_one_line(self):
        """Review Focus 2. The mount's 401 reaches the log as one line."""

        self.bind()
        self.store_token()
        self.mount.script["initialize"] = fakes.http_answer(
            401, {"error": {"code": "invalid_token", "message": "The token was revoked."}})
        self.assert_one_line(self.run_hook(), "invalid_token")
        self.assertFalse((self.home / "semanticly" / "checkpoints").exists())

    def test_no_stored_token_exits_0_with_one_line(self):
        self.bind()
        self.assert_one_line(self.run_hook(), "semanticly login")
        self.assertEqual(self.mount.requests, [])

    def test_an_unreachable_api_exits_0_with_one_line(self):
        self.bind()
        self.origin = closed_port_origin()
        self.store_token()
        self.assert_one_line(self.run_hook(), "Could not reach")

    def test_a_cli_that_is_not_executable_exits_0_with_one_line(self):
        copied = self.home / "plugin"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__"))
        (copied / "bin" / "semanticly").chmod(0o644)
        self.plugin_root = copied
        self.bind()
        result = self.run_hook(copied / "hooks" / "session-start.sh")
        self.assert_one_line(result, "no runnable CLI")

    def test_an_unset_plugin_root_still_reaches_the_cli(self):
        """`$0` is absolute, so the script finds its own CLI without the variable."""

        self.bind()
        self.store_token()
        self.answer(context([line("a1", "one")]))
        result = self.run_hook(CLAUDE_PLUGIN_ROOT=None)
        self.assertIn("hookSpecificOutput", self.output(result))

    def test_a_payload_with_no_session_exits_0_with_one_line(self):
        self.bind()
        self.store_token()
        result = subprocess.run(
            [str(SESSION_START)], input="not json", env=self.env(), cwd=str(self.home),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_one_line(result, "session")
        self.assertEqual(self.mount.requests, [])


if __name__ == "__main__":
    unittest.main()
