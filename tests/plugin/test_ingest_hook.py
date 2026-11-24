"""Tests for `hooks/ingest.sh`, the plugin's capture hook.

Every test runs the real hook as a subprocess against the real bundled CLI,
which speaks Streamable HTTP to `fakes.FakeMount`. The token sits in the fake
keychain, whose directory leads the hook's `PATH`. The session's recorded
level comes from a real `recall --session` call through the bundled CLI, the
road the session-start and prompt hooks take. The mount's recorded `ingest`
arguments are the bytes the hook caused to be sent, and the hook log is its
one stderr line.

`tests/testdata/ingest_slice.json` is a cross-suite contract. This file reads
its `transcript` and `wire`. The server's tests read `wire` and `slice` from
their own copy, so a change here needs the same change there.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_session_start import (
    CLI_NO_BRANCH,
    CLI_PATH,
    COWORK_PROJECT_DIR,
    PLUGIN,
    REPO,
    REPO_ROOT,
    SESSION,
    SessionStartTestCase,
    context,
    fakes,
)

INGEST = PLUGIN / "hooks" / "ingest.sh"
SESSION_REPO = PLUGIN / "hooks" / "session_repo.py"
FIXTURE = json.loads(
    (REPO_ROOT / "tests" / "testdata" / "ingest_slice.json").read_text(encoding="utf-8"))
OTHER_REPO = "acme/other"
ACCEPTED = {"id": "ib_1", "repo": REPO, "branch": "agent/claude-code/ci",
            "accepted_through": "u3", "status": "queued",
            "note": "extraction is queued; whatever it keeps lands on agent/claude-code/ci"}
GATE_READ = (f"semanticly[ingest]: this session holds `read` on `{REPO}`, "
             "so its transcript is not captured.")


def record(uuid: str | None, kind: str, content) -> dict:
    """One Claude Code transcript row, carrying the local fields a real row carries."""

    row = {"type": kind, "sessionId": "x", "cwd": "/Users/someone",
           "message": {"role": kind, "content": content}}
    if uuid is not None:
        row["uuid"] = uuid
    return row


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def wire(uuid: str, role: str, value: str) -> dict:
    return {"uuid": uuid, "role": role, "content": [text(value)]}


def recall_call(repo: object, name: str = "mcp__semanticly__recall") -> dict:
    """An assistant row whose tool call names `repo`, as a Cowork task's recall does."""

    return record("t1", "assistant", [
        text("Let me recall."),
        {"type": "tool_use", "id": "toolu_1", "name": name,
         "input": {"repo": repo, "query": "refunds"}}])


def cursor(value: str | None) -> dict:
    return fakes.tool_result("cursor", {"cursor": value})


THREE = (record("u1", "user", "one"), record("u2", "assistant", [text("two")]),
         record("u3", "user", "three"))


