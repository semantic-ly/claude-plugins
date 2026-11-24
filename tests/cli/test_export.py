"""Tests for `semanticly export`, both renderings.

Every test serves `GET /v1/repos/{repo}/facts` from `FakeAPI` and runs the real
`main`, so the walk, the paging and the render are all in the path under test.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest.mock

from test_keychain import REPO_SECRET, KeychainTestCase
from test_semanticly import CliTestCase, cli

OTHER_SECRET = "slt_other_99887766554433221100ffeeddccbb"

# --- fixtures ----------------------------------------------------------------


def fact(display_id: str, body: str, topic: str, sha: str, confidence: float = 0.0) -> dict:
    return {
        "id": display_id, "body": body, "topic": topic,
        "confidence": confidence, "sha": sha, "branch": "main",
    }


# Served newest-first, which is the route's own `ORDER BY f.seq DESC`.
PAGE_ONE = [
    fact("F-4", "Escalations page the on-call rotation.", "support", "d4d4d4"),
    fact("F-3", "Refunds are processed within 30 days.", "billing", "c3c3c3", 0.82),
]
PAGE_TWO = [
    fact("F-2", "Enterprise plans invoice annually.", "billing", "b2b2b2"),
    fact("F-1", "The support queue opens at 08:00 UTC.", "support", "a1a1a1"),
]


def paged_tag_route(method, path, query, body):
    """`GET /v1/repos/{repo}/facts?ref=v1.4`, two pages and a cursor."""

    assert method == "GET" and path.endswith("/facts"), (method, path)
    if query.get("cursor") == "cursor-page-2":
        return 200, {"repo": "support", "ref": "v1.4", "branch": "main",
                     "facts": PAGE_TWO, "has_more": False}
    return 200, {"repo": "support", "ref": "v1.4", "branch": "main",
                 "facts": PAGE_ONE, "has_more": True, "next_cursor": "cursor-page-2"}


# --- export ------------------------------------------------------------------


class ExportTests(CliTestCase):
    def test_two_runs_at_a_tag_produce_identical_bytes(self):
        """Drives `cmd_export` → `walk_facts` → `api` → `render_claude_md`, twice.

        Both runs go over the wire and through the render, so the pinned ref,
        the paging and the absence of a clock are all in the path being
        compared — not supplied by the setup.
        """

        api = self.serve(paged_tag_route)
        argv = ["export", "--repo", "acme/support", "--ref", "v1.4", "--format", "claude-md"]

        first_code, first, _ = self.run_cli(argv)
        second_code, second, _ = self.run_cli(argv)

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        self.assertEqual(len(api.requests), 4, "each run walks both pages")
        self.assertEqual(api.requests[0]["query"]["ref"], "v1.4")
        self.assertEqual(api.requests[1]["query"]["cursor"], "cursor-page-2")

    def test_the_walk_renders_every_page_in_ascending_seq_order(self):
        """Drives `walk_facts`'s paging and its single reversal.

        The route answers `seq DESC`, so an export that printed pages as served
        would read newest-first and one that re-sorted client side would be a
        second ordering. The expected order here is the reverse of the served
        order and nothing else.
        """

        self.serve(paged_tag_route)
        code, out, err = self.run_cli(
            ["export", "--repo", "acme/support", "--ref", "v1.4"])
        self.assertEqual(code, 0, err)
        served = [row["id"] for row in PAGE_ONE + PAGE_TWO]
        positions = [out.index(row["body"]) for row in PAGE_ONE + PAGE_TWO]
        rendered = [display for _, display in sorted(zip(positions, served, strict=True))]
        self.assertEqual(rendered, list(reversed(served)))

    def test_the_export_carries_no_clock(self):
        """Drives the rendered output of `cmd_export`."""

        self.serve(paged_tag_route)
        _, out, _ = self.run_cli(["export", "--repo", "acme/support", "--ref", "v1.4"])
        self.assertNotRegex(out, r"\d{4}-\d{2}-\d{2}")
        self.assertNotRegex(out, r"\d{2}:\d{2}:\d{2}")
        for word in ("ago", "last sync", "just now", "today", "yesterday"):
            self.assertNotIn(word, out.lower(), f"{word!r} makes two runs differ")

    def test_a_tag_export_promises_only_what_the_route_earns(self):
        """Drives `cmd_export`'s pinned discriminator and the header it selects."""

        self.serve(paged_tag_route)
        _, out, _ = self.run_cli(["export", "--repo", "acme/support", "--ref", "v1.4"])
        self.assertIn("with no merge in between, produce identical bytes", out)
        for overclaim in ("frozen", "immutable", "forever", "never changes"):
            self.assertNotIn(overclaim, out.lower())

    def test_an_export_of_main_does_not_promise_stability(self):
        """Drives the same discriminator on a moving basis."""

        self.serve(lambda *a: (200, {
            "repo": "support", "ref": "main", "branch": "main",
            "facts": PAGE_ONE, "has_more": False,
        }))
        _, out, _ = self.run_cli(["export", "--repo", "acme/support"])
        self.assertIn("moves as facts land", out)
        self.assertNotIn("identical bytes", out)

    def test_a_listing_that_drops_its_cursor_says_that_is_why(self):
        """Drives `walk_facts`'s STOPPED_NO_CURSOR arm and the sentence it picks.

        `has_more` with no `next_cursor` is the one shape that would otherwise
        render a part of a repo as though it were the whole of one. The wording
        assertion is cause-specific on purpose: this test must fail if the two
        stop causes are ever swapped.
        """

        self.serve(lambda *a: (200, {
            "repo": "support", "ref": "v1.4", "branch": "main",
            "facts": PAGE_ONE, "has_more": True,
        }))
        code, out, _ = self.run_cli(["export", "--repo", "acme/support", "--ref", "v1.4"])
        self.assertEqual(code, 0)
        self.assertIn("handed back no cursor to continue with", out)
        self.assertIn("a part, not the whole", out)
        self.assertNotIn("page limit", out, "the bound did not fire; do not say it did")
        # Structural: without the blank line the fact list folds into the
        # blockquote and renders as part of the warning.
        self.assertRegex(out, r"not the whole\.\n\n- ")

    def test_the_walk_stops_at_the_page_bound_and_says_the_bound_is_why(self):
        """Drives `walk_facts`'s MAX_PAGES arm.

        A server that never stops handing back a cursor would otherwise spin an
        unattended hook forever. The bound and a dropped cursor both end in a
        partial export and they are DIFFERENT facts, so each renders its own
        sentence — this one names the bound the reader can raise.
        """

        api = self.serve(lambda *a: (200, {
            "repo": "support", "ref": "v1.4", "branch": "main",
            "facts": PAGE_ONE, "has_more": True, "next_cursor": "always-more",
        }))
        with unittest.mock.patch.object(cli, "MAX_PAGES", 3):
            code, out, _ = self.run_cli(
                ["export", "--repo", "acme/support", "--ref", "v1.4"])
        self.assertEqual(code, 0)
        self.assertEqual(len(api.requests), 3)
        self.assertIn("stopped at its own 3-page limit with a cursor still in hand", out)
        self.assertIn("a part, not the whole", out)
        self.assertNotIn(
            "handed back no cursor", out,
            "the server had a cursor; saying it ran dry misreports why this is short")
        self.assertRegex(out, r"raise MAX_PAGES\.\n\n- ")


