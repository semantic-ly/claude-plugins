"""Tests for `semanticly recall` over the MCP `recall` tool.

Each test runs `main` against `fakes.FakeMount`, which records every
`tools/call`, so the arguments asserted are the ones that crossed the wire.
The session file is produced by earlier recalls in the same test, except
where the file itself is the input under test.
"""

from __future__ import annotations

import json
import os

import fakes
from test_mcp_client import McpTestCase
from test_semanticly import REPO_ROOT, cli

REPO = "acme/support"


def line(sha: str, layer: str = "semantic", label: str | None = None) -> dict:
    """One `/context` line as the server prints it."""

    return {"n": 1, "layer": layer, "label": label or layer, "display_id": "F-1",
            "sha": sha, "body": f"fact {sha}", "repo": REPO}


def context(lines: list[dict], level: str = "write") -> dict:
    """A `/context` body, the structured content `recall` relays verbatim."""

    text = "\n".join(row["body"] for row in lines)
    return {"lines": lines, "text": text, "level": level,
            "recent_writes": {"awaiting_review": 0, "waiting_on_quota": 0,
                              "open_proposals": 0, "url": ""}}


def standing(*shas: str) -> list[dict]:
    return [line(sha, "standing", "standing") for sha in shas]


def semantic(*shas: str) -> list[dict]:
    return [line(sha) for sha in shas]


class RecallTestCase(McpTestCase):
    def setUp(self):
        super().setUp()
        self.fake = self.mount({"recall": fakes.tool_result("", context([]))})
        os.environ["SEMANTICLY_API_URL"] = self.fake.url
        os.environ["SEMANTICLY_TOKEN"] = "slt_test"

    def answer(self, body: dict) -> None:
        """What the next `recall` answers."""

        self.fake.script["recall"] = fakes.tool_result(body["text"], body)

    def recall(self, *flags: str, body: dict | None = None, repo: str = REPO):
        if body is not None:
            self.answer(body)
        return self.run_cli(["recall", "--repo", repo, *flags])

    def sent(self, index: int = -1) -> dict:
        """The arguments of one recorded `tools/call`."""

        calls = [r["body"]["params"] for r in self.fake.requests
                 if r["method"] == "POST" and r["body"].get("method") == "tools/call"]
        self.assertEqual(calls[index]["name"], "recall")
        return calls[index]["arguments"]

    def session_file(self, session: str = "s1") -> dict:
        path = cli.injected_path(session)
        return json.loads(path.read_text(encoding="utf-8"))


class RecallArgumentTests(RecallTestCase):
    def test_recall_always_sends_standing(self):
        """Drives `cmd_recall` → `recall_arguments` → `mcp_call`, with and without the flag."""

        code, _, err = self.recall("refunds")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent(), {"repo": REPO, "query": "refunds", "standing": False})

        code, _, err = self.recall("--standing")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent(), {"repo": REPO, "standing": True})

    def test_recall_sends_no_withdrawn_argument(self):
        """Drives `recall_arguments` with every flag, and `build_parser`'s refusals."""

        code, _, err = self.recall("--ref", "v1.4", "--token-budget", "600",
                                   "--session", "s1", "refunds")
        self.assertEqual(code, 0, err)
        arguments = self.sent()
        self.assertEqual(set(arguments),
                         {"repo", "query", "standing", "ref", "token_budget", "session_ref"})
        self.assertEqual(arguments["ref"], "v1.4")
        self.assertEqual(arguments["token_budget"], 600)
        self.assertEqual(arguments["session_ref"], "s1")
        for withdrawn in ("top_k", "topic", "include_terms"):
            self.assertNotIn(withdrawn, arguments)

        for flag in ("--topic", "--top-k"):
            with self.subTest(flag=flag):
                done = self.run_process(["recall", "--repo", REPO, flag, "3", "q"])
                self.assertEqual(done.returncode, cli.EXIT_USAGE)
                self.assertIn(f"unrecognized arguments: {flag}", done.stderr)


