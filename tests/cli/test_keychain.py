"""Tests for the CLI's keychain and the token each call carries.

The keychain is `fakes.keychain_shim`, real executables named `security` and
`secret-tool` alone on `PATH`. The CLI runs them exactly as it runs the OS
tools, so every test drives `keychain_backend`, `keychain_set`, `keychain_get`
and `token_for` through the subprocess calls a real machine makes.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest.mock
import urllib.parse

import fakes
from test_semanticly import CLI_PATH, CliTestCase, FakeAPI, cli

SECRET = "slt_0123456789abcdef0123456789abcdef"
REPO_SECRET = "slt_repo_fedcba9876543210fedcba98765432"
UNSCOPED_SECRET = "slt_unscoped_00112233445566778899aabbccdd"

NOT_SIGNED_IN = "Not signed in. Run `semanticly login`."
NO_REPO_TOKEN = "No token for `acme/support`. Run `semanticly init` in a terminal."
NO_KEYCHAIN = (
    "No OS keychain is available (`security` or `secret-tool`), so `login` "
    "stores nothing. A script can set SEMANTICLY_TOKEN."
)


# What each command these tests run is answered. `recall` goes over `/mcp`, and
# `whoami` and `remember` over `/v1`.
SCRIPT = {
    "recall": fakes.tool_result("", {"lines": [], "text": "", "level": "read"}),
    "GET /v1/whoami": fakes.http_answer(200, {"org": "jordan", "scopes": ["read"]}),
    "remember": fakes.tool_result("", {"written": []}),
}


class KeychainTestCase(CliTestCase):
    def host(self) -> str:
        return urllib.parse.urlsplit(os.environ["SEMANTICLY_API_URL"]).netloc

    def serve_without_token(self, route) -> FakeAPI:
        """A `/v1` stand-in the CLI is pointed at, with SEMANTICLY_TOKEN unset."""

        api = self.serve(route)
        os.environ.pop("SEMANTICLY_TOKEN")
        return api

    def mount_without_token(self) -> fakes.FakeMount:
        """A mount the CLI is pointed at, with SEMANTICLY_TOKEN unset."""

        mount = fakes.FakeMount(SCRIPT)
        self.addCleanup(mount.close)
        os.environ["SEMANTICLY_API_URL"] = mount.url
        os.environ.pop("SEMANTICLY_TOKEN", None)
        return mount

    def seed(self, scope: str, secret: str) -> None:
        """Stores through `keychain_set`, the road `login` and `init` take."""

        cli.keychain_set(scope, secret)

    def run_cli_calling(self, func, *args):
        """Calls `func` with stdout and stderr captured, as `run_cli` does."""

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = func(*args)
        return result, out.getvalue(), err.getvalue()

    def run_process(self, argv) -> subprocess.CompletedProcess:
        """The real file in a child, so a tool's output would reach its streams."""

        return subprocess.run([sys.executable, str(CLI_PATH), *argv],
                              env=dict(os.environ), capture_output=True,
                              text=True, timeout=30)

    def everything_under_home(self) -> str:
        return "".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in self.home.rglob("*") if p.is_file())


# --- the token for each call -----------------------------------------------


