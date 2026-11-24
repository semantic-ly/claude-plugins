"""Tests for `semanticly init` and the branch list it prints.

Every run drives the real file in process against `fakes.FakeMount`, which
answers `list_repos` over `/mcp`, the exchange at `/mcp/cli-token` and the
three `/v1` routes `init` reads and writes. The keychain is
`fakes.keychain_shim` on `PATH`. A TTY run swaps stdin and stdout for streams
that report a terminal, which is the only thing the CLI asks of them.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from datetime import UTC, datetime, timedelta

import fakes
from test_semanticly import REPO_ROOT, CliTestCase, cli

UNSCOPED_SECRET = "slt_unscoped_00112233445566778899aabbccdd"
REPO_SECRET = "slt_repo_fedcba9876543210fedcba98765432"
CI_SECRET = "slt_ci_0123456789"

GUIDANCE = ("a team of agents on one project should share one branch; give an untrusted "
            "or external writer its own.")
CLAUDE_CODE_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
TERMINAL_SENTENCE = "The branch list needs `semanticly init` or `semanticly login` in a terminal."
CONSOLE = "http://console.test"

BRANCHES_PATH = "/v1/repos/acme/support/branches"
BINDING_PATH = "/v1/repos/acme/support/binding"
CLIENT_BINDING_PATH = "/v1/repos/acme/support/client-binding"

REPOS = {"repos": [
    {"repo": "acme/support", "org": "acme", "slug": "support", "level": "write",
     "description": ""},
    {"repo": "acme/docs", "org": "acme", "slug": "docs", "level": "read", "description": ""},
    {"repo": "beta/ops", "org": "beta", "slug": "ops", "level": "admin", "description": ""},
]}


def stamp(when: datetime) -> str:
    """`when` as the route writes it, RFC 3339 in UTC."""

    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def branch_rows(now: datetime) -> list[dict]:
    """The route's answer, in the order it sorts, never `main` or `agents/system/`."""

    return [
        {"name": "agents/team/shared", "bound_connections": 3,
         "last_write_at": stamp(now - timedelta(hours=3, minutes=1)), "bound": False},
        {"name": "agents/jordan/claude-code", "bound_connections": 1,
         "last_write_at": stamp(now - timedelta(days=2, minutes=1)), "bound": True},
        {"name": "agents/kim/codex", "bound_connections": 0,
         "last_write_at": stamp(now - timedelta(minutes=5, seconds=10)), "bound": False},
        {"name": "agents/x/idle", "bound_connections": 0, "last_write_at": None,
         "bound": False},
    ]


EXPECTED_ROWS = [
    "  1. agents/team/shared",
    "     shared by 3 connections · last write 3h ago",
    "  2. agents/jordan/claude-code",
    "     shared by 1 connection · last write 2d ago · bound",
    "  3. agents/kim/codex",
    "     no connections · last write 5m ago",
    "  4. agents/x/idle",
    "     no connections · no writes yet",
]


def bound_answer(branch: str) -> dict:
    return fakes.http_answer(200, {"repo": "acme/support", "branch": branch,
                                   "bound_by": "jordan", "bound_at": "2026-11-07T12:00:00Z"})


class Terminal(io.StringIO):
    """A stream that reports a terminal."""

    def isatty(self) -> bool:
        return True


class InitTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.shim = self.install_keychain()

    def mount(self, **overrides) -> fakes.FakeMount:
        """A mount answering every route `init` takes, SEMANTICLY_TOKEN unset."""

        script = {
            "list_repos": fakes.tool_result("", REPOS),
            "POST /mcp/cli-token": fakes.http_answer(201, {
                "token": REPO_SECRET, "token_id": "tok-1", "repo": "acme/support",
                "scopes": ["read", "write"], "level": "write",
                "expires_at": "2027-02-05T12:00:00Z", "console_url": CONSOLE,
                "username": "jordan"}),
            f"GET {BRANCHES_PATH}": fakes.http_answer(
                200, {"branches": branch_rows(datetime.now(UTC))}),
            f"PUT {BINDING_PATH}": bound_answer("agents/jordan/claude-code"),
            f"PUT {CLIENT_BINDING_PATH}": bound_answer("agents/jordan/claude-code"),
            **overrides,
        }
        mount = fakes.FakeMount(script)
        self.addCleanup(mount.close)
        os.environ["SEMANTICLY_API_URL"] = mount.url
        os.environ.pop("SEMANTICLY_TOKEN", None)
        return mount

    def sign_in(self, *, repo_token: bool) -> None:
        """What `login` leaves behind, the unscoped token and the console keys."""

        cli.keychain_set("*", UNSCOPED_SECRET)
        if repo_token:
            cli.keychain_set("acme/support", REPO_SECRET)
        cli.write_config({"console_url": CONSOLE, "username": "jordan"})

    def keychain_writes(self) -> list[dict]:
        return [c for c in self.shim.calls()
                if c["argv"][:1] in (["-i"], ["store"])]

    def run_tty(self, argv, typed: str = "") -> tuple[int, str, str]:
        """`main` with a terminal on stdin and stdout, `typed` as the person's input."""

        out, err = Terminal(), io.StringIO()
        real = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = Terminal(typed), out
        try:
            with contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            sys.stdin, sys.stdout = real
        return code, out.getvalue(), err.getvalue()

    def marker(self):
        return self.project / ".semanticly"

    def requests_to(self, mount: fakes.FakeMount, method: str, path: str) -> list[dict]:
        return [r for r in mount.requests if r["method"] == method and r["path"] == path]

    def puts(self, mount: fakes.FakeMount) -> list[dict]:
        return [r for r in mount.requests if r["method"] == "PUT"]


# --- the branch list --------------------------------------------------