class SessionFileTests(RecallTestCase):
    def test_a_standing_recall_sends_no_exclusions_and_replaces_the_file(self):
        """Drives `session_exclusions`' standing arm and `record_recall`'s replace."""

        self.recall("--session", "s1", "q", body=context(semantic("a1", "b2")))
        self.assertEqual(self.session_file()["fact_ids"], ["a1", "b2"])

        code, _, err = self.recall("--session", "s1", "--standing",
                                   body=context(standing("s1") + semantic("c3")))
        self.assertEqual(code, 0, err)
        self.assertNotIn("exclude_facts", self.sent())
        self.assertEqual(self.session_file(),
                         {"repo": REPO, "level": "write", "standing": ["s1"],
                          "fact_ids": ["c3"]})

    def test_a_later_recall_excludes_standing_then_tail(self):
        """Drives `session_exclusions`' order and `record_recall`'s append."""

        self.recall("--session", "s1", "--standing",
                    body=context(standing("s1", "s2") + semantic("t1")))
        self.recall("--session", "s1", "q", body=context(semantic("t2")))
        self.assertEqual(self.sent()["exclude_facts"], ["s1", "s2", "t1"])

        self.recall("--session", "s1", "q", body=context([]))
        self.assertEqual(self.sent()["exclude_facts"], ["s1", "s2", "t1", "t2"])
        self.assertEqual(self.session_file()["standing"], ["s1", "s2"])

    def test_ids_from_another_repo_are_dropped(self):
        """Drives `session_exclusions`' repo match and `record_recall`'s restart."""

        self.recall("--session", "s1", "--standing",
                    body=context(standing("s1") + semantic("t1")))
        code, _, err = self.recall("--session", "s1", "q", repo="acme/other",
                                   body=context(semantic("o1")))
        self.assertEqual(code, 0, err)
        self.assertNotIn("exclude_facts", self.sent())
        self.assertEqual(self.session_file(),
                         {"repo": "acme/other", "level": "write", "standing": [],
                          "fact_ids": ["o1"]})

    def test_the_tail_drops_oldest_and_standing_never_drops(self):
        """Drives `record_recall`'s tail bound and its one stderr line."""

        held = cli.MAX_SESSION_EXCLUSIONS - 3
        tail = [f"t{i}" for i in range(held + 1)]
        self.recall("--session", "s1", "--standing", body=context(standing("s1", "s2", "s3")))
        code, _, err = self.recall("--session", "s1", "q", body=context(semantic(*tail)))
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("semanticly:"), 1, err)
        self.assertIn("the 1 oldest", err)

        stored = self.session_file()
        self.assertEqual(stored["standing"], ["s1", "s2", "s3"])
        self.assertEqual(stored["fact_ids"], tail[1:])

        self.recall("--session", "s1", "q", body=context([]))
        self.assertEqual(self.sent()["exclude_facts"], ["s1", "s2", "s3", *tail[1:]])

    def test_standing_past_500_sends_the_newest_500_and_logs_once(self):
        """Drives `session_exclusions`' server cut (Review Focus 5)."""

        ids = [f"s{i}" for i in range(cli.SERVER_MAX_EXCLUSIONS + 100)]
        _, _, err = self.recall("--session", "s1", "--standing",
                                body=context(standing(*ids) + semantic("t1")))
        self.assertEqual(self.session_file()["fact_ids"], [], "the tail empties first")
        self.assertEqual(err.count("semanticly:"), 1, err)
        self.assertEqual(len(self.session_file()["standing"]), len(ids))

        code, _, err = self.recall("--session", "s1", "q", body=context([]))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent()["exclude_facts"], ids[-cli.SERVER_MAX_EXCLUSIONS:])
        self.assertEqual(err.count("semanticly:"), 1, err)
        self.assertIn(f"newest {cli.SERVER_MAX_EXCLUSIONS}", err)

    def test_the_session_bound_sits_under_the_server_s_ceiling(self):
        self.assertLess(cli.MAX_SESSION_EXCLUSIONS, cli.SERVER_MAX_EXCLUSIONS)

    def test_the_level_is_recorded(self):
        """Drives `record_recall`'s level from the result."""

        self.recall("--session", "s1", "--standing", body=context(standing("s1"), level="read"))
        self.assertEqual(self.session_file()["level"], "read")
        self.recall("--session", "s1", "q", body=context([], level="write"))
        self.assertEqual(self.session_file()["level"], "write")

    def test_an_old_shape_session_file_reads_as_empty(self):
        """Drives `read_injected` on the older shape, which the file itself supplies."""

        path = cli.injected_path("s1")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"repo": REPO, "fact_ids": ["F-1", "F-2"]}),
                        encoding="utf-8")
        code, _, err = self.recall("--session", "s1", "q")
        self.assertEqual(code, 0, err)
        self.assertNotIn("exclude_facts", self.sent())

    def test_an_oversized_session_file_is_skipped_and_said(self):
        """Drives `read_injected` → `read_state_file`'s cap."""

        path = cli.injected_path("s1")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"repo": REPO, "level": "write",
                                    "standing": ["x" * cli.STATE_FILE_MAX_BYTES],
                                    "fact_ids": []}), encoding="utf-8")
        code, _, err = self.recall("--session", "s1", "q")
        self.assertEqual(code, 0, err)
        self.assertNotIn("exclude_facts", self.sent())
        self.assertEqual(err.count("was not read"), 1)


