"""Tests for `hooks/memory.sh`, the plugin's memory hook.

Every test runs the real hook as a subprocess against the real bundled CLI,
which speaks Streamable HTTP to `fakes.FakeMount`. The token sits in the fake
keychain, whose directory leads the hook's `PATH`. The session's recorded
level comes from a real `recall --session` call through the bundled CLI. The
memory directory is the one beside the payload's `transcript_path`, as Claude
Code lays it out, and the mount's recorded `ingest` arguments are the bytes
the hook caused to be sent.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from test_session_start import (
    CLI_NO_BRANCH,
    CLI_PATH,
    COWORK_PROJECT_DIR,
    PLUGIN,
    REPO,
    SESSION,
    SessionStartTestCase,
    context,
    fakes,
)

MEMORY = PLUGIN / "hooks" / "memory.sh"
PROJECT = "-Users-someone-project"
SESSION_REF = "claude-code-memory:" + hashlib.sha256(PROJECT.encode()).hexdigest()[:32]
ACCEPTED = {"id": "ib_9", "repo": REPO, "branch": "team/support",
            "accepted_through": "x", "status": "queued", "note": "queued"}


def entry(kind: str, body: str, name: str = "note") -> str:
    """One memory entry, its frontmatter block and its body."""

    return f"---\nname: {name}\ndescription: about {name}\ntype: {kind}\n---\n\n{body}\n"


def uuid_of(relpath: str, entry_text: str) -> str:
    return hashlib.sha256((relpath + entry_text).encode("utf-8")).hexdigest()


class MemoryHookTestCase(SessionStartTestCase):
    """The session-start harness, aimed at the memory hook and a project's memory directory."""

    def setUp(self):
        super().setUp()
        self.project_store = self.home / ".claude" / "projects" / PROJECT
        self.memory = self.project_store / "memory"
        self.memory.mkdir(parents=True)
        self.mount.script["ingest"] = fakes.tool_result("Accepted.", ACCEPTED)

    def payload(self) -> str:
        return json.dumps({
            "session_id": SESSION,
            "transcript_path": str(self.project_store / f"{SESSION}.jsonl"),
            "cwd": str(self.project), "hook_event_name": "SessionEnd", "reason": "exit",
        })

    def write_memory(self, name: str, text: str | bytes) -> None:
        path = self.memory / name
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")

    def record_level(self, level: str, repo: str = REPO) -> None:
        """A `recall --session` through the bundled CLI that answers `level` for `repo`."""

        self.answer(context([], level=level))
        result = subprocess.run(
            [str(CLI_PATH), "recall", "--repo", repo, "--session", SESSION, "--standing"],
            env=self.env(), cwd=str(self.home), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.mount.requests.clear()

    def ready(self, level: str = "write") -> None:
        self.bind()
        self.store_token()
        self.record_level(level)

    def run_memory(self, script: Path = MEMORY, **env) -> subprocess.CompletedProcess:
        return self.run_hook(script, **env)

    def ingest_calls(self) -> list[dict]:
        return [c["arguments"] for c in self.tool_calls() if c["name"] == "ingest"]

    def posted(self) -> list[dict]:
        calls = self.ingest_calls()
        self.assertEqual(len(calls), 1, calls)
        return calls[0]["slice"]


class Entries(MemoryHookTestCase):
    """Production path: `memory.sh` → `ingest --memory-dir` → `memory_entries`."""

    def test_only_project_and_feedback_entries_are_posted_in_path_then_position_order(self):
        self.ready()
        self.write_memory("b_mixed.md", entry("user", "Jordan likes tea.", "u")
                          + entry("feedback", "Prefer short replies.", "f")
                          + entry("reference", "Docs live at /docs.", "r")
                          + entry("project", "The release is Friday.", "p"))
        self.write_memory("a_first.md", entry("project", "Lane 3.5 ships the plugin.", "a"))
        self.run_memory()
        bodies = [m["content"][0]["text"].splitlines()[-1] for m in self.posted()]
        self.assertEqual(bodies, ["Lane 3.5 ships the plugin.", "Prefer short replies.",
                                  "The release is Friday."])
        self.assertEqual({m["role"] for m in self.posted()}, {"assistant"})

    def test_the_uuid_is_sha256_of_relpath_and_entry_and_stable_across_runs(self):
        self.ready()
        first, second = entry("project", "One.", "one"), entry("feedback", "Two.", "two")
        self.write_memory("notes.md", first + second)
        self.run_memory()
        once = self.posted()
        self.mount.requests.clear()
        self.mount.script["ingest"] = fakes.tool_result("Accepted.", ACCEPTED)
        self.run_memory()
        self.assertEqual(self.posted(), once)
        self.assertEqual([m["uuid"] for m in once],
                         [uuid_of("notes.md", first.strip()), uuid_of("notes.md", second.strip())])

    def test_the_type_rides_the_first_line(self):
        self.ready()
        text = entry("feedback", "Run the suite before committing.")
        self.write_memory("feedback_suite.md", text)
        self.run_memory()
        (message,) = self.posted()
        self.assertEqual(message["content"], [{"type": "text",
                                               "text": "type: feedback\n" + text.strip()}])

    def test_memory_md_and_unparseable_files_are_skipped(self):
        self.ready()
        self.write_memory("MEMORY.md", "- [Release](project_release.md) — Friday\n")
        self.write_memory("no_close.md", "---\ntype: project\nnever closed\n")
        self.write_memory("binary.md", b"---\ntype: project\n---\n\xff\xfe not utf-8\n")
        self.write_memory("prose_first.md", "Some prose.\n" + entry("project", "Hidden."))
        self.write_memory("kept.md", entry("project", "Kept."))
        self.write_memory("ignored.txt", entry("project", "Not markdown."))
        self.run_memory()
        self.assertEqual([m["uuid"] for m in self.posted()],
                         [uuid_of("kept.md", entry("project", "Kept.").strip())])

    def test_a_rule_in_a_body_is_not_a_new_entry(self):
        """A `---` line followed by prose is the body's, so the entry stays whole."""

        self.ready()
        text = entry("project", "Above.\n\n---\n\nBelow the rule.\n\n---\n")
        self.write_memory("rule.md", text)
        self.run_memory()
        (message,) = self.posted()
        self.assertEqual(message["content"][0]["text"], "type: project\n" + text.strip())


class Post(MemoryHookTestCase):
    def test_the_post_reads_no_cursor(self):
        """One recorded `ingest` call, and it carries the slice and the memory road's keys."""

        self.ready()
        self.write_memory("p.md", entry("project", "One."))
        result = self.run_memory()
        (call,) = self.ingest_calls()
        self.assertIn("slice", call)
        self.assertEqual({k: v for k, v in call.items() if k != "slice"},
                         {"repo": REPO, "session_ref": SESSION_REF, "kind": "session",
                          "source_surface": "claude-code-memory"})
        self.assertEqual(result.stderr, "semanticly[memory]: accepted through x (batch ib_9)\n")

    def test_no_kept_entry_posts_nothing_and_says_nothing(self):
        self.ready()
        self.write_memory("u.md", entry("user", "Only a user note."))
        result = self.run_memory()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])

    def test_no_level_or_read_level_posts_nothing(self):
        """Drives the CLI's capture gate, which `ingest --memory-dir` reaches before any read."""

        self.bind()
        self.store_token()
        self.write_memory("p.md", entry("project", "One."))
        result = self.run_memory()
        self.assertEqual(result.stderr,
                         f"semanticly[memory]: this session has no recorded level on `{REPO}`, "
                         "so the project's memory is not captured.\n")
        self.assertEqual(self.mount.requests, [])

        self.record_level("read")
        result = self.run_memory()
        self.assertEqual(result.stderr,
                         f"semanticly[memory]: this session holds `read` on `{REPO}`, "
                         "so the project's memory is not captured.\n")
        self.assertEqual(self.mount.requests, [])

    def test_no_branch_ends_at_exit_0_with_the_sentence(self):
        self.ready()
        self.write_memory("p.md", entry("project", "One."))
        self.mount.script["ingest"] = fakes.tool_result(
            CLI_NO_BRANCH, {"error": {"code": "no_branch", "message": CLI_NO_BRANCH}},
            is_error=True)
        result = self.run_memory()
        self.assertEqual(result.stderr, f"semanticly[memory]: no_branch — {CLI_NO_BRANCH}\n")
        self.assertEqual(len(self.ingest_calls()), 1)

    def test_an_unbound_project_posts_nothing_and_says_nothing(self):
        self.store_token()
        self.write_memory("p.md", entry("project", "One."))
        result = self.run_memory()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])

    def test_a_payload_without_a_transcript_exits_0_with_one_line(self):
        self.ready()
        result = subprocess.run([str(MEMORY)], input=json.dumps({"session_id": SESSION}),
                                env=self.env(), cwd=str(self.home), capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("named no session and transcript", result.stderr)
        self.assertEqual(self.mount.requests, [])


class Cowork(MemoryHookTestCase):
    def test_cowork_runs_no_memory_post(self):
        """Drives the Cowork guard alone.

        The copy's lookup finds a binding, the level is recorded at `write` and
        a kept entry waits, so only the guard stops the post.
        """

        self.ready()
        self.write_memory("p.md", entry("project", "One."))
        self.plugin_root = self.plugin_that_finds_the_binding_anywhere()
        result = self.run_memory(self.plugin_root / "hooks" / "memory.sh",
                                 CLAUDE_PROJECT_DIR=COWORK_PROJECT_DIR)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])
