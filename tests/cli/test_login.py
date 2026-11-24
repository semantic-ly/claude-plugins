"""Tests for `semanticly login`.

Every run drives the real file in process. `fakes.FakeMount` answers the
protected resource document, the exchange at `/mcp/cli-token` and the `/v1`
routes. `fakes.FakeAuthKit` answers discovery, registration, the authorize
redirect and a token endpoint that checks PKCE. BROWSER names
`fakes.browser_script`, which follows the authorize redirect to the CLI's
loopback, so no real browser opens. The keychain is `fakes.keychain_shim`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.parse
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import fakes
from test_init import (
    BINDING_PATH,
    BRANCHES_PATH,
    CLIENT_BINDING_PATH,
    CONSOLE,
    GUIDANCE,
    REPO_SECRET,
    UNSCOPED_SECRET,
    InitTestCase,
    branch_rows,
)
from test_semanticly import cli

JWT = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJ1c2VyXzAxIn0.c2lnbmF0dXJlLWJ5dGVz"
CLI_TOKEN_PATH = "/mcp/cli-token"
RESOURCE_DOC_PATH = "/.well-known/oauth-protected-resource/mcp"

_browser_dir: TemporaryDirectory | None = None
_previous_browser: str | None = None


def setUpModule():
    """BROWSER names the fake for the whole module.

    `webbrowser` reads BROWSER once per process, so the path stays fixed and
    each test points the script's log somewhere of its own.
    """

    global _browser_dir, _previous_browser
    _browser_dir = TemporaryDirectory()
    _previous_browser = os.environ.get("BROWSER")
    browser = str(fakes.browser_script(Path(_browser_dir.name)))
    os.environ["BROWSER"] = browser
    webbrowser._tryorder = None
    resolved = getattr(webbrowser.get(), "name", None)
    if resolved != browser:
        raise RuntimeError(f"webbrowser resolves to {resolved!r}, not the fake browser")


def tearDownModule():
    if _previous_browser is None:
        os.environ.pop("BROWSER", None)
    else:
        os.environ["BROWSER"] = _previous_browser
    if _browser_dir is not None:
        _browser_dir.cleanup()


def unscoped_answer() -> dict:
    return fakes.http_answer(201, {
        "token": UNSCOPED_SECRET, "token_id": "tok-0", "repo": None, "scopes": ["read"],
        "level": None, "expires_at": "2027-02-05T12:00:00Z", "console_url": CONSOLE,
        "username": "jordan"})


def repo_answer(level: str = "write") -> dict:
    scopes = ["read"] if level == "read" else ["read", "write"]
    return fakes.http_answer(201, {
        "token": REPO_SECRET, "token_id": "tok-1", "repo": "acme/support", "scopes": scopes,
        "level": level, "expires_at": "2027-02-05T12:00:00Z", "console_url": CONSOLE,
        "username": "jordan"})


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class LoginTestCase(InitTestCase):
    def setUp(self):
        super().setUp()
        self.browser_log = self.home.parent / f"{self.home.name}-browser.jsonl"
        self.addCleanup(self.browser_log.unlink, missing_ok=True)
        os.environ["FAKE_BROWSER_LOG"] = str(self.browser_log)
        self.addCleanup(os.environ.pop, "FAKE_BROWSER_LOG", None)

    def authkit(self, **kwargs) -> fakes.FakeAuthKit:
        authkit = fakes.FakeAuthKit(JWT, **kwargs)
        self.addCleanup(authkit.close)
        return authkit

    def login_mount(self, authkit: fakes.FakeAuthKit, *, mints=None,
                    **overrides) -> fakes.FakeMount:
        """A mount serving the resource document and the exchange, then `init`'s routes."""

        mount = self.mount(**{
            f"POST {CLI_TOKEN_PATH}": mints or [unscoped_answer(), repo_answer()],
            **overrides,
        })
        mount.script[f"GET {RESOURCE_DOC_PATH}"] = fakes.http_answer(200, {
            "resource": mount.url + "/mcp", "authorization_servers": [authkit.url],
            "bearer_methods_supported": ["header"]})
        return mount

    def host(self) -> str:
        return os.environ["SEMANTICLY_API_URL"].removeprefix("http://")

    def browsed(self) -> list[str]:
        try:
            lines = self.browser_log.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line)["url"] for line in lines]

    def config(self) -> dict:
        return json.loads((self.home / "config.json").read_text(encoding="utf-8"))


