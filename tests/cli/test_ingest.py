"""Tests for `semanticly ingest` over the MCP `ingest` tool.

Each command test runs `main` against `fakes.FakeMount`, which records every
`tools/call`, so the arguments asserted are the ones that crossed the wire.
The session's recorded level comes from a real `recall --session` call in the
same test, never from a hand-written session file. The slicer tests call
`slice_transcript` over a transcript file, which is its whole production path.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import fakes
from test_mcp_client import McpTestCase
from test_semanticly import REPO_ROOT, cli

REPO = "acme/support"
SESSION = "s1"
FIXTURE = json.loads(
    (REPO_ROOT / "tests" / "testdata" / "ingest_slice.json").read_text(encoding="utf-8"))
ACCEPTED = {"id": "ib_1", "repo": REPO, "branch": "agent/claude-code/ci",
            "accepted_through": "u3", "status": "queued",
            "note": "extraction is queued; whatever it keeps lands on agent/claude-code/ci"}
CLI_NO_BRANCH = ("This connection has no branch for `acme/support`. Choose one at "
                 "http://console.test/acme/support/-/connections, or run `semanticly init` "
                 "in a terminal.")


def row(uuid: str | None, kind: str, content) -> dict:
    """One Claude Code transcript record."""

    record = {"type": kind, "sessionId": "x", "cwd": "/Users/someone",
              "message": {"role": kind, "content": content}}
    if uuid is not None:
        record["uuid"] = uuid
    return record


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def wire(uuid: str, role: str, *texts: str) -> dict:
    return {"uuid": uuid, "role": role, "content": [text(t) for t in texts]}


def cursor_answer(cursor: str | None) -> dict:
    return fakes.tool_result("cursor", {"cursor": cursor})


def refusal(code: str, message: str) -> dict:
    return fakes.tool_result(message, {"error": {"code": code, "message": message}},
                             is_error=True)


class IngestTestCase(McpTestCase):
    def setUp(self):
        super().setUp()
        self.fake = self.mount({
            "recall": fakes.tool_result("", {"lines": [], "text": "", "level": "write"}),
            "ingest": [cursor_answer(None), fakes.tool_result("Accepted.", ACCEPTED)],
        })
        os.environ["SEMANTICLY_API_URL"] = self.fake.url
        os.environ["SEMANTICLY_TOKEN"] = "slt_test"
        self.transcript = self.home / "transcript.jsonl"

    def record_level(self, level: str) -> None:
        """A session-start recall that answers `level`, as the hook runs it."""

        self.fake.script["recall"] = fakes.tool_result(
            "", {"lines": [], "text": "", "level": level})
        code, _, err = self.run_cli(["recall", "--repo", REPO, "--session", SESSION,
                                     "--standing"])
        self.assertEqual(code, 0, err)

    def answers(self, *entries: dict) -> None:
        """What the `ingest` calls answer, in order."""

        self.fake.script["ingest"] = list(entries)

    def write_transcript(self, *records: dict | str) -> None:
        lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
        self.transcript.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def ingest(self, *flags: str):
        return self.run_cli(["ingest", "--repo", REPO, "--session-id", SESSION,
                             "--transcript", str(self.transcript), *flags])

    def ingest_calls(self) -> list[dict]:
        return [r["body"]["params"]["arguments"] for r in self.fake.requests
                if r["method"] == "POST" and r["body"].get("method") == "tools/call"
                and r["body"]["params"]["name"] == "ingest"]

    def three_messages(self) -> None:
        self.write_transcript(row("u1", "user", "one"), row("u2", "assistant", [text("two")]),
                              row("u3", "user", "three"))


# --- the slicer ---------------------------------------------------------------


class SlicerTests(IngestTestCase):
    def slice_of(self, *records: dict | str, after: str | None = None) -> list[dict]:
        self.write_transcript(*records)
        return cli.slice_transcript(self.transcript, after)

    def test_the_slicer_matches_the_shared_fixtures_wire(self):
        """Drives `slice_transcript` over `ingest_slice.json`'s `transcript`."""

        self.assertEqual(self.slice_of(*FIXTURE["transcript"]), FIXTURE["wire"])

    def test_a_partial_last_line_stops_the_read(self):
        """Drives `slice_transcript`'s stop at the first line that does not parse."""

        whole = json.dumps(row("u2", "user", "two"))
        self.assertEqual(self.slice_of(row("u1", "user", "one"), whole[: len(whole) // 2]),
                         [wire("u1", "user", "one")])
        self.assertEqual(
            self.slice_of(row("u1", "user", "one"), "{not json", row("u3", "user", "three")),
            [wire("u1", "user", "one")])

    def test_a_message_without_a_uuid_or_text_is_left_out(self):
        """Drives `wire_message`'s survival rule."""

        tool_use = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
        tool_result = {"type": "tool_result", "tool_use_id": "t1", "content": "secret"}
        got = self.slice_of(
            row(None, "user", "no uuid"),
            row("u1", "user", "   \n"),
            row("u2", "assistant", [tool_use]),
            row("u3", "user", [tool_result]),
            row("u4", "assistant", [text("  "), text("")]),
            row("u5", "system", "a system row"),
            {"type": "summary", "uuid": "u6", "summary": "a summary"},
            row("u7", "assistant", [text("kept"), tool_use, text("")]),
        )
        self.assertEqual(got, [wire("u7", "assistant", "kept", "")])

    def test_a_row_whose_uuid_go_refuses_is_dropped(self):
        """Drives `wire_message`'s uuid rule, Go's `UUIDRefused` (`ingest/slice.go`).

        An empty uuid, one over 64 code points, or one holding a C0 or C1
        control would fail the whole post, so the row is dropped and capture
        goes on.
        """

        widest = "a" * 64
        got = self.slice_of(
            row("", "user", "an empty uuid"),
            row("a" * 65, "user", "too wide"),
            row("u\x01x", "user", "a C0 control"),
            row("u\x7fx", "user", "DEL"),
            row("u\u0085x", "user", "a C1 control"),
            row(widest, "user", "kept"),
        )
        self.assertEqual(got, [wire(widest, "user", "kept")])

    def test_string_content_becomes_one_text_block(self):
        """Drives `wire_content`'s string arm, which keeps the text verbatim."""

        self.assertEqual(self.slice_of(row("u1", "user", "  hello\n")),
                         [wire("u1", "user", "  hello\n")])

    def test_only_messages_after_the_cursor_are_posted(self):
        """Drives `ingest_transcript` → `read_cursor` → `slice_transcript`'s cut."""

        self.record_level("write")
        self.three_messages()
        self.answers(cursor_answer("u1"), fakes.tool_result("Accepted.", ACCEPTED))
        code, _, err = self.ingest()
        self.assertEqual(code, 0, err)
        posted = self.ingest_calls()[1]
        self.assertEqual(posted["slice"], [wire("u2", "assistant", "two"),
                                           wire("u3", "user", "three")])
        self.assertEqual(posted["after"], "u1")

    def test_a_cursor_not_in_the_file_posts_everything_and_says_so(self):
        """Drives `cut_after`'s miss, which posts the whole slice."""

        self.record_level("write")
        self.three_messages()
        self.answers(cursor_answer("gone"), fakes.tool_result("Accepted.", ACCEPTED))
        code, _, err = self.ingest()
        self.assertEqual(code, 0, err)
        self.assertEqual([m["uuid"] for m in self.ingest_calls()[1]["slice"]],
                         ["u1", "u2", "u3"])
        self.assertIn("`gone`", err)
        self.assertIn("posts the whole transcript", err)


# --- the command --------------------------------------------------------------


class IngestCommandTests(IngestTestCase):
    def test_ingest_reads_the_cursor_before_it_posts(self):
        """Drives `cmd_ingest` → `ingest_transcript`: a cursor read, then one post."""

        self.record_level("write")
        self.three_messages()
        code, out, err = self.ingest()
        self.assertEqual(code, 0, err)
        read, post = self.ingest_calls()
        self.assertEqual(read, {"repo": REPO, "session_ref": SESSION,
                                "source_surface": "claude-code"})
        self.assertEqual(post, {
            "repo": REPO, "session_ref": SESSION, "kind": "session",
            "source_surface": "claude-code",
            "slice": [wire("u1", "user", "one"), wire("u2", "assistant", "two"),
                      wire("u3", "user", "three")]})
        self.assertEqual(out, "accepted through u3 (batch ib_1)\n")

    def test_nothing_new_posts_nothing_and_prints_the_cursor(self):
        """Drives `ingest_transcript`'s empty cut, which renders the cursor."""

        self.record_level("write")
        self.three_messages()
        self.answers(cursor_answer("u3"))
        code, out, err = self.ingest()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.ingest_calls()), 1)
        self.assertEqual(out, "nothing new after u3\n")

    def test_no_level_or_read_level_sends_nothing(self):
        """Drives `capture_allowed` ahead of any touch of the transcript.

        The transcript is absent, so a gate placed after the stat or the open
        would fail with exit 2 instead of returning 0.
        """

        code, out, err = self.ingest()
        self.assertEqual((code, out), (0, ""))
        self.assertIn("is not captured", err)
        self.assertEqual(self.fake.requests, [])

        self.record_level("read")
        self.fake.requests.clear()
        code, out, err = self.ingest()
        self.assertEqual((code, out), (0, ""))
        self.assertIn("`read`", err)
        self.assertEqual(self.fake.requests, [])

    def test_a_missing_transcript_fails_before_the_cursor_read(self):
        """Drives `transcript_tail`'s stat, which runs before `read_cursor`."""

        self.record_level("write")
        code, out, err = self.ingest()
        self.assertEqual((code, out), (2, ""))
        self.assertIn(str(self.transcript), err)
        self.assertEqual(self.ingest_calls(), [])

    def test_a_level_recorded_for_another_repo_sends_nothing(self):
        """Drives `capture_allowed`'s repo match."""

        self.fake.script["recall"] = fakes.tool_result(
            "", {"lines": [], "text": "", "level": "write"})
        self.run_cli(["recall", "--repo", "acme/other", "--session", SESSION, "--standing"])
        self.three_messages()
        self.fake.requests.clear()
        code, _, err = self.ingest()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ingest_calls(), [])

    def test_a_no_branch_refusal_prints_the_cli_sentence_and_posts_nothing(self):
        """Drives `read_cursor` → `mcp_call`'s `isError` arm through `main`."""

        self.record_level("write")
        self.three_messages()
        self.answers(refusal("no_branch", CLI_NO_BRANCH))
        code, out, err = self.ingest()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"no_branch — {CLI_NO_BRANCH}", err)
        self.assertEqual(len(self.ingest_calls()), 1)
        self.assertNotIn("slice", self.ingest_calls()[0])

    def test_a_refused_replay_prints_its_refused_reason(self):
        """Drives `print_ingest_answer`'s refused arm."""

        self.record_level("write")
        self.three_messages()
        note = "this slice was already accepted; nothing was queued a second time"
        self.answers(cursor_answer(None), fakes.tool_result("Refused.", {
            "id": "ib_1", "status": "refused", "refused_reason": "stranded",
            "note": note, "cursor": None, "branch": "agent/claude-code/ci"}))
        code, out, err = self.ingest()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, f"refused: stranded\n{note}\n")

    def test_an_unknown_status_prints_the_status_its_batch_and_the_note(self):
        """Drives `print_ingest_answer`'s arm for a status it does not name."""

        self.record_level("write")
        self.three_messages()
        self.answers(cursor_answer(None), fakes.tool_result("Replayed.", {
            "id": "ib_9", "status": "failed", "note": "extraction failed"}))
        code, out, err = self.ingest()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "failed: batch ib_9\nextraction failed\n")

    def test_the_json_flag_prints_the_body_verbatim(self):
        """Drives `print_ingest_answer`'s `--json` arm."""

        self.record_level("write")
        self.three_messages()
        code, out, err = self.ingest("--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), ACCEPTED)

    def test_slice_too_large_is_reported_not_hidden(self):
        """Drives the post's `isError` arm through `main`."""

        self.record_level("write")
        self.three_messages()
        message = "The slice is over 524288 bytes. Post a shorter one."
        self.answers(cursor_answer(None), refusal("slice_too_large", message))
        code, out, err = self.ingest()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"slice_too_large — {message}", err)

    def test_ingest_sends_no_external_id(self):
        """Drives every `ingest` call's arguments."""

        self.record_level("write")
        self.three_messages()
        self.ingest()
        for arguments in self.ingest_calls():
            self.assertNotIn("external_id", arguments)

    def test_the_surface_flag_reaches_source_surface_and_client_name(self):
        """Drives `add_surface_flag` → `ingest_transcript` → `McpClient.open`."""

        self.record_level("write")
        self.three_messages()
        self.fake.requests.clear()
        self.ingest("--surface", "cowork")
        for arguments in self.ingest_calls():
            self.assertEqual(arguments["source_surface"], "cowork")
        names = {r["body"]["params"]["clientInfo"]["name"] for r in self.fake.requests
                 if r["method"] == "POST" and r["body"].get("method") == "initialize"}
        self.assertEqual(names, {"cowork"})