class BranchListTests(InitTestCase):
    def render(self, rows, now) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_branch_list(rows, now=now)
        return out.getvalue()

    def test_the_rows_print_in_the_routes_order_with_their_mono_lines(self):
        """Drives `print_branch_list` over every mono form and `bound`."""

        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        lines = self.render(branch_rows(now), now).splitlines()
        self.assertEqual(lines[1:], EXPECTED_ROWS)

    def test_the_guidance_sentence_comes_first_byte_for_byte(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        self.assertEqual(cli.GUIDANCE, GUIDANCE)
        self.assertEqual(self.render(branch_rows(now), now).splitlines()[0], GUIDANCE)
        self.assertEqual(self.render([], now), GUIDANCE + "\n",
                         "a repository with no bindable branch shows the sentence alone")

    def test_a_non_numeric_sharing_count_prints_no_sharing_clause(self):
        """Drives `mono_line` → `sharing_phrase` over a count the route never sends."""

        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        rows = [{"name": "agents/x/odd", "bound_connections": "many",
                 "last_write_at": stamp(now - timedelta(hours=3, minutes=1))}]
        self.assertEqual(self.render(rows, now).splitlines()[1:],
                         ["  1. agents/x/odd", "     last write 3h ago"])


# --- `init` with a TTY -------------------------------------------


class InteractiveInitTests(InitTestCase):
    def test_a_tty_run_writes_semanticly_and_both_bindings(self):
        """Drives `cmd_init` → `init_interactive` → `bind_both` over both PUT routes."""

        mount = self.mount()
        self.sign_in(repo_token=True)

        code, out, err = self.run_tty(["init"], typed="1\n2\n")

        self.assertEqual(code, 0, err)
        self.assertEqual(self.marker().read_text(encoding="utf-8"), "acme/support\n")
        self.assertIn("  1. acme/support", out)
        self.assertIn("  2. beta/ops", out)
        self.assertNotIn("acme/docs", out, "a repository held at read is not offered")
        for row in EXPECTED_ROWS:
            self.assertIn(row, out)
        self.assertIn(f"{CONSOLE}/acme/support/branches", out)
        self.assertIn("agents/jordan/claude-code", out)

        binding = self.requests_to(mount, "PUT", BINDING_PATH)
        client = self.requests_to(mount, "PUT", CLIENT_BINDING_PATH)
        self.assertEqual([r["body"] for r in binding], [{"branch": "agents/jordan/claude-code"}])
        self.assertEqual([r["body"] for r in client], [
            {"client_id": CLAUDE_CODE_CLIENT_ID, "branch": "agents/jordan/claude-code"}])
        self.assertEqual(cli.CLAUDE_CODE_CLIENT_ID, CLAUDE_CODE_CLIENT_ID)
        for request in [*binding, *client, *self.requests_to(mount, "GET", BRANCHES_PATH)]:
            self.assertEqual(request["headers"]["authorization"], f"Bearer {REPO_SECRET}")
        listing = [r for r in mount.requests if (r["body"] or {}).get("method") == "tools/call"]
        self.assertEqual(listing[0]["headers"]["authorization"], f"Bearer {UNSCOPED_SECRET}")
        self.assertEqual(self.requests_to(mount, "POST", "/mcp/cli-token"), [],
                         "a held repository token is not minted again")

    def test_a_tty_run_mints_the_repositorys_token_when_none_is_held(self):
        """Drives `init_interactive`'s mint over `POST /mcp/cli-token` into the keychain."""

        mount = self.mount()
        self.sign_in(repo_token=False)

        code, _, err = self.run_tty(["init", "--repo", "acme/support"], typed="1\n")

        self.assertEqual(code, 0, err)
        mints = self.requests_to(mount, "POST", "/mcp/cli-token")
        self.assertEqual([r["body"] for r in mints], [{"repo": "acme/support"}])
        self.assertEqual(mints[0]["headers"]["authorization"], f"Bearer {UNSCOPED_SECRET}")
        host = os.environ["SEMANTICLY_API_URL"].removeprefix("http://")
        self.assertEqual(self.shim.entries()[("semanticly", f"{host}:acme/support")],
                         REPO_SECRET)
        for request in self.puts(mount):
            self.assertEqual(request["headers"]["authorization"], f"Bearer {REPO_SECRET}")
        self.assertEqual([r["body"]["branch"] for r in self.puts(mount)],
                         ["agents/team/shared", "agents/team/shared"])

    def test_a_repo_flag_naming_no_write_row_is_refused(self):
        """Drives `init_interactive`'s preselect against the `write` and `admin` rows."""

        mount = self.mount()
        self.sign_in(repo_token=True)

        code, _, err = self.run_tty(["init", "--repo", "acme/docs"], typed="1\n")

        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("acme/docs", err)
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.puts(mount), [])

    def test_a_reserved_branch_relays_branch_reserved(self):
        """Drives `bind_both`'s first PUT into a refusal that stops the second."""

        sentence = "Branch names under `agents/system/` are reserved."
        mount = self.mount(**{f"PUT {BINDING_PATH}": fakes.http_answer(
            422, {"error": {"code": "branch_reserved", "message": sentence}})})
        self.sign_in(repo_token=True)

        code, _, err = self.run_tty(["init", "--repo", "acme/support"], typed="1\n")

        self.assertEqual(code, cli.EXIT_API)
        self.assertIn("422 branch_reserved", err)
        self.assertIn(sentence, err)
        self.assertEqual(self.requests_to(mount, "PUT", CLIENT_BINDING_PATH), [])

    def test_a_refusal_from_the_branches_route_binds_nothing(self):
        """Drives the branch list's refusal through `init_interactive`."""

        sentence = "This needs write on acme/support."
        mount = self.mount(**{f"GET {BRANCHES_PATH}": fakes.http_answer(
            403, {"error": {"code": "insufficient_level", "message": sentence}})})
        self.sign_in(repo_token=True)

        code, _, err = self.run_tty(["init", "--repo", "acme/support"], typed="1\n")

        self.assertEqual(code, cli.EXIT_API)
        self.assertIn(sentence, err)
        self.assertEqual(self.puts(mount), [])

    def test_a_non_ascii_digit_is_refused_and_binds_nothing(self):
        """Drives `read_choice` → `choice_index` over a superscript two."""

        mount = self.mount()
        self.sign_in(repo_token=True)

        code, _, err = self.run_tty(["init", "--repo", "acme/support"], typed="\u00b2\n")

        self.assertEqual(code, cli.EXIT_USAGE, err)
        self.assertIn("is not a number from 1 to 4", err)
        self.assertEqual(self.puts(mount), [])

    def test_init_never_names_main_or_agents_system(self):
        """Drives the choice over the route's rows, and nothing past them binds."""

        mount = self.mount()
        self.sign_in(repo_token=True)

        code, out, err = self.run_tty(["init", "--repo", "acme/support"], typed="5\n")

        self.assertEqual(code, cli.EXIT_USAGE, "a number past the route's rows is refused")
        numbered = [line.split(". ", 1)[1] for line in out.splitlines()
                    if line.startswith("  ") and line.strip()[:1].isdigit()]
        self.assertEqual(numbered, [row["name"] for row in branch_rows(datetime.now(UTC))])
        self.assertNotIn("main", numbered)
        self.assertNotIn("agents/system/", out)
        self.assertEqual(self.puts(mount), [])


