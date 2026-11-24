"""Smoke tests that run on Windows as well as macOS and Linux.

Stdlib `unittest`, run from the repo root:

    python3 -m unittest discover -s tests/smoke -t tests/smoke

The CLI and plugin suites stand in for the OS keychain with executables on
`PATH`, which Windows cannot run. These tests need no stand-in. They start the
real CLI and the real hooks in an empty project, where nothing is bound and
nothing reaches the network, and on Windows they store one token in the real
Credential Manager.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "semanticly"
CLI_PATH = PLUGIN / "bin" / "semanticly"
HOOKS = ("session-start.sh", "sweep.sh", "prompt-recall.sh", "ingest.sh", "memory.sh")
WINDOWS = sys.platform == "win32"
SECRET = "slt_0123456789abcdef0123456789abcdef"


def find_bash() -> str | None:
    """Git Bash on Windows, where a bare `bash` can be WSL's, else `bash` on PATH."""

    if WINDOWS:
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
            candidate = Path(root or "") / "Git" / "bin" / "bash.exe"
            if root and candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


BASH = find_bash()


def load_cli():
    loader = SourceFileLoader("semanticly_smoke_cli", str(CLI_PATH))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class SmokeTestCase(unittest.TestCase):
    """An empty project and config home, so no hook finds a repository or a token."""

    def setUp(self):
        self._scratch = TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.scratch = Path(self._scratch.name)
        self.project = self.scratch / "project"
        self.project.mkdir()
        self.env = {**os.environ,
                    "SEMANTICLY_HOME": (self.scratch / "home").as_posix(),
                    "CLAUDE_PROJECT_DIR": self.project.as_posix(),
                    "CLAUDE_PLUGIN_ROOT": PLUGIN.as_posix(),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("SEMANTICLY_API_URL", "SEMANTICLY_TOKEN", "CLAUDE_PLUGIN_OPTION_API_URL"):
            self.env.pop(name, None)

    def run_command(self, argv: list[str], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", timeout=120, env=self.env,
                              cwd=str(self.project))


class Cli(SmokeTestCase):
    def test_the_cli_prints_its_help_under_this_python(self):
        done = self.run_command([sys.executable, str(CLI_PATH), "--help"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: semanticly", done.stdout)

    @unittest.skipIf(BASH is None, "no bash to run the file through")
    def test_the_cli_runs_as_a_command_and_finds_a_python_itself(self):
        done = self.run_command([BASH, "-c", '"$0" --help', CLI_PATH.as_posix()])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: semanticly", done.stdout)

    @unittest.skipUnless(WINDOWS and shutil.which("py"), "the `py` launcher is Windows only")
    def test_the_py_launcher_runs_the_cli_when_told_its_version(self):
        """A bare `py <file>` acts on the file's `#!/bin/sh` line. `py -3` does not."""

        done = self.run_command(["py", "-3", str(CLI_PATH), "--help"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: semanticly", done.stdout)

    def test_an_unnamed_repo_is_refused_before_any_request(self):
        done = self.run_command([sys.executable, str(CLI_PATH), "recall", "refunds"])
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("No repo.", done.stderr)

    def test_a_non_ascii_fact_survives_the_cli_s_streams(self):
        """Drives `use_utf8_streams` through `remember`'s stdin and its refusal on stderr."""

        done = self.run_command([sys.executable, str(CLI_PATH), "remember", "--repo",
                                 "acme/suppørt—x"], stdin="Die Frist beträgt 30 Tage — 見本\n")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("acme/suppørt—x", done.stderr)


@unittest.skipIf(BASH is None, "the hooks are bash scripts")
class Hooks(SmokeTestCase):
    PAYLOAD = json.dumps({"session_id": "smoke-1", "prompt": "what do we know? — 見本",
                          "transcript_path": "/nonexistent/smoke-1.jsonl"})

    def run_hook(self, name: str) -> subprocess.CompletedProcess:
        return self.run_command([BASH, (PLUGIN / "hooks" / name).as_posix()], self.PAYLOAD)

    def test_the_finder_names_a_python_that_runs(self):
        script = '. "$0"; [ -n "$py" ] && "$py" -c "print(1)"'
        done = self.run_command([BASH, "-c", script,
                                 (PLUGIN / "hooks" / "find-python.sh").as_posix()])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "1"), done.stderr)

    def test_every_hook_exits_zero_in_an_unbound_project(self):
        for name in HOOKS:
            with self.subTest(hook=name):
                done = self.run_hook(name)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("Traceback", done.stderr)
                self.assertNotIn("no Python", done.stderr)
                self.assertNotIn("no runnable CLI", done.stderr)

    def test_no_hook_reader_prints_a_carriage_return(self):
        """Windows text streams add one per line, and the hook's shell would keep it."""

        for script, argv in (("hook_inputs.py", ["session-start"]), ("prompt_inputs.py", [""])):
            with self.subTest(script=script):
                done = subprocess.run(
                    [sys.executable, str(PLUGIN / "hooks" / script), *argv],
                    input=self.PAYLOAD.encode("utf-8"), capture_output=True, timeout=120,
                    env=self.env, cwd=str(self.project))
                self.assertNotIn(b"\r", done.stdout + done.stderr)

        done = subprocess.run([sys.executable, str(CLI_PATH), "recall", "refunds"],
                              capture_output=True, timeout=120, env=self.env,
                              cwd=str(self.project))
        self.assertNotIn(b"\r", done.stdout + done.stderr)

    def test_session_start_reminds_an_unbound_project_to_bind(self):
        done = self.run_hook("session-start.sh")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {
            "systemMessage": "Semantic.ly isn't bound to this project; run `/semanticly:init`."})

    def test_a_bound_project_reaches_the_cli_and_stops_at_the_missing_token(self):
        """The hook reads `.semanticly`, runs the CLI, and logs its refusal in one line."""

        (self.project / ".semanticly").write_text("acme/support\n", encoding="utf-8")
        self.env["SEMANTICLY_API_URL"] = "http://127.0.0.1:9"
        done = self.run_hook("prompt-recall.sh")
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("semanticly[prompt-recall]:", done.stderr)


@unittest.skipUnless(WINDOWS, "the Credential Manager is Windows only")
class WindowsCredentials(unittest.TestCase):
    def test_the_real_credential_manager_round_trips_a_token(self):
        cli = load_cli()
        account = "semanticly-smoke.invalid:acme/support"
        target = cli.wincred_target(account)
        self.assertTrue(cli.wincred_write(target, account, SECRET))
        self.assertEqual(cli.wincred_read(target), SECRET)
        self.assertIsNone(cli.wincred_read(target + "-missing"))

    def test_the_keychain_calls_use_it(self):
        cli = load_cli()
        origin = "https://semanticly-smoke.invalid"
        self.assertEqual(cli.keychain_backend(), cli.WINDOWS_CREDENTIALS)
        cli.keychain_set("acme/smoke", SECRET, origin=origin)
        self.assertEqual(cli.keychain_get("acme/smoke", origin=origin), SECRET)
        self.assertIsNone(cli.keychain_get("acme/none", origin=origin))


if __name__ == "__main__":
    unittest.main()
