"""Tests for `hooks/hook_inputs.py`'s loader, which every hook's Python shares.

Each test runs a real hook as a subprocess from a plugin copy whose bundled
CLI fails to import, the way a CLI needing a newer Python fails on an older
one. The session-start harness supplies the environment, with the fake
keychain's directory first on `PATH`. The hook's exit code, its stderr and
the mount's recorded requests are what is asserted.
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

from test_session_start import COWORK_PROJECT_DIR, PLUGIN, SessionStartTestCase

sys.path.insert(0, str(PLUGIN / "hooks"))
import hook_inputs

BROKEN_CLI = '#!/usr/bin/env python3\nfrom datetime import NO_SUCH_NAME\n'
LOADER_LINE = "the bundled CLI did not load (ImportError: "

# Each hook, its log tag, and the environment that reaches its loader.
HOOKS = (
    ("session-start.sh", "session-start", {}),
    ("sweep.sh", "sweep", {}),
    ("ingest.sh", "ingest", {}),
    ("ingest.sh", "ingest", {"CLAUDE_PROJECT_DIR": COWORK_PROJECT_DIR}),
    ("memory.sh", "memory", {}),
    ("prompt-recall.sh", "prompt-recall", {}),
)


class BrokenLoader(SessionStartTestCase):
    """Production path: each hook's Python loading the CLI through `load_cli`."""

    def plugin_with_a_broken_cli(self) -> Path:
        copied = self.home / "plugin"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__"))
        (copied / "bin" / "semanticly").write_text(BROKEN_CLI, encoding="utf-8")
        return copied

    def test_a_cli_that_does_not_load_costs_each_hook_one_line(self):
        self.plugin_root = self.plugin_with_a_broken_cli()
        for script, tag, env in HOOKS:
            with self.subTest(script=script, **env):
                result = self.run_hook(self.plugin_root / "hooks" / script, **env)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertTrue(
                    result.stderr.startswith(f"semanticly[{tag}]: {LOADER_LINE}"),
                    result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.mount.requests, [])


class ExitCodes(unittest.TestCase):

    def test_every_hook_reads_the_readers_stop_code(self):
        """Each hook's `INPUTS_STOP` is a copy of `hook_inputs.EXIT_STOP`."""

        for script, _, _ in HOOKS:
            text = (PLUGIN / "hooks" / script).read_text(encoding="utf-8")
            with self.subTest(script=script):
                self.assertRegex(text, rf"(?m)^INPUTS_STOP={hook_inputs.EXIT_STOP}$")


if __name__ == "__main__":
    unittest.main()