class IngestHookTestCase(SessionStartTestCase):
    """The session-start harness, aimed at the capture hook and its transcript."""

    def setUp(self):
        super().setUp()
        self.transcript = self.home / "transcript.jsonl"
        self.payload_extra: dict = {}
        self.answers(cursor(None), fakes.tool_result("Accepted.", ACCEPTED))

    def payload(self) -> str:
        body = {"session_id": SESSION, "transcript_path": str(self.transcript),
                "cwd": str(self.project), "hook_event_name": "SessionEnd"}
        body.update(self.payload_extra)
        return json.dumps(body)

    def answers(self, *entries: dict) -> None:
        """What the `ingest` calls answer, in order."""

        self.mount.script["ingest"] = list(entries)

    def write_transcript(self, *records: dict) -> None:
        self.transcript.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8")

    def record_level(self, level: str, repo: str = REPO) -> None:
        """A `recall --session` through the bundled CLI that answers `level` for `repo`."""

        self.answer(context([], level=level))
        result = subprocess.run(
            [str(CLI_PATH), "recall", "--repo", repo, "--session", SESSION, "--standing"],
            env=self.env(), cwd=str(self.home), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.mount.requests.clear()

    def ready(self, level: str = "write") -> None:
        """A bound project, its token and a recorded level."""

        self.bind()
        self.store_token()
        self.record_level(level)

    def run_ingest(self, script: Path = INGEST, **env) -> subprocess.CompletedProcess:
        return self.run_hook(script, **env)

    def ingest_calls(self) -> list[dict]:
        return [c["arguments"] for c in self.tool_calls() if c["name"] == "ingest"]

    def posted_slice(self) -> list[dict]:
        posts = [c for c in self.ingest_calls() if "slice" in c]
        self.assertEqual(len(posts), 1, self.ingest_calls())
        return posts[0]["slice"]


class SharedFixture(IngestHookTestCase):
    """The producer half of `ingest_slice.json`."""

    def test_the_hook_posts_the_shared_fixtures_wire(self):
        """Drives `ingest.sh` → `ingest --transcript` over the fixture's `transcript`.

        Equality on whole messages, so a local field riding along beside
        `uuid`, `role` and `content` fails here.
        """

        self.ready()
        self.write_transcript(*FIXTURE["transcript"])
        self.run_ingest()
        read, post = self.ingest_calls()
        self.assertEqual(read, {"repo": REPO, "session_ref": SESSION,
                                "source_surface": "claude-code"})
        self.assertEqual(post, {"repo": REPO, "session_ref": SESSION, "kind": "session",
                                "source_surface": "claude-code", "slice": FIXTURE["wire"]})

    def test_the_shared_fixtures_transcript_is_still_adversarial(self):
        """Guards the test above from passing on a transcript already in wire shape."""

        rows = FIXTURE["transcript"]
        contents = [row["message"]["content"] for row in rows]
        block_types = {b.get("type") for c in contents if isinstance(c, list) for b in c}
        self.assertLessEqual({"tool_use", "tool_result"}, block_types)
        self.assertTrue(block_types - {"text", "tool_use", "tool_result"},
                        "no row carries a non-text block beside the tool blocks")
        self.assertTrue(any("uuid" not in row for row in rows),
                        "every row carries a uuid")
        self.assertTrue(any(isinstance(c, str) for c in contents))
        carried = set().union(*(set(row) for row in rows))
        self.assertLessEqual({"cwd", "sessionId", "parentUuid", "version"}, carried)


class Capture(IngestHookTestCase):
    def test_an_accepted_post_logs_how_far_it_reaches_and_its_batch(self):
        self.ready()
        self.write_transcript(*THREE)
        result = self.run_ingest()
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr,
                         "semanticly[ingest]: accepted through u3 (batch ib_1)\n")
        self.assertEqual(self.posted_slice(), [wire("u1", "user", "one"),
                                               wire("u2", "assistant", "two"),
                                               wire("u3", "user", "three")])

    def test_only_what_follows_the_servers_cursor_is_posted(self):
        """Drives the CLI's cut at the cursor the server answered."""

        self.ready()
        self.write_transcript(*THREE)
        self.answers(cursor("u1"), fakes.tool_result("Accepted.", ACCEPTED))
        self.run_ingest()
        post = self.ingest_calls()[1]
        self.assertEqual(post["after"], "u1")
        self.assertEqual([m["uuid"] for m in post["slice"]], ["u2", "u3"])

    def test_nothing_past_the_cursor_posts_nothing_and_logs_the_cursor(self):
        self.ready()
        self.write_transcript(*THREE)
        self.answers(cursor("u3"))
        result = self.run_ingest()
        self.assertEqual(result.stderr, "semanticly[ingest]: nothing new after u3\n")
        self.assertEqual(len(self.ingest_calls()), 1)

    def test_a_refused_batch_logs_its_reason(self):
        """Drives the hook's log of the CLI's `--json` body through `ingest_headline`."""

        self.ready()
        self.write_transcript(*THREE)
        self.answers(cursor(None), fakes.tool_result("Refused.", {
            "id": "ib_1", "status": "refused", "refused_reason": "stranded",
            "note": "this slice was already accepted", "cursor": None}))
        result = self.run_ingest()
        self.assertEqual(result.stderr, "semanticly[ingest]: refused: stranded\n")

    def test_no_branch_ends_at_exit_0_with_the_sentence_and_no_post(self):
        """The cursor read's refusal reaches the log verbatim, and no slice is sent."""

        self.ready()
        self.write_transcript(*THREE)
        self.answers(fakes.tool_result(
            CLI_NO_BRANCH, {"error": {"code": "no_branch", "message": CLI_NO_BRANCH}},
            is_error=True))
        result = self.run_ingest()
        self.assertEqual(result.stderr, f"semanticly[ingest]: no_branch — {CLI_NO_BRANCH}\n")
        self.assertEqual(len(self.ingest_calls()), 1)
        self.assertNotIn("slice", self.ingest_calls()[0])

    def test_trigger_and_reason_change_nothing(self):
        """PreCompact's `trigger` and SessionEnd's `reason` reach no argument."""

        self.ready()
        self.write_transcript(*THREE)
        posted = []
        for extra in ({"hook_event_name": "PreCompact", "trigger": "manual"},
                      {"hook_event_name": "PreCompact", "trigger": "auto"},
                      {"hook_event_name": "SessionEnd", "reason": "clear"},
                      {"hook_event_name": "SessionEnd", "reason": "logout"}):
            self.payload_extra = extra
            self.mount.requests.clear()
            self.answers(cursor(None), fakes.tool_result("Accepted.", ACCEPTED))
            self.run_ingest()
            posted.append(self.ingest_calls())
        self.assertTrue(all(calls == posted[0] for calls in posted), posted)


