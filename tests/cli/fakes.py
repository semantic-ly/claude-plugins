"""Stand-ins for what the CLI reaches outside its own process.

Each fake is a real program or server the CLI talks to over its real channel,
so a test drives the CLI's own code path and never a patched function.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KEYCHAIN_TOOLS = ("security", "secret-tool")

# One script serves both tools. It dispatches on the name it was run as,
# records its argv and stdin, and keeps entries in a JSON file beside itself.
_KEYCHAIN_SCRIPT = r'''
import json
import os
import shlex
import sys
from pathlib import Path

STORE = Path(__file__).resolve().parent / "keychain.json"


def load():
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": {}, "calls": []}


def save(state):
    STORE.write_text(json.dumps(state), encoding="utf-8")


def flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def attribute(args, name):
    for i in range(len(args) - 1):
        if args[i] == name:
            return args[i + 1]
    return None


def security(state, args, stdin):
    if args[:1] == ["-i"]:
        for line in stdin.splitlines():
            words = shlex.split(line)
            if words[:1] == ["add-generic-password"]:
                key = flag(words, "-s") + "\n" + flag(words, "-a")
                state["entries"][key] = flag(words, "-w")
        return 0
    if args[:1] == ["find-generic-password"]:
        key = flag(args, "-s") + "\n" + flag(args, "-a")
        if key not in state["entries"]:
            return 44
        print(state["entries"][key])
        return 0
    return 2


def secret_tool(state, args, stdin):
    key = (attribute(args, "service") or "") + "\n" + (attribute(args, "account") or "")
    if args[:1] == ["store"]:
        state["entries"][key] = stdin
        return 0
    if args[:1] == ["lookup"]:
        if key not in state["entries"]:
            return 1
        sys.stdout.write(state["entries"][key])
        return 0
    return 2


def main():
    tool = Path(sys.argv[0]).name
    args = sys.argv[1:]
    stdin = sys.stdin.read()
    state = load()
    state["calls"].append({"tool": tool, "argv": args, "stdin": stdin})
    code = (security if tool == "security" else secret_tool)(state, args, stdin)
    save(state)
    sys.exit(code)


main()
'''


class KeychainShim:
    """A directory holding fake keychain tools, and what they recorded."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin = bin_dir
        self.store = bin_dir / "keychain.json"

    def _state(self) -> dict:
        try:
            return json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"entries": {}, "calls": []}

    def calls(self) -> list[dict]:
        """Every run, in order, as `{tool, argv, stdin}`."""

        return self._state()["calls"]

    def entries(self) -> dict[tuple[str, str], str]:
        """Stored secrets keyed by `(service, account)`."""

        return {tuple(key.split("\n", 1)): value
                for key, value in self._state()["entries"].items()}


