"""Tests for the CLI's MCP client and the commands that stay on `/v1`.

`mcp_call` speaks Streamable HTTP to `fakes.FakeMount`, a real server that
records every request, so each test drives `McpClient`, `read_rpc_response`
and the shared opener over the wire. `whoami` runs over `/v1` against the
same mount.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse

import fakes
from test_semanticly import CLI_PATH, CliTestCase, cli

UNSCOPED_SECRET = "slt_unscoped_00112233445566778899aabbccdd"
REPO_SECRET = "slt_repo_fedcba9876543210fedcba98765432"
ACCEPT = "application/json, text/event-stream"
INVALID_TOKEN = {"error": {"code": "invalid_token",
                           "message": "That token is unknown, expired, or revoked."}}


class McpTestCase(CliTestCase):
    def mount(self, script=None) -> fakes.FakeMount:
        mount = fakes.FakeMount(script)
        self.addCleanup(mount.close)
        return mount

    def call(self, mount, tool="list_repos", arguments=None, repo=None,
             surface="claude-code"):
        """`mcp_call` against `mount`, with the token from SEMANTICLY_TOKEN."""

        os.environ.setdefault("SEMANTICLY_TOKEN", "slt_test")
        return cli.mcp_call(cli.Config(mount.url), repo, tool, arguments or {}, surface)

    def run_process(self, argv) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLI_PATH), *argv],
                              env=dict(os.environ), capture_output=True,
                              text=True, timeout=30)


# --- one invocation ---------------------------------------------------------


class SequenceTests(McpTestCase):
    def test_one_call_is_initialize_initialized_call_then_delete(self):
        """Drives `mcp_call` → `McpClient.open`, `tools_call` and `close`."""

        mount = self.mount({"list_repos": fakes.tool_result(
            "acme/support", {"repos": [{"repo": "acme/support"}]})})
        text, structured = self.call(mount, surface="claude-code-memory")

        self.assertEqual(mount.rpc_methods(), [
            "initialize", "notifications/initialized", "tools/call", "DELETE"])
        self.assertEqual({r["path"] for r in mount.requests}, {"/mcp"})
        for request in mount.requests:
            self.assertEqual(request["headers"]["authorization"], "Bearer slt_test")
        for request in mount.requests[:3]:
            self.assertEqual(request["headers"]["accept"], ACCEPT)

        first, *rest = mount.requests
        self.assertNotIn("mcp-protocol-version", first["headers"])
        self.assertNotIn("mcp-session-id", first["headers"])
        for request in rest:
            self.assertEqual(request["headers"]["mcp-protocol-version"], "2025-06-18")
            self.assertEqual(request["headers"]["mcp-session-id"], fakes.SESSION_ID)

        params = first["body"]["params"]
        self.assertEqual(params["protocolVersion"], "2025-06-18")
        self.assertEqual(params["clientInfo"],
                         {"name": "claude-code-memory", "version": cli.VERSION})
        self.assertEqual(mount.requests[2]["body"]["params"],
                         {"name": "list_repos", "arguments": {}})
        self.assertEqual((text, structured),
                         ("acme/support", {"repos": [{"repo": "acme/support"}]}))

    def test_the_arguments_are_sent_as_the_caller_built_them(self):
        """Drives `mcp_call`'s arguments into `tools/call` unchanged.

        The caller puts `repo` in its own arguments, and
        `mcp_call`'s `repo` picks the token alone.
        """

        mount = self.mount({"recall": fakes.tool_result("nothing", {"lines": []})})
        arguments = {"repo": "acme/support", "query": "refunds"}
        self.call(mount, tool="recall", arguments=arguments, repo="acme/support")
        self.assertEqual(mount.requests[2]["body"]["params"]["arguments"], arguments)

    def test_list_repos_never_carries_a_repository(self):
        """Drives `mcp_call` with a named repo on the one tool whose input is empty.

        The server answers `unknown_argument` to any key `list_repos` is sent.
        """

        mount = self.mount({"list_repos": fakes.tool_result("", {"repos": []})})
        self.call(mount, tool="list_repos", arguments={}, repo="acme/support")
        self.assertEqual(mount.requests[2]["body"]["params"],
                         {"name": "list_repos", "arguments": {}})

    def test_the_protocol_header_follows_the_negotiated_version(self):
        """Drives `McpClient.open`'s read of `initialize`'s `protocolVersion`.

        The mount answers an older revision than the CLI asked for, so only a
        negotiated header carries it.
        """

        mount = fakes.FakeMount(
            {"list_repos": fakes.tool_result("", {"repos": []})},
            protocol_version="2025-03-26")
        self.addCleanup(mount.close)
        self.call(mount)
        first, *rest = mount.requests
        self.assertEqual(first["body"]["params"]["protocolVersion"], "2025-06-18")
        for request in rest:
            self.assertEqual(request["headers"]["mcp-protocol-version"], "2025-03-26")

    def test_an_sse_response_is_read_by_request_id(self):
        """Drives `read_rpc_response`'s event-stream arm (Review Focus 3).

        A notification and a response to another id come first, so only the
        id match picks the right event.
        """

        decoys = [
            {"jsonrpc": "2.0", "method": "notifications/progress",
             "params": {"progressToken": 1, "progress": 0.5}},
            {"jsonrpc": "2.0", "id": 99,
             "result": {"content": [{"type": "text", "text": "not yours"}]}},
        ]
        mount = self.mount({"list_repos": fakes.tool_result(
            "yours", {"repos": []}, sse=True, before=decoys)})
        text, structured = self.call(mount)
        self.assertEqual((text, structured), ("yours", {"repos": []}))
        self.assertEqual(mount.rpc_methods()[-1], "DELETE")


# --- refusals ---------------------------------------------------------------------


class RefusalTests(McpTestCase):
    def test_every_redirect_is_refused_and_redacted(self):
        """Drives the shared opener's 3xx arm from `/mcp` (Review Focus 3).

        The mount sees one request, the target none, and the printed target
        carries no userinfo.
        """

        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                target = self.mount()
                netloc = urllib.parse.urlsplit(target.url).netloc
                mount = self.mount({"initialize": fakes.http_answer(
                    status, None, {"Location": f"http://bob:hunter2@{netloc}/mcp"})})

                with self.assertRaises(cli.CliError) as caught:
                    self.call(mount)
                message = str(caught.exception)
                self.assertEqual(caught.exception.exit_code, cli.EXIT_USAGE)
                self.assertIn(str(status), message)
                self.assertIn("does not redirect", message)
                self.assertIn(f"http://{netloc}/mcp", message)
                self.assertNotIn("hunter2", message)
                self.assertNotIn("bob", message)
                self.assertNotIn("slt_test", message)
                self.assertEqual(len(mount.requests), 1, "no second request")
                self.assertEqual(target.requests, [], "the redirect target was contacted")

    def test_an_is_error_result_exits_1_with_the_servers_sentence(self):
        """Drives `tools_call`'s `isError` arm into `ApiError`, and `close` after it."""

        envelope = {"error": {"code": "insufficient_level",
                              "message": "You hold read on acme/support; this needs write."}}
        mount = self.mount({"remember": fakes.tool_result(
            "insufficient_level: You hold read on acme/support; this needs write.",
            envelope, is_error=True)})

        with self.assertRaises(cli.ApiError) as caught:
            self.call(mount, tool="remember", repo="acme/support")
        self.assertEqual(caught.exception.exit_code, cli.EXIT_API)
        self.assertEqual(str(caught.exception),
                         "insufficient_level — You hold read on acme/support; this needs write.")
        self.assertEqual(caught.exception.payload, envelope)
        self.assertEqual(mount.rpc_methods()[-1], "DELETE", "the session closes on a refusal")

    def test_a_401_says_run_login(self):
        """Drives the opener's HTTP error arm and `ApiError`'s remedy (Review Focus 2).

        The mount's 401 reaches `mcp_call`, and `whoami`'s reaches `main`'s
        stderr. Both name `invalid_token` and `semanticly login`.
        """

        mount = self.mount({
            "initialize": fakes.http_answer(401, INVALID_TOKEN),
            "GET /v1/whoami": fakes.http_answer(401, INVALID_TOKEN),
        })
        with self.assertRaises(cli.ApiError) as caught:
            self.call(mount)
        self.assertEqual(caught.exception.exit_code, cli.EXIT_API)
        self.assertEqual(caught.exception.payload, INVALID_TOKEN)
        self.assertIn("invalid_token", str(caught.exception))
        self.assertIn("semanticly login", str(caught.exception))
        self.assertEqual(len(mount.requests), 1, "no session opened, so none closes")

        os.environ["SEMANTICLY_API_URL"] = mount.url
        code, _, err = self.run_cli(["whoami"])
        self.assertEqual(code, cli.EXIT_API)
        self.assertIn("invalid_token", err)
        self.assertIn("semanticly login", err)
        self.assertNotIn("slt_test", err)