class BelowWrite(IngestHookTestCase):
    """Review Focus 1. A session below `write` opens no transcript and sends nothing."""

    def watch_for_a_reader(self, fifo: Path) -> tuple[threading.Event, threading.Event]:
        """Flags `opened` when anything opens `fifo` to read it."""

        opened, stop = threading.Event(), threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        raise
                    stop.wait(0.005)
                    continue
                opened.set()
                os.close(fd)
                return

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        return opened, stop

    def test_a_session_below_write_opens_no_transcript(self):
        """Drives the CLI's capture gate, which `ingest.sh` reaches before any read."""

        self.ready(level="read")
        os.mkfifo(self.transcript)
        opened, stop = self.watch_for_a_reader(self.transcript)
        result = self.run_ingest()
        stop.set()
        self.assertFalse(opened.is_set(), "the transcript was opened")
        self.assertEqual(result.stderr, GATE_READ + "\n")
        self.assertEqual(self.mount.requests, [])

    def test_no_recorded_level_sends_nothing(self):
        self.bind()
        self.store_token()
        self.write_transcript(*THREE)
        result = self.run_ingest()
        self.assertIn("has no recorded level", result.stderr)
        self.assertEqual(self.mount.requests, [])


class Cowork(IngestHookTestCase):
    """The Cowork arm. Each test runs a plugin copy whose `.semanticly` lookup succeeds.

    Under `/private/var/empty` no file can be bound, so the lookup alone would
    send nothing. The copy finds a binding, which leaves the hook's Cowork arm
    as the only thing that picks the repository.
    """

    def setUp(self):
        super().setUp()
        self.plugin_root = self.plugin_that_finds_the_binding_anywhere()

    def run_cowork(self) -> subprocess.CompletedProcess:
        return self.run_ingest(self.plugin_root / "hooks" / "ingest.sh",
                               CLAUDE_PROJECT_DIR=COWORK_PROJECT_DIR)

    def test_cowork_posts_to_the_repo_its_recall_named_with_surface_cowork(self):
        """Drives `session_repo.py` over the transcript, then `ingest --surface cowork`."""

        self.bind(OTHER_REPO)
        self.store_token(REPO)
        self.record_level("write", REPO)
        self.write_transcript(record("c1", "user", "What is the refund window?"),
                              recall_call(REPO), record("c2", "assistant", "Thirty days."))
        self.run_cowork()
        calls = self.ingest_calls()
        self.assertEqual({c["repo"] for c in calls}, {REPO})
        self.assertEqual({c["source_surface"] for c in calls}, {"cowork"})
        self.assertEqual([m["uuid"] for m in self.posted_slice()], ["c1", "t1", "c2"])
        names = {r["body"]["params"]["clientInfo"]["name"] for r in self.mount.requests
                 if r["method"] == "POST" and r["body"].get("method") == "initialize"}
        self.assertEqual(names, {"cowork"})

    def test_a_cowork_task_that_named_no_repo_posts_nothing(self):
        """The bound repo has a write level recorded, so only the Cowork arm stops the post."""

        self.ready()
        self.write_transcript(record("c1", "user", "hello"),
                              recall_call("Not A Repo"),
                              recall_call(REPO, name="mcp__semanticly__remember"))
        result = self.run_cowork()
        self.assertEqual(self.mount.requests, [])
        self.assertEqual(result.stderr,
                         "semanticly[ingest]: this Cowork task named no repository in a "
                         "recall; nothing captured\n")


