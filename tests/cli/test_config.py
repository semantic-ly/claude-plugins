"""Tests for the CLI's configuration and its repository.

`load_config` and `resolve_host` decide which host a call reaches,
`write_config` keeps `~/.semanticly/config.json`, and `resolve_repo` reads the
destination from `--repo` or the nearest `.semanticly` file. Where a test can
observe the answer on the wire, it runs the CLI file as a subprocess against a
stand-in API.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import fakes
from test_semanticly import CLI_PATH, CliTestCase, cli

SUBPROCESS_TIMEOUT = 30


def whoami_route(name):
    return lambda *a: (200, {"org": name, "token": "t", "scopes": []})


class CliProcessTestCase(CliTestCase):
    """Adds a run of the real CLI file in a child process."""

    def run_process(self, argv, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), *argv],
            cwd=str(cwd or self.home), env=dict(os.environ),
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )

    def write_config_file(self, body) -> None:
        (self.home / "config.json").write_text(json.dumps(body), encoding="utf-8")


# --- configuration and the no-default-repo ruling ----------------------------


class ConfigTests(CliTestCase):
    def test_an_unnamed_repo_is_refused_rather_than_guessed(self):
        """Drives `resolve_repo` through `cmd_recall`."""

        api = self.serve(lambda *a: (200, {}))
        code, _, err = self.run_cli(["recall", "anything"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--repo", err)
        self.assertIn("no default repo", err)
        self.assertEqual(api.requests, [], "a refused destination must not hit the API")

    def test_a_missing_token_is_refused_before_any_request(self):
        """Drives `token_for` with no `SEMANTICLY_TOKEN` and an empty keychain."""

        api = self.serve(lambda *a: (200, {}))
        os.environ.pop("SEMANTICLY_TOKEN")
        self.install_keychain()
        code, _, err = self.run_cli(["whoami"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("Run `semanticly login`", err)
        self.assertEqual(api.requests, [])

    def test_the_config_file_supplies_what_the_environment_does_not(self):
        """Drives `read_config_file` → `load_config` → `token_for` → `api` → `cmd_whoami`.

        The file supplies the host and the keychain supplies the token. A
        `token` key in the file is ignored, because tokens live in the
        keychain and nowhere else.
        """

        api = self.serve(lambda *a: (200, {
            "org": "acme", "token": "ci", "agent": None, "scopes": ["read", "write"],
        }))
        base = os.environ.pop("SEMANTICLY_API_URL")
        os.environ.pop("SEMANTICLY_TOKEN")
        (self.home / "config.json").write_text(
            json.dumps({"api_url": base, "token": "slt_from_file"}), encoding="utf-8")
        self.install_keychain()
        cli.keychain_set("*", "slt_from_keychain")

        code, out, err = self.run_cli(["whoami"])
        self.assertEqual(code, 0, err)
        self.assertIn("acme", out)
        self.assertIn("read write", out)
        self.assertEqual(api.requests[0]["auth"], "Bearer slt_from_keychain")

    def test_an_error_envelope_is_reported_as_the_server_worded_it(self):
        """Drives `api`'s HTTPError arm and `ApiError`."""

        self.serve(lambda *a: (403, {"error": {
            "code": "insufficient_level",
            "message": "You have read on support; this needs write.",
        }}))
        code, _, err = self.run_cli(["remember", "--repo", "acme/support", "a fact"])
        self.assertEqual(code, cli.EXIT_API)
        self.assertIn("insufficient_level", err)
        self.assertIn("this needs write", err)

    def test_every_redirect_status_is_refused_and_the_target_sees_nothing(self):
        """Drives `api`'s opener — `NoRedirects` and the 3xx arm.

        The stdlib default opener would follow the redirect and forward
        `Authorization` to the second listener. The assertion that matters is
        that the second listener saw NO request at all.

        All five statuses, because `redirect_request` is the single hook every
        one of urllib's redirect handlers routes through — so returning None
        refuses all five today. Pinning one and claiming five is how a sixth
        status with its own path would get in unnoticed.
        """

        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                target = self.listener(lambda *a: (200, {"stolen": True}))
                self.serve(lambda *a, _s=status, _t=target: (
                    _s, {}, {"Location": _t.url + "/whoami"}))

                code, _, err = self.run_cli(["whoami"])
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn(str(status), err)
                self.assertIn("does not redirect", err)
                self.assertEqual(target.requests, [], "the redirect target was contacted")
                self.assertNotIn("slt_test", err, "the refusal must not print the token")

    def test_a_remote_cleartext_base_url_is_refused_before_any_request(self):
        """Drives `check_cleartext` through `load_config`."""

        os.environ["SEMANTICLY_API_URL"] = "http://staging.example.invalid:8080/v1"
        os.environ["SEMANTICLY_TOKEN"] = "slt_secret"
        code, out, err = self.run_cli(["whoami"])
        self.assertEqual((code, out), (cli.EXIT_USAGE, ""))
        self.assertIn("staging.example.invalid:8080", err)
        self.assertIn("SEMANTICLY_ALLOW_HTTP=1", err)
        self.assertNotIn("Could not reach", err, "the refusal comes before the connection")
        self.assertNotIn("slt_secret", err, "the refusal must not carry the token")

    def test_the_opt_in_lets_a_remote_cleartext_base_url_through_with_a_warning(self):
        """The host is unroutable, so the run fails at the connection, after the warning."""

        os.environ["SEMANTICLY_API_URL"] = "http://staging.example.invalid:8080/v1"
        os.environ["SEMANTICLY_TOKEN"] = "slt_secret"
        os.environ["SEMANTICLY_ALLOW_HTTP"] = "1"
        self.addCleanup(os.environ.pop, "SEMANTICLY_ALLOW_HTTP", None)
        _, _, err = self.run_cli(["whoami"])
        self.assertIn("warning", err)
        self.assertIn("staging.example.invalid:8080", err)
        self.assertIn("Could not reach", err)
        self.assertNotIn("slt_secret", err, "the warning must not carry the token")

    def test_a_cleartext_url_with_userinfo_does_not_echo_it(self):
        """Drives `check_cleartext`'s rebuild from the parsed parts, on both arms."""

        os.environ["SEMANTICLY_API_URL"] = "http://bob:hunter2@staging.example.invalid/v1"
        os.environ["SEMANTICLY_TOKEN"] = "slt_secret"
        self.addCleanup(os.environ.pop, "SEMANTICLY_ALLOW_HTTP", None)
        for allow in ("", "1"):
            with self.subTest(allow=allow):
                os.environ["SEMANTICLY_ALLOW_HTTP"] = allow
                _, _, err = self.run_cli(["whoami"])
                self.assertIn("staging.example.invalid", err)
                self.assertNotIn("hunter2", err)

    def test_a_loopback_base_url_is_silent(self):
        """The local stack is the ordinary case and must not nag."""

        self.serve(lambda *a: (200, {"org": "acme", "token": "t", "scopes": []}))
        code, _, err = self.run_cli(["whoami"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")

    def test_an_https_base_url_is_silent(self):
        """Drives `check_cleartext`'s scheme arm."""

        os.environ["SEMANTICLY_API_URL"] = "https://api.example.invalid/v1"
        os.environ["SEMANTICLY_TOKEN"] = "slt_secret"
        _, _, err = self.run_cli(["whoami"])
        self.assertNotIn("cleartext", err)


# --- the host ------------------------------------------------------------------


class HostTests(CliProcessTestCase):
    def test_the_host_order_is_env_then_plugin_option_then_file_then_the_public_origin(self):
        """Drives `resolve_host` through `load_config` → `api` → `cmd_whoami`."""

        from_env = self.listener(whoami_route("env"))
        from_option = self.listener(whoami_route("option"))
        from_file = self.listener(whoami_route("file"))
        os.environ["SEMANTICLY_TOKEN"] = "slt_test"
        os.environ["SEMANTICLY_API_URL"] = from_env.url
        os.environ["CLAUDE_PLUGIN_OPTION_API_URL"] = from_option.url
        self.write_config_file({"api_url": from_file.url})

        for expected, unset in (("env", "SEMANTICLY_API_URL"),
                                ("option", "CLAUDE_PLUGIN_OPTION_API_URL"),
                                ("file", None)):
            with self.subTest(expected=expected):
                run = self.run_process(["whoami", "--json"])
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)["org"], expected)
            if unset:
                os.environ.pop(unset)

        self.assertEqual(len(from_env.requests), 1)
        self.assertEqual(len(from_option.requests), 1)
        self.assertEqual(len(from_file.requests), 1)

        self.write_config_file({})
        self.assertEqual(cli.load_config().api_url, cli.PUBLIC_API_ORIGIN)

    def test_a_trailing_v1_is_stripped(self):
        """Drives `resolve_host`'s strip, then `api`'s `/v1` on the wire."""

        os.environ["SEMANTICLY_API_URL"] = "http://h.example:1/v1/"
        self.assertEqual(cli.resolve_host({}), "http://h.example:1")
        os.environ["SEMANTICLY_API_URL"] = "http://h.example:1/v1/v1"
        self.assertEqual(cli.resolve_host({}), "http://h.example:1/v1",
                         "one trailing /v1 is stripped, not every one")
        os.environ["SEMANTICLY_API_URL"] = "http://h.example:1"
        self.assertEqual(cli.resolve_host({}), "http://h.example:1")

        api = self.serve(whoami_route("acme"))
        for configured in (api.url, api.url.removesuffix("/v1")):
            with self.subTest(configured=configured):
                os.environ["SEMANTICLY_API_URL"] = configured
                run = self.run_process(["whoami"])
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(api.requests[-1]["path"], "/v1/whoami")

    def test_a_source_that_strips_to_empty_falls_through(self):
        """Drives `resolve_host`'s fall-through over a source that is only `/v1`."""

        for empty in ("/v1", "/", "/v1/", "  /v1  "):
            with self.subTest(empty=empty):
                os.environ["CLAUDE_PLUGIN_OPTION_API_URL"] = "http://option.example"
                os.environ["SEMANTICLY_API_URL"] = empty
                self.assertEqual(cli.resolve_host({}), "http://option.example")
                os.environ["CLAUDE_PLUGIN_OPTION_API_URL"] = empty
                self.assertEqual(cli.resolve_host({"api_url": "http://file.example/v1"}),
                                 "http://file.example")
                self.assertEqual(cli.resolve_host({"api_url": empty}), cli.PUBLIC_API_ORIGIN)

    def test_no_configured_host_reads_the_public_origin(self):
        """Drives `resolve_host`'s last arm through `load_config`."""

        self.assertEqual(cli.PUBLIC_API_ORIGIN, "https://api.semantic.ly")
        os.environ["SEMANTICLY_TOKEN"] = "slt_test"
        self.assertEqual(cli.load_config().api_url, cli.PUBLIC_API_ORIGIN)

    def test_a_non_object_config_is_an_error_and_a_missing_one_is_not(self):
        """Drives `read_config_file`'s two arms and the `api_url` type check."""

        os.environ["SEMANTICLY_TOKEN"] = "slt_test"
        self.assertEqual(cli.load_config().api_url, cli.PUBLIC_API_ORIGIN)

        self.write_config_file(["api_url", "http://h.example"])
        with self.assertRaises(cli.CliError) as caught:
            cli.load_config()
        self.assertIn("config.json", str(caught.exception))
        run = self.run_process(["whoami"])
        self.assertEqual(run.returncode, cli.EXIT_USAGE)
        self.assertIn("JSON object", run.stderr)

        self.write_config_file({"api_url": 8080})
        with self.assertRaises(cli.CliError) as caught:
            cli.load_config()
        self.assertIn("api_url", str(caught.exception))