# --- `init` read-only ---------------------------------------------


class ReadOnlyInitTests(InitTestCase):
    def test_a_read_only_run_writes_and_binds_nothing(self):
        """Drives `cmd_init` → `init_read_only` with the repository's token held."""

        mount = self.mount()
        self.sign_in(repo_token=True)
        writes_before = len(self.keychain_writes())

        code, out, err = self.run_cli(["init", "--repo", "acme/support"])

        self.assertEqual(code, 0, err)
        self.assertFalse(self.marker().exists())
        self.assertEqual(len(self.keychain_writes()), writes_before)
        self.assertEqual(self.puts(mount), [])
        self.assertEqual(self.requests_to(mount, "POST", "/mcp/cli-token"), [])
        self.assertIn("acme/support", out)
        self.assertIn("beta/ops", out)
        self.assertNotIn("acme/docs", out)
        self.assertIn(GUIDANCE, out)
        for row in EXPECTED_ROWS:
            self.assertIn(row, out)
        self.assertIn(f"{CONSOLE}/acme/support/connections", out)
        self.assertLess(out.index(GUIDANCE), out.index(f"{CONSOLE}/acme/support/connections"))

    def test_a_read_only_run_without_the_repos_token_prints_the_terminal_sentence(self):
        """Drives `init_read_only`'s arm for a repository the keychain holds no token for."""

        mount = self.mount()
        self.sign_in(repo_token=False)
        self.marker().write_text("acme/support\n", encoding="utf-8")

        code, out, err = self.run_cli(["init"])

        self.assertEqual(code, 0, err)
        self.assertIn(TERMINAL_SENTENCE, out)
        self.assertNotIn(GUIDANCE, out)
        self.assertIn(f"{CONSOLE}/acme/support/connections", out)
        self.assertEqual(self.requests_to(mount, "GET", BRANCHES_PATH), [])
        self.assertEqual(self.requests_to(mount, "POST", "/mcp/cli-token"), [])

    def test_semanticly_token_forces_the_read_only_run(self):
        """Drives `cmd_init`'s choice with a terminal on both streams."""

        mount = self.mount()
        self.sign_in(repo_token=False)
        os.environ["SEMANTICLY_TOKEN"] = CI_SECRET

        code, out, err = self.run_tty(["init", "--repo", "acme/support"], typed="1\n1\n")

        self.assertEqual(code, 0, err)
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.puts(mount), [])
        self.assertEqual(self.requests_to(mount, "POST", "/mcp/cli-token"), [])
        listing = [r for r in mount.requests if (r["body"] or {}).get("method") == "tools/call"]
        self.assertEqual(listing[0]["headers"]["authorization"], f"Bearer {CI_SECRET}")
        [branches] = self.requests_to(mount, "GET", BRANCHES_PATH)
        self.assertEqual(branches["headers"]["authorization"], f"Bearer {CI_SECRET}")
        self.assertIn(GUIDANCE, out)
        self.assertIn(f"{CONSOLE}/acme/support/connections", out)