class SessionRepo(unittest.TestCase):
    """Production path: `session_repo.py TRANSCRIPT` as the hooks run it."""

    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.transcript = self.scratch / "t.jsonl"

    def repo_of(self, *records: dict | str) -> subprocess.CompletedProcess:
        self.transcript.write_text("".join(
            (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records),
            encoding="utf-8")
        return subprocess.run(["python3", str(SESSION_REPO), str(self.transcript)],
                              capture_output=True, text=True, timeout=30)

    def test_the_first_well_formed_recall_names_the_repo(self):
        result = self.repo_of(recall_call("Bad/Repo"), recall_call(f"{REPO}\n"),
                              recall_call(OTHER_REPO, name="mcp__semanticly__remember"),
                              recall_call(REPO), recall_call(OTHER_REPO))
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, REPO + "\n", ""))

    def test_another_servers_recall_tool_names_nothing(self):
        for name in ("mcp__evil__recall", "evil__recall", "__recall", "recall",
                     "mcp__semanticly__recall_all"):
            with self.subTest(name=name):
                result = self.repo_of(recall_call(OTHER_REPO, name=name))
                self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_each_spelling_of_the_connector_names_the_repo(self):
        for name in ("mcp__semanticly__recall", "mcp__plugin_semanticly_semanticly__recall",
                     "mcp__Semantic_ly__recall", "mcp__claude_ai_Semantic_ly__recall"):
            with self.subTest(name=name):
                result = self.repo_of(recall_call(REPO, name=name))
                self.assertEqual((result.returncode, result.stdout), (0, REPO + "\n"))

    def test_a_transcript_with_no_recall_prints_nothing(self):
        result = self.repo_of(record("u1", "user", "hi"))
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_the_read_stops_at_a_line_that_does_not_parse(self):
        result = self.repo_of(record("u1", "user", "hi"), "{partial", recall_call(REPO))
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_a_missing_transcript_prints_nothing(self):
        result = subprocess.run(["python3", str(SESSION_REPO), str(self.scratch / "none")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


class ExitsZero(IngestHookTestCase):
    """Each case breaks one thing the hook depends on. `run_hook` asserts exit 0 and one line."""

    def test_an_unbound_project_sends_nothing_and_says_nothing(self):
        self.store_token()
        self.write_transcript(*THREE)
        result = self.run_ingest()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])

    def test_a_malformed_file_is_said_once_and_never_sent(self):
        (self.project / ".semanticly").write_text("slt_not_a_repo\nsecond\n", encoding="utf-8")
        self.store_token()
        result = self.run_ingest()
        self.assertIn("semanticly[ingest]:", result.stderr)
        self.assertNotIn("slt_not_a_repo", result.stderr)
        self.assertEqual(self.mount.requests, [])

    def test_a_malformed_payload_exits_0_with_one_line(self):
        self.ready()
        result = subprocess.run([str(INGEST)], input="not json", env=self.env(),
                                cwd=str(self.home), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("session", result.stderr)
        self.assertEqual(self.mount.requests, [])

    def test_a_payload_without_a_session_exits_0_with_one_line(self):
        self.ready()
        self.payload_extra = {"session_id": ""}
        result = self.run_ingest()
        self.assertIn("named no session", result.stderr)
        self.assertEqual(self.mount.requests, [])

    def test_a_missing_transcript_exits_0_with_one_line(self):
        self.ready()
        result = self.run_ingest()
        self.assertIn(str(self.transcript), result.stderr)
        self.assertEqual(self.ingest_calls(), [])

    def test_no_stored_token_exits_0_with_one_line(self):
        self.ready()
        self.keychain.store.write_text(json.dumps({"entries": {}, "calls": []}),
                                       encoding="utf-8")
        self.write_transcript(*THREE)
        result = self.run_ingest()
        self.assertIn("No token for", result.stderr)
        self.assertEqual(self.mount.requests, [])

    def test_a_revoked_token_exits_0_with_one_line(self):
        """Review Focus 2. The mount's 401 reaches the log as one line."""

        self.ready()
        self.write_transcript(*THREE)
        self.mount.script["initialize"] = fakes.http_answer(
            401, {"error": {"code": "invalid_token", "message": "The token was revoked."}})
        result = self.run_ingest()
        self.assertIn("invalid_token", result.stderr)
        self.assertEqual(self.ingest_calls(), [])

    def test_a_cli_that_is_not_executable_exits_0_with_one_line(self):
        copied = self.home / "plugin"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__"))
        (copied / "bin" / "semanticly").chmod(0o644)
        self.plugin_root = copied
        self.bind()
        result = self.run_ingest(copied / "hooks" / "ingest.sh")
        self.assertIn("no runnable CLI", result.stderr)

    def test_an_unset_plugin_root_still_reaches_the_cli(self):
        self.ready()
        self.write_transcript(*THREE)
        result = self.run_ingest(CLAUDE_PLUGIN_ROOT=None)
        self.assertIn("accepted through u3", result.stderr)

    def test_the_hook_never_removes_the_fallback_log(self):
        """A failed `mktemp` leaves `/dev/null` as the log, and no trap removes it."""

        self.ready()
        self.write_transcript(*THREE)
        shims = self.home / "shims"
        shims.mkdir()
        log = self.home / "rm.log"
        (shims / "mktemp").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (shims / "rm").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n',
                                  encoding="utf-8")
        for name in ("mktemp", "rm"):
            (shims / name).chmod(0o755)
        self.run_ingest(PATH=f"{shims}{os.pathsep}{self.env()['PATH']}")
        self.assertEqual(len(self.ingest_calls()), 2)
        self.assertNotIn("/dev/null", log.read_text(encoding="utf-8") if log.exists() else "")


if __name__ == "__main__":
    unittest.main()