class RecallSurfaceTests(RecallTestCase):
    def test_the_surface_flag_becomes_the_client_name(self):
        """Drives `add_surface_flag` → `cmd_recall` → `McpClient.open`."""

        names = []
        for flags in ((), ("--surface", "cowork")):
            self.fake.requests.clear()
            code, _, err = self.recall(*flags, "q")
            self.assertEqual(code, 0, err)
            init = next(r for r in self.fake.requests if r["body"].get("method") == "initialize")
            names.append(init["body"]["params"]["clientInfo"]["name"])
        self.assertEqual(names, ["claude-code", "cowork"])


class RecallOutputTests(RecallTestCase):
    def test_a_block_with_no_term_or_semantic_line_prints_nothing_in_a_session(self):
        """Drives `print_recall` on each arm, and `--json`'s verbatim echo."""

        quiet = context([line("e1", "expansion")])
        code, out, err = self.recall("--session", "s1", "q", body=quiet)
        self.assertEqual((code, out), (0, ""), err)

        code, out, _ = self.recall("--session", "s1", "--json", "q", body=quiet)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), quiet)

        for layer in ("term", "semantic"):
            with self.subTest(layer=layer):
                _, out, _ = self.recall("--session", "s1", "q",
                                        body=context([line("x1", layer)]))
                self.assertEqual(out, "fact x1\n")

        _, out, _ = self.recall("--session", "s1", "--standing", body=context(standing("s1")))
        self.assertEqual(out, "fact s1\n")

        _, out, _ = self.recall("q", body=quiet)
        self.assertEqual(out, "fact e1\n", "outside a session the text always prints")


    def test_a_refusal_under_json_prints_its_envelope_and_exits_1(self):
        """Drives `cmd_recall`'s refusal arm, which a hook reads instead of stderr."""

        envelope = {"error": {"code": "no_branch", "message": "This connection has no branch."}}
        self.fake.script["recall"] = fakes.tool_result(
            "no_branch: This connection has no branch.", envelope, is_error=True)
        code, out, err = self.recall("--session", "s1", "--standing", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), envelope)
        self.assertIn("no_branch — This connection has no branch.", err)
        self.assertFalse(cli.injected_path("s1").exists(), "a refusal records nothing")

        code, out, _ = self.recall("--standing")
        self.assertEqual((code, out), (1, ""), "without --json stdout stays empty")


class SourceTests(RecallTestCase):
    def test_no_file_under_plugins_names_recall_text_go(self):
        """Pins the deleted Go file out of every comment and document here."""

        name = "recall_text" + ".go"
        named = []
        for root, dirs, files in os.walk(REPO_ROOT / "plugins"):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for file in files:
                path = os.path.join(root, file)
                with open(path, "rb") as handle:
                    if name.encode() in handle.read():
                        named.append(os.path.relpath(path, REPO_ROOT))
        self.assertEqual(named, [])