class IngestFileTests(IngestTestCase):
    def test_file_posts_the_prepared_slice_without_a_gate_or_a_cursor_read(self):
        """Drives `cmd_ingest` → `ingest_file`."""

        prepared = self.home / "slice.json"
        prepared.write_text(json.dumps(FIXTURE["wire"]), encoding="utf-8")
        self.answers(fakes.tool_result("Accepted.", ACCEPTED))
        code, out, err = self.run_cli(["ingest", "--repo", REPO, "--session-id", SESSION,
                                       "--file", str(prepared)])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ingest_calls(), [{
            "repo": REPO, "session_ref": SESSION, "kind": "session",
            "source_surface": "claude-code", "slice": FIXTURE["wire"]}])
        self.assertEqual(out, "accepted through u3 (batch ib_1)\n")

    def test_an_unreadable_file_fails_with_a_message_and_sends_nothing(self):
        """Drives `ingest_file`'s read refusal."""

        for name, content in (("missing.json", None), ("bad.json", "{not json")):
            with self.subTest(name=name):
                path = self.home / name
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                code, _, err = self.run_cli(["ingest", "--repo", REPO, "--session-id",
                                             SESSION, "--file", str(path)])
                self.assertEqual(code, 2)
                self.assertIn(str(Path(path)), err)
                self.assertEqual(self.fake.requests, [])


