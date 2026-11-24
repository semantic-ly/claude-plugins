"""Tests for the CLI at `plugins/semanticly/bin/semanticly`.

Stdlib `unittest`, run from the repo root:

    python3 -m unittest discover -s tests/cli -t tests/cli

They live here rather than under a console app's `tests/` because the CLI is not
part of the Django app: it needs no settings module, no database and no
`pytest`, and putting it in the Django suite would make the plugin's runtime
depend on a service it never talks to.

Every test drives the real request-and-render path. The API is a stdlib HTTP
server that answers what `/v1` answers, so a test's setup supplies the wire
bytes and never the parsed result the code under test is supposed to produce.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from tempfile import TemporaryDirectory

import fakes

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "plugins" / "semanticly" / "bin" / "semanticly"


def load_cli():
    """Import the extensionless executable as a module."""

    loader = SourceFileLoader("semanticly_cli", str(CLI_PATH))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_cli()


# --- a stand-in for `/v1` -----------------------------------------------------


class FakeAPI:
    """A real HTTP server that answers whatever `route` returns.

    `route(method, path, query, body) -> (status, dict)`. Requests are recorded
    so a test can assert on the bytes the CLI actually sent.
    """

    def __init__(self, route):
        self.route = route
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self, method):
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                query = dict(urllib.parse.parse_qsl(parsed.query))
                outer.requests.append({
                    "method": method,
                    "path": parsed.path,
                    "query": query,
                    "body": body,
                    "auth": self.headers.get("Authorization"),
                })
                result = outer.route(method, parsed.path, query, body)
                status, payload = result[0], result[1]
                extra = result[2] if len(result) > 2 else {}
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                for name, value in extra.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CliTestCase(unittest.TestCase):
    """Runs `main` with a scratch config home and no inherited environment."""

    def setUp(self):
        self._home = TemporaryDirectory()
        self.home = Path(self._home.name)
        self.addCleanup(self._home.cleanup)
        for name in ("SEMANTICLY_HOME", "SEMANTICLY_API_URL", "SEMANTICLY_TOKEN",
                     "SEMANTICLY_REPO", "SEMANTICLY_ALLOW_HTTP",
                     "CLAUDE_PLUGIN_OPTION_API_URL",
                     "CLAUDE_PROJECT_DIR"):
            self._unset(name)
        os.environ["SEMANTICLY_HOME"] = str(self.home)

        # An empty project, so no `.semanticly` above the checkout names a repo.
        self.project = self.home / "project"
        self.project.mkdir()
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)

        # `PATH` names an empty directory, so no real keychain tool is reached.
        self._tools = TemporaryDirectory()
        self.addCleanup(self._tools.cleanup)
        self.tools = Path(self._tools.name)
        self._unset("PATH")
        os.environ["PATH"] = str(self.tools)

    def install_keychain(self, tools=fakes.KEYCHAIN_TOOLS) -> fakes.KeychainShim:
        """Fake keychain tools on `PATH`, outside `SEMANTICLY_HOME`."""

        return fakes.keychain_shim(self.tools, tools)

    def _unset(self, name):
        previous = os.environ.pop(name, None)

        def restore():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

        self.addCleanup(restore)

    def listener(self, route) -> FakeAPI:
        """A server the CLI is NOT pointed at, for redirect targets."""

        api = FakeAPI(route)
        self.addCleanup(api.close)
        return api

    def serve(self, route) -> FakeAPI:
        api = self.listener(route)
        os.environ["SEMANTICLY_API_URL"] = api.url
        os.environ.setdefault("SEMANTICLY_TOKEN", "slt_test")
        return api

    def run_cli(self, argv, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            sys.stdin = real_stdin
        return code, out.getvalue(), err.getvalue()


class SourceTests(unittest.TestCase):
    def test_the_cli_is_executable(self):
        self.assertTrue(os.access(CLI_PATH, os.X_OK), f"{CLI_PATH} is not executable")

    def test_the_cli_imports_nothing_outside_the_standard_library(self):
        """Pins "stdlib only" against the source, not against an install log."""

        tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        outside = sorted(roots - set(sys.stdlib_module_names) - {"__future__"})
        self.assertEqual(outside, [], f"third-party imports: {outside}")

    def test_the_help_text_makes_no_promise_the_route_does_not_earn(self):
        """Pins the carried ruling against every help string argparse renders."""

        text = CLI_PATH.read_text(encoding="utf-8")
        docs = " ".join(
            re.findall(r'help=\s*\(?\s*["\'](.*?)["\']\s*\)?[,)]', text, re.S)
            + re.findall(r'description=\s*\(?\s*(.*?)\s*\)?,\n', text, re.S)
        ).lower()
        for overclaim in ("immutable", "frozen", "reproducible forever", "never change"):
            self.assertNotIn(overclaim, docs)


if __name__ == "__main__":
    unittest.main()