# --- whoami and the retired checkpoint ------------------------------------------


class WhoamiTests(McpTestCase):
    def test_whoami_runs_over_v1_with_the_unscoped_token(self):
        """Drives `cmd_whoami` → `token_for(cfg, None)` → `api` → `GET /v1/whoami`.

        A `.semanticly` and a repository token are both present, and neither
        is what `whoami` sends. SEMANTICLY_TOKEN wins when set.
        """

        mount = self.mount({"GET /v1/whoami": fakes.http_answer(
            200, {"org": "jordan", "token": "cli", "agent": None, "scopes": ["read"]})})
        os.environ["SEMANTICLY_API_URL"] = mount.url
        os.environ.pop("SEMANTICLY_TOKEN", None)
        (self.project / ".semanticly").write_text("acme/support\n", encoding="utf-8")
        self.install_keychain()
        cli.keychain_set("*", UNSCOPED_SECRET)
        cli.keychain_set("acme/support", REPO_SECRET)

        code, out, err = self.run_cli(["whoami"])
        self.assertEqual(code, 0, err)
        self.assertEqual(mount.requests[-1]["method"], "GET")
        self.assertEqual(mount.requests[-1]["path"], "/v1/whoami")
        self.assertEqual(mount.requests[-1]["headers"]["authorization"],
                         f"Bearer {UNSCOPED_SECRET}")
        self.assertIn("org    jordan", out)

        os.environ["SEMANTICLY_TOKEN"] = "slt_ci"
        code, _, err = self.run_cli(["whoami"])
        self.assertEqual(code, 0, err)
        self.assertEqual(mount.requests[-1]["headers"]["authorization"], "Bearer slt_ci")
        self.assertEqual(len(mount.requests), 2, "whoami opens no MCP session")


class CheckpointGoneTests(McpTestCase):
    def test_the_checkpoint_subcommand_is_gone(self):
        """Drives `build_parser` in the real file, and the module's names."""

        done = self.run_process(["checkpoint", "show", "--session", "s"])
        self.assertEqual(done.returncode, cli.EXIT_USAGE)
        self.assertIn("invalid choice: 'checkpoint'", done.stderr)
        for name in ("cmd_checkpoint", "checkpoint_path", "read_checkpoint",
                     "write_checkpoint", "write_checkpoint_or_fail"):
            self.assertFalse(hasattr(cli, name), name)