class IngestSweepTests(IngestTestCase):
    def setUp(self):
        super().setUp()
        self.sweep_dir = self.home / "transcripts"
        self.sweep_dir.mkdir()

    def session_file(self, session: str, *records: dict) -> Path:
        path = self.sweep_dir / f"{session}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path

    def sweep(self, *flags: str):
        return self.run_cli(["ingest", "--repo", REPO, "--sweep", str(self.sweep_dir), *flags])

    def test_the_sweep_posts_each_sessions_tail(self):
        """Drives `cmd_ingest` → `ingest_sweep` → `transcript_tail` per file.

        No session level is recorded, so the posts prove the sweep gates on the
        cursor read alone.
        """

        self.session_file("sa", row("a1", "user", "one"), row("a2", "assistant", "two"))
        self.session_file("sb", row("b1", "user", "three"), row("b2", "user", "four"))
        (self.sweep_dir / "notes.txt").write_text("not a transcript", encoding="utf-8")
        self.answers(cursor_answer(None), fakes.tool_result("Accepted.", ACCEPTED),
                     cursor_answer("b1"), fakes.tool_result("Accepted.", ACCEPTED))
        code, out, err = self.sweep()
        self.assertEqual(code, 0, err)
        surface = {"source_surface": "claude-code"}
        self.assertEqual(self.ingest_calls(), [
            {"repo": REPO, "session_ref": "sa", **surface},
            {"repo": REPO, "session_ref": "sa", "kind": "session", **surface,
             "slice": [wire("a1", "user", "one"), wire("a2", "assistant", "two")]},
            {"repo": REPO, "session_ref": "sb", **surface},
            {"repo": REPO, "session_ref": "sb", "kind": "session", **surface,
             "slice": [wire("b2", "user", "four")], "after": "b1"},
        ])
        accepted = "accepted through u3 (batch ib_1)"
        self.assertEqual(out, f"session sa\n{accepted}\nsession sb\n{accepted}\n")

    def test_the_sweep_stops_on_a_403_cursor_read_without_opening_a_transcript(self):
        """Drives `ingest_sweep`'s order, the cursor read before any open.

        Both transcripts are unreadable, so an open ahead of the read would end
        the run with exit 2 and no read at all.
        """

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        for name in ("sa", "sb"):
            path = self.session_file(name, row("a1", "user", "one"))
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)
        message = "You hold read on acme/support. Capture needs write."
        self.answers(refusal("insufficient_level", message))
        code, out, err = self.sweep()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"insufficient_level — {message}", err)
        self.assertEqual(self.ingest_calls(),
                         [{"repo": REPO, "session_ref": "sa", "source_surface": "claude-code"}])

    def test_a_per_file_refusal_skips_that_session_and_the_sweep_goes_on(self):
        """Drives `ingest_sweep`'s skip arm for a refusal `sweep_stops_on` rejects."""

        self.session_file("sa", row("a1", "user", "one"))
        self.session_file("sb", row("b1", "user", "two"))
        message = "The slice is over 524288 bytes. Post a shorter one."
        self.answers(cursor_answer(None), refusal("slice_too_large", message),
                     cursor_answer(None), fakes.tool_result("Accepted.", ACCEPTED))
        code, out, err = self.sweep()
        self.assertEqual(code, 1, err)
        self.assertIn(f"session `sa` was skipped — slice_too_large — {message}", err)
        self.assertEqual(out, "session sb\naccepted through u3 (batch ib_1)\n")
        self.assertEqual([c["session_ref"] for c in self.ingest_calls()],
                         ["sa", "sa", "sb", "sb"])

    def test_an_unreadable_transcript_skips_that_session_and_the_sweep_goes_on(self):
        """Drives `ingest_sweep`'s skip arm for a `TranscriptError` from `read_wire`."""

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        unreadable = self.session_file("sa", row("a1", "user", "one"))
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o600)
        self.session_file("sb", row("b1", "user", "two"))
        self.answers(cursor_answer(None), cursor_answer(None),
                     fakes.tool_result("Accepted.", ACCEPTED))
        code, out, err = self.sweep()
        self.assertEqual(code, 1, err)
        self.assertIn(f"session `sa` was skipped — Could not read {unreadable}", err)
        self.assertEqual(out, "session sb\naccepted through u3 (batch ib_1)\n")

    def test_ingest_disabled_stops_the_sweep_before_the_next_session(self):
        """Drives `sweep_stops_on`'s code arm, which re-raises out of the loop."""

        self.session_file("sa", row("a1", "user", "one"))
        self.session_file("sb", row("b1", "user", "two"))
        message = "Capture is switched off for acme/support."
        self.answers(refusal("ingest_disabled", message))
        code, out, err = self.sweep()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"ingest_disabled — {message}", err)
        self.assertNotIn("skipped", err)
        self.assertEqual([c["session_ref"] for c in self.ingest_calls()], ["sa"])

    def test_the_json_flag_prints_one_object_per_line(self):
        """Drives `print_sweep_answer`'s JSON Lines arm."""

        self.session_file("sa", row("a1", "user", "one"))
        self.session_file("sb", row("b1", "user", "two"))
        self.answers(cursor_answer(None), fakes.tool_result("Accepted.", ACCEPTED),
                     cursor_answer("b1"))
        code, out, err = self.sweep("--json")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual([json.loads(line) for line in lines], [
            {"session": "sa", "answer": ACCEPTED},
            {"session": "sb", "answer": {"cursor": "b1"}},
        ])

    def test_a_directory_with_no_transcripts_posts_nothing(self):
        """Drives `ingest_sweep`'s empty listing."""

        (self.sweep_dir / "notes.txt").write_text("not a transcript", encoding="utf-8")
        code, out, err = self.sweep()
        self.assertEqual((code, out), (0, ""), err)
        self.assertEqual(self.fake.requests, [])

    def test_a_missing_directory_fails_and_sends_nothing(self):
        """Drives `ingest_sweep`'s directory check."""

        self.sweep_dir.rmdir()
        code, _, err = self.sweep()
        self.assertEqual(code, 2)
        self.assertIn(str(self.sweep_dir), err)
        self.assertEqual(self.fake.requests, [])