class LoginTests(LoginTestCase):
    def test_login_stores_the_unscoped_token_and_writes_three_config_keys(self):
        """Drives `cmd_login` → `authkit_sign_in` → `exchange` → `store_login`."""

        authkit = self.authkit()
        mount = self.login_mount(authkit)
        cli.write_config({"prompt_recall": False})

        code, out, err = self.run_cli(["login"])

        self.assertEqual(code, 0, err)
        mints = self.requests_to(mount, "POST", CLI_TOKEN_PATH)
        self.assertEqual(len(mints), 1, "no repository was named, so one mint")
        self.assertEqual(mints[0]["headers"]["authorization"], f"Bearer {JWT}")
        self.assertIsNone(mints[0]["body"], "the unscoped mint carries an empty body")
        self.assertEqual(self.shim.entries(),
                         {("semanticly", f"{self.host()}:*"): UNSCOPED_SECRET})
        self.assertEqual(self.config(), {"api_url": mount.url, "console_url": CONSOLE,
                                         "username": "jordan", "prompt_recall": False})
        self.assertIn("jordan", out)
        self.assertEqual(self.puts(mount), [])

    def test_the_authorize_request_carries_pkce_s256_state_and_resource(self):
        """Drives `register_client`, `pkce_pair`, the authorize URL and `redeem_code`."""

        authkit = self.authkit()
        mount = self.login_mount(authkit)

        code, out, err = self.run_cli(["login"])

        self.assertEqual(code, 0, err)
        resource = mount.url + "/mcp"
        [registration] = authkit.requests_to("/oauth2/register")
        self.assertEqual(registration["json"]["token_endpoint_auth_method"], "none")
        [redirect_uri] = registration["json"]["redirect_uris"]
        self.assertNotIn("authorization", registration["headers"])

        [authorize] = authkit.requests_to("/oauth2/authorize")
        query = authorize["query"]
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], fakes.FAKE_CLIENT_ID)
        self.assertEqual(query["redirect_uri"], redirect_uri)
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["resource"], resource)
        self.assertGreaterEqual(len(query["state"]), 32)

        [token] = authkit.requests_to("/oauth2/token")
        form = token["form"]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], fakes.FAKE_CODE)
        self.assertEqual(form["resource"], resource)
        self.assertEqual(form["redirect_uri"], redirect_uri)
        self.assertEqual(s256(form["code_verifier"]), query["code_challenge"])
        self.assertNotIn("authorization", token["headers"])

        [opened] = self.browsed()
        self.assertTrue(opened.startswith(authkit.url + "/oauth2/authorize?"))
        self.assertIn(opened, out, "the URL is printed as well as opened")

    def test_a_mismatched_state_is_refused_and_stores_nothing(self):
        """Drives `LoopbackReceiver`'s state check through `cmd_login`."""

        authkit = self.authkit(state_override="forged-state")
        mount = self.login_mount(authkit)

        code, _, err = self.run_cli(["login"])

        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("state", err)
        self.assertEqual(authkit.requests_to("/oauth2/token"), [], "no code is redeemed")
        self.assertEqual(self.requests_to(mount, "POST", CLI_TOKEN_PATH), [])
        self.assertEqual(self.shim.entries(), {})
        self.assertFalse((self.home / "config.json").exists())

    def test_the_loopback_binds_127_0_0_1(self):
        """Drives `LoopbackReceiver` directly, then the redirect `login` registers."""

        receiver = cli.LoopbackReceiver()
        try:
            host, port = receiver.server.server_address[:2]
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
            self.assertEqual(receiver.redirect_uri, f"http://127.0.0.1:{port}/callback")
        finally:
            receiver.close()

        authkit = self.authkit()
        self.login_mount(authkit)
        code, _, err = self.run_cli(["login"])
        self.assertEqual(code, 0, err)
        [registration] = authkit.requests_to("/oauth2/register")
        [redirect_uri] = registration["json"]["redirect_uris"]
        self.assertEqual(urllib.parse.urlsplit(redirect_uri).hostname, "127.0.0.1")

    def test_the_fake_browser_is_the_one_webbrowser_opens(self):
        """Pins the module setup, so a stdlib change cannot reach a real browser."""

        self.assertEqual(webbrowser.get().name, os.environ["BROWSER"])

    def test_the_loopback_gives_up_after_its_timeout(self):
        """Drives `LoopbackReceiver.wait` past its deadline with no callback."""

        receiver = cli.LoopbackReceiver(timeout=0.2)
        try:
            with self.assertRaises(cli.CliError) as caught:
                receiver.wait("s")
        finally:
            receiver.close()
        self.assertIn("did not finish", str(caught.exception))

    def test_no_keychain_refuses_before_opening_the_browser(self):
        """Drives `cmd_login`'s first check with no keychain tool on PATH."""

        for tool in fakes.KEYCHAIN_TOOLS:
            (self.tools / tool).unlink()
        authkit = self.authkit()
        mount = self.login_mount(authkit)

        code, _, err = self.run_cli(["login"])

        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn(cli.NO_KEYCHAIN, err)
        self.assertEqual(self.browsed(), [])
        self.assertEqual(authkit.requests, [])
        self.assertEqual(mount.requests, [])

    def test_the_jwt_is_never_written_anywhere(self):
        """Drives a whole login with a repository, then reads every file it could touch."""

        authkit = self.authkit()
        self.login_mount(authkit)

        code, out, err = self.run_cli(["login", "--repo", "acme/support"], stdin="2\n")

        self.assertEqual(code, 0, err)
        self.assertNotIn(JWT, out + err)
        for root in (self.home, self.tools):
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(JWT, path.read_text(encoding="utf-8", errors="replace"),
                                     f"{path} holds the JWT")
        self.assertNotIn(JWT, json.dumps(self.shim.calls()))
        self.assertNotIn(JWT, self.shim.entries().values())


