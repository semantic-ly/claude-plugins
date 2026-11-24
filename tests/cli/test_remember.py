"""Tests for `semanticly remember` over the MCP `remember` tool.

Each test runs `main` against `fakes.FakeMount`, which records every
`tools/call`, so the arguments asserted are the ones that crossed the wire.
"""

from __future__ import annotations

import json
import os

import fakes
from test_mcp_client import McpTestCase

REPO = "acme/support"
REPO_SECRET = "slt_repo_fedcba9876543210fedcba98765432"
UNSCOPED_SECRET = "slt_unscoped_00112233445566778899aabbccdd"
WRITTEN = {"written": [{"id": "F-9", "sha": "f9f9f9"}], "branch": "agent/claude-code/ci"}
NOT_ALLOWED = ("supersedes", "supports", "contradicts", "term", "aliases", "rationale",
               "external_id", "external_ids")


class RememberTestCase(McpTestCase):
    def setUp(self):
        super().setUp()
        self.fake = self.mount({"remember": fakes.tool_result("F-9 fact:f9f9f9", WRITTEN)})
        os.environ["SEMANTICLY_API_URL"] = self.fake.url
        os.environ["SEMANTICLY_TOKEN"] = "slt_test"

    def remember(self, *flags: str, stdin: str = ""):
        return self.run_cli(["remember", "--repo", REPO, *flags], stdin=stdin)

    def calls(self) -> list[dict]:
        return [r["body"]["params"] for r in self.fake.requests
                if r["method"] == "POST" and r["body"].get("method") == "tools/call"]

    def sent(self) -> dict:
        calls = self.calls()
        self.assertEqual(calls[-1]["name"], "remember")
        return calls[-1]["arguments"]


class RememberArgumentTests(RememberTestCase):
    def test_remember_sends_topics_session_ref_and_no_unstated_confidence(self):
        """Drives `cmd_remember` → `remember_arguments` → `mcp_call`, with and without flags."""

        code, out, err = self.remember("--topic", "billing", "--topic", "refunds",
                                       "--session", "s1", "Refunds are 30 days.")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent(), {
            "repo": REPO, "facts": [{"body": "Refunds are 30 days."}],
            "topics": ["billing", "refunds"], "session_ref": "s1"})
        self.assertIn("F-9 fact:f9f9f9", out)

        code, _, err = self.remember("--confidence", "0.7", "Refunds.")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sent(), {
            "repo": REPO, "facts": [{"body": "Refunds."}], "confidence": 0.7})

        code, _, err = self.remember("Plain.")
        self.assertEqual(code, 0, err)
        sent = self.sent()
        for key in ("topics", "confidence", "session_ref"):
            self.assertNotIn(key, sent)

    def test_remember_sends_one_statement_object(self):
        """Drives `remember_arguments`' `facts`, over arguments and stdin."""

        for stdin, words in (("", ["Refunds", "are", "30", "days."]),
                             ("  From stdin.  \n", [])):
            with self.subTest(stdin=stdin):
                code, _, err = self.remember(*words, stdin=stdin)
                self.assertEqual(code, 0, err)
                sent = self.sent()
                facts = sent["facts"]
                self.assertEqual(len(facts), 1)
                self.assertEqual(list(facts[0]), ["body"])
                for key in NOT_ALLOWED:
                    self.assertNotIn(key, facts[0])
                    self.assertNotIn(key, sent)
        self.assertEqual(self.sent()["facts"], [{"body": "From stdin."}])

    def test_remember_prints_the_structured_body_with_json(self):
        """Drives `cmd_remember`'s `--json` arm."""

        code, out, err = self.remember("--json", "A fact.")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), WRITTEN)

    def test_a_blank_statement_sends_nothing(self):
        """Drives `cmd_remember`'s empty-body refusal."""

        code, _, err = self.remember("  ", stdin="")
        self.assertEqual(code, 2)
        self.assertIn("Nothing to remember", err)
        self.assertEqual(self.fake.requests, [])


class RememberTokenTests(RememberTestCase):
    def test_remember_under_the_unscoped_token_sends_nothing(self):
        """Drives `token_for(write=True)` through `cmd_remember`, with only `*` stored."""

        del os.environ["SEMANTICLY_TOKEN"]
        self.install_keychain()
        cli_module = __import__("test_semanticly").cli
        cli_module.keychain_set("*", UNSCOPED_SECRET, origin=self.fake.url)
        code, _, err = self.remember("A fact.")
        self.assertEqual(code, 2)
        self.assertIn(f"No token for `{REPO}`. Run `semanticly init` in a terminal.", err)
        self.assertEqual(self.fake.requests, [])

    def test_remember_uses_the_repositorys_own_token(self):
        """Drives `token_for(write=True)`'s keychain arm through `cmd_remember`."""

        del os.environ["SEMANTICLY_TOKEN"]
        self.install_keychain()
        cli_module = __import__("test_semanticly").cli
        cli_module.keychain_set(REPO, REPO_SECRET, origin=self.fake.url)
        code, _, err = self.remember("A fact.")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.requests[0]["headers"]["authorization"],
                         f"Bearer {REPO_SECRET}")


class RememberSurfaceTests(RememberTestCase):
    def client_name(self) -> str:
        init = next(r for r in self.fake.requests if r["body"].get("method") == "initialize")
        return init["body"]["params"]["clientInfo"]["name"]

    def test_the_surface_flag_becomes_the_client_name(self):
        """Drives `add_surface_flag` → `cmd_remember` → `McpClient.open`."""

        self.remember("A fact.")
        self.assertEqual(self.client_name(), "claude-code")
        self.fake.requests.clear()
        self.remember("--surface", "cowork", "A fact.")
        self.assertEqual(self.client_name(), "cowork")