def keychain_shim(directory: Path, tools: tuple[str, ...] = KEYCHAIN_TOOLS) -> KeychainShim:
    """Write fake `security` and `secret-tool` into `directory`.

    Put `directory` alone on `PATH`, so the real tools stay out of reach. Each
    script runs under this interpreter by absolute path, so it needs nothing
    else on `PATH`.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    source = f"#!{sys.executable}\n{_KEYCHAIN_SCRIPT}"
    for tool in tools:
        path = directory / tool
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return KeychainShim(directory)


# --- a stand-in for the MCP mount ---------------------------------------------

SESSION_ID = "fake-session-1"
PROTOCOL_VERSION = "2025-06-18"


def tool_result(text: str, structured: dict | None = None, *, is_error: bool = False,
                sse: bool = False, before: list | None = None) -> dict:
    """A script entry answering one `tools/call` with a `CallToolResult`.

    With `sse`, the answer is a `text/event-stream`, and each message in
    `before` goes out as its own event ahead of the response.
    """

    result: dict = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return {"result": result, "sse": sse, "before": before or []}


def http_answer(status: int, body: object = None, headers: dict | None = None) -> dict:
    """A script entry answering at the HTTP level, ahead of any JSON-RPC."""

    return {"status": status, "body": body, "headers": headers or {}}


class FakeMount:
    """A stdlib Streamable HTTP server that answers `/mcp` from a script.

    `initialize` answers with an `Mcp-Session-Id`, a notification with 202 and
    `DELETE /mcp` with 204. A `tools/call` answers from `script[tool]`. Any
    script key that names a JSON-RPC method, `"DELETE /mcp"`, or
    `"{METHOD} {path}"` for any other path overrides the default with an
    `http_answer`. A list of entries answers in order, and its last
    entry answers every call after it. Every request is recorded with its headers, their
    names lowercased, and its parsed body. `initialize` answers
    `protocol_version` whatever the client asked for.
    """

    def __init__(self, script: dict | None = None,
                 protocol_version: str = PROTOCOL_VERSION) -> None:
        self.script = dict(script or {})
        self.protocol_version = protocol_version
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._serve(self, "GET")

            def do_POST(self):
                outer._serve(self, "POST")

            def do_DELETE(self):
                outer._serve(self, "DELETE")

            def do_PUT(self):
                outer._serve(self, "PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """The origin, the value `api_url` holds."""

        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def rpc_methods(self) -> list[str]:
        """Each recorded request as its JSON-RPC method, or `DELETE` for the close."""

        return [r["method"] if r["method"] != "POST" else (r["body"] or {}).get("method", "")
                for r in self.requests]

    def _serve(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        self.requests.append({
            "method": method,
            "path": path,
            "query": dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query)),
            "headers": {k.lower(): v for k, v in handler.headers.items()},
            "body": body,
        })
        if path == "/mcp" and method == "POST":
            self._answer_rpc(handler, body if isinstance(body, dict) else {})
        elif path == "/mcp" and method == "DELETE":
            self._send_entry(handler, self.script.get("DELETE /mcp", http_answer(204)))
        else:
            entry = self._next_entry(f"{method} {path}") or http_answer(
                404, {"error": {"code": "not_found", "message": "No such route."}})
            self._send_entry(handler, entry)

    def _answer_rpc(self, handler: BaseHTTPRequestHandler, body: dict) -> None:
        rpc = body.get("method", "")
        key = (body.get("params") or {}).get("name", "") if rpc == "tools/call" else rpc
        entry = self._next_entry(key) if "id" in body else self.script.get(key)
        if entry is not None and "status" in entry:
            self._send_entry(handler, entry)
        elif rpc == "initialize":
            result = {"protocolVersion": self.protocol_version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-mount", "version": "0"}}
            self._send(handler, 200, "application/json",
                       json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}),
                       {"Mcp-Session-Id": SESSION_ID})
        elif "id" not in body:
            self._send(handler, 202, "application/json", "")
        elif entry is None:
            error = {"code": -32601, "message": f"No script entry for {key!r}."}
            self._send(handler, 200, "application/json",
                       json.dumps({"jsonrpc": "2.0", "id": body["id"], "error": error}))
        else:
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": entry["result"]}
            if entry.get("sse"):
                events = [*entry.get("before", []), reply]
                stream = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events)
                self._send(handler, 200, "text/event-stream", stream, close=True)
            else:
                self._send(handler, 200, "application/json", json.dumps(reply))

    def _next_entry(self, key: str) -> dict | None:
        """The entry answering `key` now, taken from the front of a list."""

        entry = self.script.get(key)
        if not isinstance(entry, list):
            return entry
        return entry.pop(0) if len(entry) > 1 else entry[0]

    def _send_entry(self, handler: BaseHTTPRequestHandler, entry: dict) -> None:
        body = entry.get("body")
        text = "" if body is None else json.dumps(body)
        self._send(handler, entry["status"], "application/json", text, entry.get("headers"))

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, content_type: str, text: str,
              headers: dict | None = None, *, close: bool = False) -> None:
        """One answer. `close` ends the body at the connection's close, as a stream does."""

        encoded = text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        if close:
            handler.send_header("Connection", "close")
            handler.close_connection = True
        else:
            handler.send_header("Content-Length", str(len(encoded)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(encoded)


# --- a stand-in for AuthKit and the person's browser ---------------------------

# The browser script. It records the URL it was handed, then forks so its
# caller returns at once, as a real browser does, and the child follows the
# authorize redirect to the loopback. The parent always exits 0, so
# `webbrowser` never falls through to a real browser.
_BROWSER_SCRIPT = r'''
import json
import os
import sys
import urllib.request

url = sys.argv[1]
log = os.environ.get("FAKE_BROWSER_LOG", "")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"url": url}) + "\n")
if os.fork():
    sys.exit(0)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    opener.open(url, timeout=30).read()
except Exception:
    pass
os._exit(0)
'''


def browser_script(directory: Path) -> Path:
    """Write the fake browser into `directory` and return its path for BROWSER."""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "fake-browser"
    path.write_text(f"#!{sys.executable}\n{_BROWSER_SCRIPT}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


FAKE_CLIENT_ID = "client_fake_cli"
FAKE_CODE = "code_fake_0123"


class FakeAuthKit:
    """A stdlib authorization server with RFC 8414 metadata, RFC 7591
    registration, an authorize endpoint and a PKCE-checking token endpoint.

    `authorize` answers 302 to the request's `redirect_uri` with a code and
    the request's `state`, or `state_override` when set. `token` answers the
    access token only when the code matches and the verifier hashes to the
    challenge the authorize request carried. Every request is recorded as
    `{method, path, query, form, json}`.
    """

    def __init__(self, access_token: str, *, state_override: str | None = None) -> None:
        self.access_token = access_token
        self.state_override = state_override
        self.requests: list[dict] = []
        self.challenge = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._serve(self, "GET")

            def do_POST(self):
                outer._serve(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def metadata(self) -> dict:
        return {
            "issuer": self.url,
            "authorization_endpoint": self.url + "/oauth2/authorize",
            "token_endpoint": self.url + "/oauth2/token",
            "registration_endpoint": self.url + "/oauth2/register",
            "code_challenge_methods_supported": ["S256"],
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def requests_to(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]

    def _serve(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        split = urllib.parse.urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length).decode("utf-8") if length else ""
        is_json = "json" in (handler.headers.get("Content-Type") or "")
        record = {
            "method": method,
            "path": split.path,
            "query": dict(urllib.parse.parse_qsl(split.query)),
            "headers": {k.lower(): v for k, v in handler.headers.items()},
            "form": {} if is_json else dict(urllib.parse.parse_qsl(raw)),
            "json": json.loads(raw) if is_json and raw else None,
        }
        self.requests.append(record)
        route = f"{method} {split.path}"
        if route == "GET /.well-known/oauth-authorization-server":
            self._send(handler, 200, self.metadata())
        elif route == "POST /oauth2/register":
            self._send(handler, 201, {"client_id": FAKE_CLIENT_ID,
                                      **(record["json"] or {})})
        elif route == "GET /oauth2/authorize":
            self._authorize(handler, record["query"])
        elif route == "POST /oauth2/token":
            self._token(handler, record["form"])
        else:
            self._send(handler, 404, {"error": "not_found"})

    def _authorize(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        self.challenge = query.get("code_challenge", "")
        state = self.state_override if self.state_override is not None else query.get("state", "")
        target = query.get("redirect_uri", "") + "?" + urllib.parse.urlencode(
            {"code": FAKE_CODE, "state": state})
        self._send(handler, 302, None, {"Location": target})

    def _token(self, handler: BaseHTTPRequestHandler, form: dict) -> None:
        digest = hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if form.get("code") != FAKE_CODE or not self.challenge or expected != self.challenge:
            self._send(handler, 400, {"error": "invalid_grant"})
            return
        self._send(handler, 200, {"access_token": self.access_token, "token_type": "Bearer",
                                  "expires_in": 300})

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, body: object,
              headers: dict | None = None) -> None:
        encoded = b"" if body is None else json.dumps(body).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(encoded)