# --- the other keys --------------------------------------------------------------


class ConfigKeyTests(CliProcessTestCase):
    def test_prompt_recall_is_off_only_for_literal_false(self):
        """Drives `load_config`'s `prompt_recall`."""

        os.environ["SEMANTICLY_TOKEN"] = "slt_test"
        self.write_config_file({"prompt_recall": False})
        self.assertIs(cli.load_config().prompt_recall, False)
        for value in ("false", 0, None):
            with self.subTest(value=value):
                self.write_config_file({"prompt_recall": value})
                self.assertIs(cli.load_config().prompt_recall, True)
        self.write_config_file({})
        self.assertIs(cli.load_config().prompt_recall, True)

    def test_prompt_recall_setting_says_why_a_malformed_setting_stays_on(self):
        """Drives `prompt_recall_setting`, the per-prompt hook's reader."""

        self.assertEqual(cli.prompt_recall_setting(), (True, ""))
        for value, expected in ((False, (False, "")), (True, (True, ""))):
            with self.subTest(value=value):
                self.write_config_file({"prompt_recall": value})
                self.assertEqual(cli.prompt_recall_setting(), expected)
        for value in ("false", 0, None):
            with self.subTest(value=value):
                self.write_config_file({"prompt_recall": value})
                on, why = cli.prompt_recall_setting()
                self.assertIs(on, True)
                self.assertIn("`prompt_recall`", why)
                self.assertIn("stays on", why)
        (self.home / "config.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(cli.prompt_recall_setting(), (True, ""),
                         "the CLI's own refusal is the one line a malformed file gets")

    def test_write_config_keeps_other_keys(self):
        """Drives `write_config` → `read_config_file` → `load_config`."""

        os.environ["SEMANTICLY_TOKEN"] = "slt_test"
        self.write_config_file({"api_url": "https://h.example", "prompt_recall": False,
                                "someone_elses": [1, 2]})
        cli.write_config({"console_url": "https://console.example/", "username": "ada"})

        stored = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, {
            "api_url": "https://h.example", "prompt_recall": False,
            "someone_elses": [1, 2], "console_url": "https://console.example/",
            "username": "ada",
        })
        self.assertEqual(sorted(p.name for p in self.home.iterdir()),
                         ["config.json", "project"], "no temp file is left behind")

        cfg = cli.load_config()
        self.assertEqual(cfg.console_url, "https://console.example")
        self.assertEqual(cfg.username, "ada")

    def test_write_config_creates_the_directory(self):
        """Drives `write_config` with no config home on disk."""

        os.environ["SEMANTICLY_HOME"] = str(self.home / "fresh")
        cli.write_config({"api_url": "https://h.example"})
        stored = json.loads((self.home / "fresh" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, {"api_url": "https://h.example"})


# --- the repository --------------------------------------------------------------


class RepoTests(CliProcessTestCase):
    def serve_recall(self) -> fakes.FakeMount:
        """A mount the CLI is pointed at, answering `recall`."""

        mount = fakes.FakeMount({"recall": fakes.tool_result("", {"lines": []})})
        self.addCleanup(mount.close)
        os.environ["SEMANTICLY_API_URL"] = mount.url
        os.environ.setdefault("SEMANTICLY_TOKEN", "slt_test")
        return mount

    def recalled_repo(self, run: subprocess.CompletedProcess, mount) -> str:
        """The `repo` argument of the last `recall` the mount was sent."""

        self.assertEqual(run.returncode, 0, run.stderr)
        calls = [r["body"]["params"] for r in mount.requests
                 if (r["body"] or {}).get("method") == "tools/call"]
        return calls[-1]["arguments"]["repo"]

    def test_repo_comes_from_the_flag_then_the_nearest_semanticly(self):
        """Drives `project_root` → `find_semanticly` → `resolve_repo` → `cmd_recall`."""

        api = self.serve_recall()
        deep = self.project / "a" / "b"
        deep.mkdir(parents=True)
        (self.project / ".semanticly").write_text("acme/far\n", encoding="utf-8")
        os.environ["CLAUDE_PROJECT_DIR"] = str(deep)

        run = self.run_process(["recall", "q"])
        self.assertEqual(self.recalled_repo(run, api), "acme/far",
                         "a file two levels up is found")

        (self.project / "a" / ".semanticly").write_text("acme/near\n", encoding="utf-8")
        run = self.run_process(["recall", "q"])
        self.assertEqual(self.recalled_repo(run, api), "acme/near",
                         "the nearer file wins")

        run = self.run_process(["recall", "--repo", "acme/flag", "q"])
        self.assertEqual(self.recalled_repo(run, api), "acme/flag",
                         "the flag wins over every file")

        (deep / ".semanticly").mkdir()
        run = self.run_process(["recall", "q"])
        self.assertEqual(self.recalled_repo(run, api), "acme/near",
                         "a directory named .semanticly, like ~/.semanticly, is walked past")

        os.environ.pop("CLAUDE_PROJECT_DIR")
        run = self.run_process(["recall", "q"], cwd=self.project)
        self.assertEqual(self.recalled_repo(run, api), "acme/far",
                         "with no CLAUDE_PROJECT_DIR the walk starts at the working directory")

    def test_no_repo_is_refused_and_semanticly_repo_is_ignored(self):
        """Drives `resolve_repo`'s refusal through `cmd_recall`."""

        api = self.serve(lambda *a: (200, {"results": []}))
        os.environ["SEMANTICLY_REPO"] = "acme/support"
        run = self.run_process(["recall", "q"])
        self.assertEqual(run.returncode, cli.EXIT_USAGE)
        self.assertIn("--repo", run.stderr)
        self.assertIn(".semanticly", run.stderr)
        self.assertEqual(api.requests, [], "a refused destination must not hit the API")
        self.assertNotIn("SEMANTICLY_REPO", CLI_PATH.read_text(encoding="utf-8"))

    def test_a_semanticly_file_holding_a_token_shaped_line_is_refused(self):
        """Drives `read_semanticly`'s refusal through `cmd_recall`."""

        api = self.serve(lambda *a: (200, {"results": []}))
        marker = self.project / ".semanticly"
        secret = "slt_0123456789abcdef0123456789abcdef"
        for content in (f"{secret}\n", "acme/support\nslt_more\n", "acme/support\n\n",
                        "acme/support\r", " acme/support\n", "Acme/Support\n", ""):
            with self.subTest(content=content):
                marker.write_text(content, encoding="utf-8")
                run = self.run_process(["recall", "q"])
                self.assertEqual(run.returncode, cli.EXIT_USAGE)
                self.assertIn(str(marker), run.stderr)
                self.assertNotIn("slt_", run.stderr, "the refusal must not echo the file")
                self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(api.requests, [])

    def test_a_semanticly_file_may_end_without_a_newline_or_with_crlf(self):
        """Drives `read_semanticly`'s accepted endings through `cmd_recall`."""

        api = self.serve_recall()
        marker = self.project / ".semanticly"
        for content in ("acme/support\n", "acme/support\r\n", "acme/support"):
            with self.subTest(content=content):
                marker.write_bytes(content.encode("utf-8"))
                run = self.run_process(["recall", "q"])
                self.assertEqual(self.recalled_repo(run, api), "acme/support")