class LoginWithRepoTests(LoginTestCase):
    def test_login_with_a_repo_mints_lists_and_binds_the_choice(self):
        """Drives `login_repo` → `mint_repo_token` → `choose_branch` → `bind_cli`."""

        authkit = self.authkit()
        mount = self.login_mount(authkit)

        code, out, err = self.run_cli(["login", "--repo", "acme/support"], stdin="2\n")

        self.assertEqual(code, 0, err)
        mints = self.requests_to(mount, "POST", CLI_TOKEN_PATH)
        self.assertEqual([m["body"] for m in mints], [None, {"repo": "acme/support"}])
        self.assertEqual(mints[1]["headers"]["authorization"], f"Bearer {UNSCOPED_SECRET}")
        self.assertEqual(self.shim.entries()[("semanticly", f"{self.host()}:acme/support")],
                         REPO_SECRET)
        [listing] = self.requests_to(mount, "GET", BRANCHES_PATH)
        self.assertEqual(listing["headers"]["authorization"], f"Bearer {REPO_SECRET}")
        self.assertIn(GUIDANCE, out)
        self.assertIn(f"{CONSOLE}/acme/support/branches", out)
        [binding] = self.requests_to(mount, "PUT", BINDING_PATH)
        self.assertEqual(binding["body"], {"branch": "agents/jordan/claude-code"})
        self.assertEqual(binding["headers"]["authorization"], f"Bearer {REPO_SECRET}")
        self.assertEqual(self.requests_to(mount, "PUT", CLIENT_BINDING_PATH), [],
                         "login binds the CLI's connection alone")

    def test_a_semanticly_file_names_the_repository(self):
        """Drives `named_repo` inside `cmd_login` from the project's marker."""

        authkit = self.authkit()
        mount = self.login_mount(authkit)
        self.marker().write_text("acme/support\n", encoding="utf-8")

        code, _, err = self.run_cli(["login"], stdin="1\n")

        self.assertEqual(code, 0, err)
        [binding] = self.requests_to(mount, "PUT", BINDING_PATH)
        self.assertEqual(binding["body"], {"branch": branch_rows(datetime.now(UTC))[0]["name"]})

    def test_a_reader_is_told_and_nothing_is_bound(self):
        """Drives `login_repo`'s arm for `403 insufficient_level` on the listing."""

        authkit = self.authkit()
        mount = self.login_mount(
            authkit, mints=[unscoped_answer(), repo_answer("read")],
            **{f"GET {BRANCHES_PATH}": fakes.http_answer(403, {"error": {
                "code": "insufficient_level", "message": "This needs write on acme/support."}})})

        code, out, err = self.run_cli(["login", "--repo", "acme/support"], stdin="1\n")

        self.assertEqual(code, 0, err)
        self.assertIn("`acme/support` is read only for you here", out)
        self.assertEqual(self.puts(mount), [])
        self.assertNotIn(GUIDANCE, out)

    def test_an_empty_choice_binds_nothing_and_prints_the_branches_url(self):
        """Drives `choose_branch`'s skip through `prompt_choice`."""

        authkit = self.authkit()
        mount = self.login_mount(authkit)

        code, out, err = self.run_cli(["login", "--repo", "acme/support"], stdin="\n")

        self.assertEqual(code, 0, err)
        self.assertEqual(self.puts(mount), [])
        last = out.strip().splitlines()[-1]
        self.assertIn("Nothing was bound", last)
        self.assertIn(f"{CONSOLE}/acme/support/branches", last)

    def test_a_refused_bind_relays_the_servers_sentence(self):
        """Drives `bind_cli`'s PUT into a refusal."""

        sentence = "Branch names under `agents/system/` are reserved."
        authkit = self.authkit()
        self.login_mount(authkit, **{f"PUT {BINDING_PATH}": fakes.http_answer(
            422, {"error": {"code": "branch_reserved", "message": sentence}})})

        code, _, err = self.run_cli(["login", "--repo", "acme/support"], stdin="1\n")

        self.assertEqual(code, cli.EXIT_API)
        self.assertIn(sentence, err)