class IngestDocumentTests(IngestTestCase):
    def test_a_document_posts_the_whole_file_and_prints_confirm_url(self):
        """Drives `cmd_ingest` → `ingest_document` → `print_ingest_answer`.

        No session level is recorded, because a person's explicit post is not
        gated. The status is `queued`, so the confirm arm must win over the
        accepted one.
        """

        document = self.home / "runbook.md"
        content = "# Runbook\n\nRefunds take 30 days.\n"
        document.write_text(content, encoding="utf-8")
        url = "http://console.test/acme/support/-/import/imp_1"
        note = "nothing is written until a person confirms the preview at confirm_url"
        self.answers(fakes.tool_result("Confirm.", {
            "import": "imp_1", "repo": "support", "branch": "agent/claude-code/ci",
            "status": "queued", "confirm_url": url, "note": note}))
        code, out, err = self.run_cli(["ingest", "--repo", REPO, "--kind", "document",
                                       "--file", str(document)])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ingest_calls(), [{
            "repo": REPO, "kind": "document", "slice": content,
            "source_surface": "claude-code"}])
        self.assertEqual(out, f"confirm at {url} (import imp_1)\n{note}\n")

    def test_an_unreadable_document_fails_and_sends_nothing(self):
        """Drives `read_document`'s refusal."""

        path = self.home / "missing.md"
        code, _, err = self.run_cli(["ingest", "--repo", REPO, "--kind", "document",
                                     "--file", str(path)])
        self.assertEqual(code, 2)
        self.assertIn(str(path), err)
        self.assertEqual(self.fake.requests, [])