# --- the interchange JSON ------------------------------------------------------

# The keys `export --json` may write, and nothing else.
INTERCHANGE_KEYS = {
    "body", "kind", "term", "aliases", "topics", "rationale", "confidence",
    "valid_from", "valid_to", "assertions", "display_id", "sha",
}

SHA_A = "sha256:" + "a1" * 32
SHA_B = "sha256:" + "b2" * 32


# Served newest-first. The definition carries every field and the claim only
# what an unadorned write carries, as the listing prints them.
INTERCHANGE_ROWS = [
    {
        "id": "f-0002", "display_id": "f-0002", "body": "A VP refund is a refund over $900.",
        "topic": "billing", "topics": ["billing", "refunds"], "confidence": 0.75,
        "sha": SHA_B[7:13], "sha_full": SHA_B, "branch": "v1.4",
        "kind": "definition", "term": "VP refund", "aliases": ["vp sign-off"],
        "rationale": "Finance said so.", "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-12-31T23:59:59Z",
        "assertions": [{"subject": "refund", "predicate": "needs", "object": "VP sign-off"}],
    },
    {
        "id": "f-0001", "display_id": "f-0001", "body": "Invoices go out on the first.",
        "topic": "billing", "topics": ["billing"], "confidence": None,
        "sha": SHA_A[7:13], "sha_full": SHA_A, "branch": "v1.4", "kind": "claim",
    },
]


def interchange_route(method, path, query, body):
    """The tag's listing in two pages, so the walk's paging is in the path."""

    assert method == "GET" and path.endswith("/facts"), (method, path)
    page = {"repo": "support", "ref": "v1.4", "branch": "main"}
    if query.get("cursor") == "cursor-page-2":
        return 200, {**page, "facts": INTERCHANGE_ROWS[1:], "has_more": False}
    return 200, {**page, "facts": INTERCHANGE_ROWS[:1], "has_more": True,
                 "next_cursor": "cursor-page-2"}


