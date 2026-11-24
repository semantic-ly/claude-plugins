"""Tests for the plugin's manifest, hook registration and prose.

Production path: the files Claude Code itself reads on install.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "semanticly"
SESSION_START = PLUGIN / "hooks" / "session-start.sh"
INGEST = PLUGIN / "hooks" / "ingest.sh"
PROMPT_RECALL = PLUGIN / "hooks" / "prompt-recall.sh"
SWEEP = PLUGIN / "hooks" / "sweep.sh"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
MCP_CONFIG = PLUGIN / ".mcp.json"
CLI_PATH = PLUGIN / "bin" / "semanticly"

# `claude plugin validate` can exit 0 on a manifest it printed as invalid, so
# the printed failure marker decides the result.
VALIDATOR_FAIL_MARKER = "\u2718"
VALIDATOR_TIMEOUT_SECONDS = 120

CLAUDE_MISSING = shutil.which("claude") is None
SKIP_REASON = (
    "`claude` is not on PATH — install @anthropic-ai/claude-code to run this "
    "suite; the CI step installs it before running this gate"
)


def load_cli():
    loader = SourceFileLoader("semanticly_layout_cli", str(CLI_PATH))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class PluginLayout(unittest.TestCase):

    def test_the_manifest_carries_the_named_fields(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for field in ("name", "version", "description", "author",
                      "repository", "license", "keywords"):
            self.assertIn(field, manifest)

    def hooks_config(self) -> dict:
        return json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    def registrations(self, event: str) -> dict:
        """The event's hooks keyed by script name."""

        return {hook["command"].rsplit("/", 1)[1].rstrip('"'): hook
                for entry in self.hooks_config()["hooks"][event] for hook in entry["hooks"]}

    def test_every_hook_command_is_reached_through_the_plugin_root(self):
        config = self.hooks_config()
        seen = {}
        for event, entries in config["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    command = hook["command"]
                    self.assertTrue(command.startswith('"${CLAUDE_PLUGIN_ROOT}/'), command)
                    self.assertTrue(command.endswith('"'), command)
                    script = PLUGIN / command.split("}/", 1)[1].rstrip('"')
                    self.assertTrue(os.access(script, os.X_OK), script)
                    seen.setdefault(event, []).append(script.name)
        self.assertEqual(seen["SessionStart"], ["session-start.sh", "sweep.sh"])
        self.assertEqual(seen["UserPromptSubmit"], ["prompt-recall.sh"])
        self.assertEqual(seen["PreCompact"], ["ingest.sh", "memory.sh"])
        self.assertEqual(seen["SessionEnd"], ["ingest.sh", "memory.sh"])

    def test_the_matcher_carries_compact(self):
        self.assertEqual(self.hooks_config()["hooks"]["SessionStart"][0]["matcher"],
                         "startup|resume|clear|compact")

    def test_the_sweep_and_capture_registrations_are_async(self):
        self.assertIs(self.registrations("SessionStart")["sweep.sh"].get("async"), True)
        self.assertNotIn("async", self.registrations("SessionStart")["session-start.sh"])
        for event in ("PreCompact", "SessionEnd"):
            for name, hook in self.registrations(event).items():
                self.assertIs(hook.get("async"), True, f"{event} {name}")

    def test_prompt_recall_has_a_ten_second_timeout(self):
        hook = self.registrations("UserPromptSubmit")["prompt-recall.sh"]
        self.assertEqual(hook.get("timeout"), 10)

    def test_the_prompt_recall_registration_is_unconditional(self):
        """The hook runs on install, and `prompt_recall: false` is its only switch.

        A conditional registration would be a second switch, and one that
        needs a reinstall to flip.
        """

        config = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        entries = config["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(entries), 1)
        self.assertNotIn("matcher", entries[0])

    def test_the_skill_names_itself_after_its_directory(self):
        text = (PLUGIN / "skills" / "semanticly" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---\n", 2)[1]
        self.assertIn("name: semanticly", front)
        self.assertIn("description:", front)

    def test_no_hook_script_speaks_http_or_sql_of_its_own(self):
        """The one dependency is the bundled CLI. A curl here is the defect."""

        for script in (SESSION_START, SWEEP, INGEST, PROMPT_RECALL):
            body = "\n".join(
                line for line in script.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#"))
            for banned in ("curl", "wget", "psql", "urllib.request", "http.client", "nc "):
                self.assertNotIn(banned, body, f"{script.name} uses {banned}")

    def test_no_hook_script_reads_the_unconfirmed_input_fields(self):
        for script in (SESSION_START, SWEEP, INGEST, PROMPT_RECALL):
            body = "\n".join(
                line for line in script.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#"))
            for banned in ("trigger", "reason", "custom_instructions"):
                self.assertNotIn(banned, body, f"{script.name} reads {banned}")


class PluginManifest(unittest.TestCase):
    """Production path: the manifest and connector files Claude Code reads on install."""

    def manifest(self) -> dict:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))

    @unittest.skipIf(CLAUDE_MISSING, SKIP_REASON)
    def test_the_real_validator_accepts_the_plugin(self):
        claude_bin = shutil.which("claude")
        assert claude_bin is not None
        for target in (PLUGIN, REPO_ROOT):
            done = subprocess.run([claude_bin, "plugin", "validate", str(target), "--strict"],
                                  capture_output=True, text=True,
                                  timeout=VALIDATOR_TIMEOUT_SECONDS)
            output = done.stdout + done.stderr
            self.assertNotIn(VALIDATOR_FAIL_MARKER, output, output)
            self.assertEqual(done.returncode, 0, output)

    def test_the_marketplace_lists_one_plugin(self):
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        plugins = marketplace["plugins"]
        self.assertEqual([p["name"] for p in plugins], [self.manifest()["name"]])
        self.assertEqual(plugins[0]["source"], "./plugins/semanticly")
        self.assertEqual(plugins[0]["description"], self.manifest()["description"])
        self.assertFalse((REPO_ROOT / "plugins" / "cowork").exists())

    def test_the_manifest_carries_no_displayName(self):
        self.assertNotIn("displayName", self.manifest())

    def test_the_manifest_name_is_kebab_case(self):
        self.assertRegex(self.manifest()["name"], KEBAB)

    def test_the_manifest_says_unlicensed(self):
        self.assertEqual(self.manifest()["license"], "UNLICENSED")

    def test_the_description_names_the_hooks_the_skill_and_the_connector(self):
        description = self.manifest()["description"].lower()
        for word in ("hook", "skill", "connector", "claude code", "cowork"):
            self.assertIn(word, description)

    def test_user_config_holds_api_url_alone(self):
        user_config = self.manifest()["userConfig"]
        self.assertEqual(list(user_config), ["api_url"])
        field = user_config["api_url"]
        self.assertEqual(field["type"], "string")
        self.assertEqual(field["title"], "Semantic.ly API host")
        self.assertTrue(field["description"].strip())

    def test_the_api_url_default_is_the_clis_public_origin(self):
        default = self.manifest()["userConfig"]["api_url"]["default"]
        self.assertEqual(default, load_cli().PUBLIC_API_ORIGIN)

    def test_the_connector_names_the_service_url_from_user_config(self):
        servers = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(list(servers), ["semanticly"])
        self.assertEqual(servers["semanticly"],
                         {"type": "http", "url": "${user_config.api_url}/mcp"})


if __name__ == "__main__":
    unittest.main()