class TokenOrderTests(KeychainTestCase):
    def test_semanticly_token_wins_over_the_keychain(self):
        """Drives `token_for`'s first arm through `cmd_recall` and `cmd_remember`."""

        api = self.mount_without_token()
        shim = self.install_keychain()
        self.seed("acme/support", REPO_SECRET)
        self.seed("*", UNSCOPED_SECRET)
        seeded_calls = len(shim.calls())
        os.environ["SEMANTICLY_TOKEN"] = "slt_ci"

        for argv in (["recall", "--repo", "acme/support", "q"],
                     ["remember", "--repo", "acme/support", "a fact"],
                     ["whoami"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(api.requests[-1]["headers"]["authorization"], "Bearer slt_ci")
        self.assertEqual(len(shim.calls()), seeded_calls,
                         "SEMANTICLY_TOKEN set means the keychain is never read")

    def test_a_malformed_semanticly_token_is_refused_without_echoing_it(self):
        """Drives `token_for`'s first arm against TOKEN_RULE, in a child process.

        An interior newline or control character would reach `http.client`,
        which raises with the header value in its message. The refusal comes
        first and never prints the value.
        """

        api = self.mount_without_token()
        for value in ("slt_secret\nInjected: 1", "slt_sec\x01ret"):
            with self.subTest(value=value):
                os.environ["SEMANTICLY_TOKEN"] = value
                done = self.run_process(["whoami"])
                self.assertEqual(done.returncode, cli.EXIT_USAGE, done.stderr)
                self.assertEqual(done.stderr.strip(),
                                 "semanticly: SEMANTICLY_TOKEN is not a token.")
                self.assertNotIn("slt_sec", done.stderr + done.stdout)
        self.assertEqual(api.requests, [])

    def test_a_repos_token_wins_over_the_unscoped_one(self):
        """Drives `token_for`'s keychain arms through `cmd_recall` and `cmd_whoami`."""

        api = self.mount_without_token()
        shim = self.install_keychain()
        self.seed("acme/support", REPO_SECRET)
        self.seed("*", UNSCOPED_SECRET)
        self.assertEqual(shim.entries(), {
            ("semanticly", f"{self.host()}:acme/support"): REPO_SECRET,
            ("semanticly", f"{self.host()}:*"): UNSCOPED_SECRET,
        })

        code, _, err = self.run_cli(["recall", "--repo", "acme/support", "q"])
        self.assertEqual(code, 0, err)
        self.assertEqual(api.requests[-1]["headers"]["authorization"], f"Bearer {REPO_SECRET}")

        code, _, err = self.run_cli(["recall", "--repo", "acme/other", "q"])
        self.assertEqual(code, 0, err)
        self.assertEqual(api.requests[-1]["headers"]["authorization"], f"Bearer {UNSCOPED_SECRET}",
                         "a read with no repository token falls back to the unscoped one")

        code, _, err = self.run_cli(["whoami"])
        self.assertEqual(code, 0, err)
        self.assertEqual(api.requests[-1]["headers"]["authorization"], f"Bearer {UNSCOPED_SECRET}")

    def test_no_token_says_run_login(self):
        """Drives `token_for`'s last arm, with an empty keychain and with none."""

        api = self.mount_without_token()
        for tools in ((), ("security", "secret-tool")):
            with self.subTest(tools=tools):
                self.install_keychain(tools)
                code, _, err = self.run_cli(["recall", "--repo", "acme/support", "q"])
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn(NOT_SIGNED_IN, err)
                code, _, err = self.run_cli(["whoami"])
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn(NOT_SIGNED_IN, err)
        self.assertEqual(api.requests, [])

    def test_a_write_with_no_repos_token_says_run_init(self):
        """Drives `token_for(write=True)` through `cmd_remember` and `cmd_ingest`."""

        api = self.mount_without_token()
        self.install_keychain()
        self.seed("*", UNSCOPED_SECRET)
        self.seed("acme/other", REPO_SECRET)

        prepared = self.home / "slice.json"
        prepared.write_text('[{"uuid": "u1", "role": "user", "content": []}]', encoding="utf-8")
        for argv in (["remember", "--repo", "acme/support", "a fact"],
                     ["ingest", "--repo", "acme/support", "--session-id", "s",
                      "--file", str(prepared)]):
            with self.subTest(command=argv[0]):
                code, _, err = self.run_cli(argv)
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn(NO_REPO_TOKEN, err)
                self.assertNotIn(UNSCOPED_SECRET, err)
        self.assertEqual(api.requests, [], "the refusal comes before any request")


# --- the keychain entries ----------------------------------------------------


class KeychainBackendTests(KeychainTestCase):
    def test_the_security_shim_receives_the_secret_on_stdin_and_never_argv(self):
        """Drives `keychain_set` and `keychain_get` over `security` on darwin."""

        api = self.mount_without_token()
        shim = self.install_keychain()
        with unittest.mock.patch.object(cli.sys, "platform", "darwin"):
            self.assertEqual(cli.keychain_backend(), str(self.tools / "security"))
            _, out, err = self.run_cli_calling(cli.keychain_set, "acme/support", SECRET)
            self.assertEqual(cli.keychain_get("acme/support"), SECRET)
            self.assertIsNone(cli.keychain_get("acme/missing"))
            rc, rout, rerr = self.run_cli(["recall", "--repo", "acme/support", "q"])

        self.assertEqual(rc, 0, rerr)
        self.assertEqual(api.requests[-1]["headers"]["authorization"], f"Bearer {SECRET}")
        account = f"{self.host()}:acme/support"
        self.assertEqual(shim.entries(), {("semanticly", account): SECRET})

        calls = shim.calls()
        self.assertTrue(calls)
        self.assertEqual({c["tool"] for c in calls}, {"security"})
        store = calls[0]
        self.assertEqual(store["argv"], ["-i"])
        self.assertIn("add-generic-password -U -s semanticly", store["stdin"])
        self.assertIn(SECRET, store["stdin"])
        for call in calls:
            self.assertNotIn(SECRET, " ".join(call["argv"]), "the secret reached argv")
        self.assertIn(["find-generic-password", "-s", "semanticly", "-a", account, "-w"],
                      [c["argv"] for c in calls])

        for stream in (out, err, rout, rerr):
            self.assertNotIn(SECRET, stream)
        self.assertNotIn(SECRET, self.everything_under_home())

    def test_a_looked_up_secret_never_reaches_the_cli_s_own_streams(self):
        """Drives `keychain_get` in a child on this machine's own backend.

        Both shims print the secret on a lookup, as the real tools do, so a
        child that let the tool inherit its stdout would show it here.
        """

        api = self.mount_without_token()
        self.install_keychain()
        self.seed("acme/support", SECRET)
        run = self.run_process(["recall", "--repo", "acme/support", "q"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(api.requests[-1]["headers"]["authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, run.stdout)
        self.assertNotIn(SECRET, run.stderr)

    def test_a_secret_that_could_break_the_command_line_is_refused(self):
        """Drives `keychain_set`'s check before it writes on `security -i`'s stdin."""

        self.mount_without_token()
        shim = self.install_keychain()
        with unittest.mock.patch.object(cli.sys, "platform", "darwin"):
            for secret in ("slt_a\nadd-generic-password -s x -a y -w z", 'slt_"quoted"',
                           "slt_ space", ""):
                with self.subTest(secret=secret):
                    with self.assertRaises(cli.CliError) as caught:
                        cli.keychain_set("acme/support", secret)
                    self.assertNotIn("slt_", str(caught.exception))
        self.assertEqual(shim.calls(), [], "nothing reached the tool")

    def test_secret_tool_stores_and_looks_up_by_service_and_account(self):
        """Drives `keychain_set` and `keychain_get` over `secret-tool` off darwin."""

        self.mount_without_token()
        shim = self.install_keychain()
        with unittest.mock.patch.object(cli.sys, "platform", "linux"):
            self.assertEqual(cli.keychain_backend(), str(self.tools / "secret-tool"),
                             "`security` is not used off macOS")
            _, out, err = self.run_cli_calling(cli.keychain_set, "*", SECRET)
            self.assertEqual(cli.keychain_get("*"), SECRET)
            self.assertIsNone(cli.keychain_get("acme/support"))

        account = f"{self.host()}:*"
        store, lookup = shim.calls()[0], shim.calls()[1]
        self.assertEqual(store["argv"][0], "store")
        self.assertTrue(store["argv"][1].startswith("--label="))
        self.assertEqual(store["argv"][2:], ["service", "semanticly", "account", account])
        self.assertEqual(store["stdin"], SECRET)
        self.assertEqual(lookup["argv"], ["lookup", "service", "semanticly", "account", account])
        for call in shim.calls():
            self.assertNotIn(SECRET, " ".join(call["argv"]))
        for stream in (out, err):
            self.assertNotIn(SECRET, stream)
        self.assertNotIn(SECRET, self.everything_under_home())

    def test_no_keychain_tool_refuses_to_store(self):
        """Drives `keychain_backend`'s None arm through `keychain_set` and `keychain_get`."""

        self.mount_without_token()
        self.assertIsNone(cli.keychain_backend())
        with self.assertRaises(cli.CliError) as caught:
            cli.keychain_set("*", SECRET)
        self.assertEqual(str(caught.exception), NO_KEYCHAIN)
        self.assertIsNone(cli.keychain_get("*"))
        self.assertNotIn(SECRET, self.everything_under_home())

    def test_the_account_never_carries_userinfo(self):
        """Drives `keychain_account` over a base URL holding a password."""

        api = self.mount_without_token()
        shim = self.install_keychain()
        parsed = urllib.parse.urlsplit(api.url)
        os.environ["SEMANTICLY_API_URL"] = f"http://bob:hunter2@{parsed.netloc}/v1"
        cli.keychain_get("*")
        self.assertNotIn("hunter2", repr(shim.calls()))
        self.assertNotIn("bob", repr(shim.calls()))