class ExportJsonTests(CliTestCase):
    argv = ["export", "--repo", "acme/support", "--ref", "v1.4", "--json"]

    def export_json(self) -> dict:
        self.serve(interchange_route)
        code, out, err = self.run_cli(self.argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_export_json_is_schema_1_with_the_named_fields(self):
        """Drives `cmd_export` → `walk_facts` → `interchange_entry`."""

        doc = self.export_json()
        self.assertEqual(set(doc), {"schema", "facts"})
        self.assertEqual(doc["schema"], 1)
        definition, claim = doc["facts"][1], doc["facts"][0]
        self.assertEqual([e["body"] for e in doc["facts"]],
                         [row["body"] for row in reversed(INTERCHANGE_ROWS)],
                         "ascending seq, the reverse of the served order")
        self.assertEqual(definition, {
            "body": "A VP refund is a refund over $900.", "kind": "definition",
            "term": "VP refund", "aliases": ["vp sign-off"], "topics": ["billing", "refunds"],
            "rationale": "Finance said so.", "confidence": 0.75,
            "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-12-31T23:59:59Z",
            "assertions": [{"subject": "refund", "predicate": "needs", "object": "VP sign-off"}],
            "display_id": "f-0002", "sha": SHA_B,
        })
        # An absent field is omitted, and a null confidence is absent too.
        self.assertEqual(claim, {
            "body": "Invoices go out on the first.", "kind": "claim", "topics": ["billing"],
            "display_id": "f-0001", "sha": SHA_A,
        })
        for entry in doc["facts"]:
            self.assertLessEqual(set(entry), INTERCHANGE_KEYS)

    def test_display_id_and_sha_are_carried(self):
        """Drives `interchange_entry`'s provenance pair, the whole hash and never the prefix."""

        doc = self.export_json()
        self.assertEqual([(e["display_id"], e["sha"]) for e in doc["facts"]],
                         [("f-0001", SHA_A), ("f-0002", SHA_B)])

    def test_two_exports_at_a_tag_are_identical(self):
        """Drives the whole JSON road twice over the wire."""

        api = self.serve(interchange_route)
        first_code, first, _ = self.run_cli(self.argv)
        second_code, second, _ = self.run_cli(self.argv)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))
        self.assertEqual(len(api.requests), 4, "each run walks both pages")

    def test_a_json_export_that_drops_its_cursor_writes_nothing_and_exits_1(self):
        """Drives `cmd_export`'s refusal on `walk_facts`'s STOPPED_NO_CURSOR arm."""

        self.serve(lambda *a: (200, {
            "repo": "support", "ref": "v1.4", "branch": "main",
            "facts": INTERCHANGE_ROWS[:1], "has_more": True,
        }))
        code, out, err = self.run_cli(self.argv)
        self.assertEqual((code, out), (1, ""), "a part must never look like the whole")
        self.assertIn("handed back no cursor to continue with", err)

    def test_a_json_export_at_the_page_bound_writes_nothing_and_exits_1(self):
        """Drives `cmd_export`'s refusal on `walk_facts`'s MAX_PAGES arm."""

        self.serve(lambda *a: (200, {
            "repo": "support", "ref": "v1.4", "branch": "main",
            "facts": INTERCHANGE_ROWS[:1], "has_more": True, "next_cursor": "always-more",
        }))
        with unittest.mock.patch.object(cli, "MAX_PAGES", 2):
            code, out, err = self.run_cli(self.argv)
        self.assertEqual((code, out), (1, ""), "a part must never look like the whole")
        self.assertIn("stopped at its own 2-page limit", err)

    def test_json_and_format_are_refused_together(self):
        """Drives the parser's exclusive rendering group."""

        self.serve(interchange_route)
        with self.assertRaises(SystemExit) as raised, \
                contextlib.redirect_stderr(io.StringIO()):
            self.run_cli([*self.argv, "--format", "claude-md"])
        self.assertEqual(raised.exception.code, 2)


class ExportTokenTests(KeychainTestCase):
    def test_export_walks_with_the_repository_s_own_token(self):
        """Drives `cmd_export` → `token_for` → the keychain shim → the walk's header."""

        api = self.serve_without_token(interchange_route)
        self.install_keychain()
        self.seed("acme/support", REPO_SECRET)
        self.seed("acme/other", OTHER_SECRET)

        for repo, secret in (("acme/support", REPO_SECRET), ("acme/other", OTHER_SECRET)):
            before = len(api.requests)
            code, _, err = self.run_cli(["export", "--repo", repo, "--ref", "v1.4", "--json"])
            self.assertEqual(code, 0, err)
            walk = api.requests[before:]
            self.assertEqual({r["auth"] for r in walk}, {f"Bearer {secret}"}, repo)