class IngestMemoryTests(IngestTestCase):
    """Production path: `cmd_ingest` → `ingest_memory` → `memory_entries`."""

    PROJECT_ENTRY = "---\nname: release\ntype: project\n---\n\nThe release is Friday.\n"

    def setUp(self):
        super().setUp()
        self.memory = self.home / "projects" / "-Users-someone-app" / "memory"
        self.memory.mkdir(parents=True)
        self.answers(fakes.tool_result("Accepted.", ACCEPTED))

    def ingest_memory(self, *flags: str):
        return self.run_cli(["ingest", "--repo", REPO, "--session-id", SESSION,
                             "--memory-dir", str(self.memory), *flags])

    def test_memory_posts_one_slice_under_the_projects_session_ref(self):
        self.record_level("write")
        self.fake.requests.clear()
        (self.memory / "release.md").write_text(self.PROJECT_ENTRY, encoding="utf-8")
        code, out, err = self.ingest_memory()
        self.assertEqual(code, 0, err)
        (call,) = self.ingest_calls()
        digest = hashlib.sha256(b"-Users-someone-app").hexdigest()[:32]
        self.assertEqual(call["session_ref"], f"claude-code-memory:{digest}")
        self.assertNotIn("someone", json.dumps(call), "the local path left the machine")
        self.assertEqual(call["source_surface"], "claude-code-memory")
        self.assertEqual(call["kind"], "session")
        self.assertEqual(call["slice"][0]["content"][0]["text"],
                         "type: project\n" + self.PROJECT_ENTRY.strip())
        names = {r["body"]["params"]["clientInfo"]["name"] for r in self.fake.requests
                 if r["method"] == "POST" and r["body"].get("method") == "initialize"}
        self.assertEqual(names, {"claude-code-memory"})
        self.assertEqual(out, "accepted through u3 (batch ib_1)\n")

    def test_memory_below_write_reads_no_file_and_sends_nothing(self):
        """The gate runs first, so a missing directory is never reached."""

        self.record_level("read")
        self.fake.requests.clear()
        self.memory.rmdir()
        code, out, err = self.ingest_memory()
        self.assertEqual((code, out), (0, ""))
        self.assertIn("the project's memory is not captured", err)
        self.assertEqual(self.fake.requests, [])

    def test_a_missing_memory_directory_posts_nothing(self):
        self.record_level("write")
        self.fake.requests.clear()
        self.memory.rmdir()
        code, out, err = self.ingest_memory()
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.fake.requests, [])


class IngestUsageTests(IngestTestCase):
    def test_flags_an_arm_does_not_take_are_refused(self):
        """Drives `checked_ingest_flags`."""

        prepared = str(self.home / "slice.json")
        cases = {
            "memory without a session": ["--memory-dir", str(self.home)],
            "memory with a surface": ["--memory-dir", str(self.home), "--session-id", SESSION,
                                      "--surface", "cowork"],
            "a document from a transcript": ["--kind", "document", "--session-id", SESSION,
                                             "--transcript", str(self.transcript)],
            "a sweep with a session": ["--sweep", str(self.home), "--session-id", SESSION],
            "a document with a session": ["--kind", "document", "--file", prepared,
                                          "--session-id", SESSION],
            "a transcript without a session": ["--transcript", str(self.transcript)],
            "a slice without a session": ["--file", prepared],
        }
        for name, flags in cases.items():
            with self.subTest(name):
                code, _, err = self.run_cli(["ingest", "--repo", REPO, *flags])
                self.assertEqual(code, 2, err)
                self.assertIn("semanticly:", err)
                self.assertEqual(self.fake.requests, [])
