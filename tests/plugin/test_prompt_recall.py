"""Tests for `hooks/prompt-recall.sh`, the plugin's `UserPromptSubmit` hook.

Every test runs the real hook as a subprocess against the real bundled CLI,
which speaks Streamable HTTP to `fakes.FakeMount`. The session-start harness
supplies the environment, with the fake keychain's directory first on `PATH`,
so the CLI reads its token through its own keychain road and never reaches the
real one. The wire bytes the mount serves are the setup, and the hook's stdout,
stderr and the mount's recorded calls are what is asserted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

from test_ingest_hook import OTHER_REPO, recall_call, record
from test_session_start import (
    COWORK_PROJECT_DIR,
    PLUGIN,
    REPO,
    SESSION,
    SessionStartTestCase,
    closed_port_origin,
    fakes,
)

PROMPT_RECALL = PLUGIN / "hooks" / "prompt-recall.sh"

sys.path.insert(0, str(PLUGIN / "hooks"))
import prompt_inputs  # noqa: E402

PROMPT = "How do refunds work?"
MAX_QUERY_CHARS = 2000


def turn_line(sha: str, body: str, layer: str = "semantic") -> dict:
    """One `/context` line from a per-turn layer."""

    return {"n": 1, "layer": layer, "label": layer, "display_id": "F-1",
            "sha": sha, "body": body, "repo": REPO}


def turn(lines: list[dict]) -> dict:
    """A per-turn `/context` body, which `recall` relays as its structured content."""

    return {"lines": lines, "level": "write",
            "text": "\n".join(f"[{row['label']}] {row['body']}" for row in lines)}


def refusal(code: str) -> dict:
    """A tool's `isError` result carrying a `/v1` envelope."""

    envelope = {"error": {"code": code, "message": f"The server refused with {code}."}}
    return fakes.tool_result(f"{code}: refused", envelope, is_error=True)


class PromptRecallTestCase(SessionStartTestCase):
    """The session-start harness, aimed at the per-prompt hook and its payload."""

    def setUp(self):
        super().setUp()
        self.prompt = PROMPT
        self.transcript = self.home / "t.jsonl"
        self.transcript.write_text("", encoding="utf-8")

    def ready(self) -> None:
        """A bound project, a stored token and an answer holding one new line."""

        self.bind()
        self.store_token()
        self.answer(turn([turn_line("a1", "Refunds run 30 days.")]))

    def payload(self) -> str:
        return json.dumps({
            "session_id": SESSION, "transcript_path": str(self.transcript),
            "cwd": str(self.project), "hook_event_name": "UserPromptSubmit",
            "prompt": self.prompt,
        })

    def write_config(self, body: object) -> None:
        home = self.home / "semanticly"
        home.mkdir(exist_ok=True)
        (home / "config.json").write_text(json.dumps(body), encoding="utf-8")

    def recall(self, script=PROMPT_RECALL, **env) -> subprocess.CompletedProcess:
        return self.run_hook(script, **env)

    def context_text(self, result: subprocess.CompletedProcess) -> str:
        inner = self.output(result)["hookSpecificOutput"]
        self.assertEqual(inner["hookEventName"], "UserPromptSubmit")
        return inner["additionalContext"]

    def recall_arguments(self) -> list[dict]:
        return [call["arguments"] for call in self.tool_calls() if call["name"] == "recall"]

    def checkpoints(self):
        return self.home / "semanticly" / "checkpoints"


class OnByDefault(PromptRecallTestCase):
    """Production path: the hook's `prompt_recall` read through the CLI's config rule."""

    def test_recall_runs_with_no_flag_set(self):
        self.ready()
        result = self.recall()
        header, text = self.context_text(result).split("\n\n", 1)
        self.assertIn(f"`{REPO}`", header)
        self.assertEqual(text, "[semantic] Refunds run 30 days.")
        self.assertEqual(len(self.recall_arguments()), 1)
        self.assertEqual(result.stderr, "")

    def test_literal_false_turns_it_off_and_a_typo_leaves_it_on_and_says_so(self):
        """Drives the opt-out guard. The project is bound and the token stored,
        so only the guard can stop the call."""

        self.ready()
        self.write_config({"prompt_recall": False})
        result = self.recall()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(self.mount.requests, [])
        self.assertFalse(self.checkpoints().exists())

        self.write_config({"prompt_recall": "false"})
        result = self.recall()
        self.assertIn("Refunds run 30 days.", self.context_text(result))
        self.assertIn("semanticly[prompt-recall]: `prompt_recall`", result.stderr)
        self.assertIn("stays on", result.stderr)


    def test_a_malformed_config_file_writes_one_line_and_injects_nothing(self):
        """The CLI's refusal of the file is the one line, and the hook adds none."""

        self.ready()
        home = self.home / "semanticly"
        home.mkdir(exist_ok=True)
        (home / "config.json").write_text("{not json", encoding="utf-8")
        result = self.recall()
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("is not valid JSON", result.stderr)
        self.assertNotIn("stays on", result.stderr,
                         "the CLI refuses the file, so recall does not stay on")


class Request(PromptRecallTestCase):
    """Production path: the hook's argv to the CLI, read off the mount's recording."""

    def test_the_request_sends_standing_false_and_budget_600(self):
        self.ready()
        self.recall()
        self.assertEqual(self.recall_arguments(), [{
            "repo": REPO, "standing": False, "query": PROMPT, "token_budget": 600,
            "session_ref": SESSION}])

    def test_the_query_is_cut_at_2000_code_points(self):
        """Review Focus 4. The 2000th code point is astral, and it arrives whole."""

        self.ready()
        head = "a" * (MAX_QUERY_CHARS - 1) + "\U0001F600"
        self.prompt = head + "b" * 50
        result = self.recall()
        self.assertEqual(self.recall_arguments()[0]["query"], head)
        self.assertEqual(result.stderr.count("\n"), 1, result.stderr)
        self.assertIn("semanticly[prompt-recall]:", result.stderr)
        self.assertIn(f"first {MAX_QUERY_CHARS}", result.stderr)

    def test_a_prompt_opening_with_a_dash_is_a_query(self):
        self.ready()
        self.prompt = "--standing --repo acme/other what changed?"
        self.recall()
        arguments = self.recall_arguments()[0]
        self.assertEqual((arguments["query"], arguments["repo"], arguments["standing"]),
                         (self.prompt, REPO, False))

    def test_a_second_turn_excludes_what_the_first_turn_injected(self):
        """Drives `recall --session`'s record and read through two hook runs."""

        self.ready()
        self.recall()
        self.recall()
        self.assertEqual(self.recall_arguments()[1]["exclude_facts"], ["a1"])

    def test_an_unbound_project_calls_nothing(self):
        self.store_token()
        result = self.recall()
        self.assertEqual(result.stdout, "")
        self.assertIn(".semanticly", result.stderr)
        self.assertEqual(self.mount.requests, [])


class Cowork(PromptRecallTestCase):
    """The Cowork arm. Each test runs a plugin copy whose `.semanticly` lookup succeeds.

    Under `/private/var/empty` no file can be bound, so the lookup alone would
    call nothing. The copy finds a binding, which leaves the hook's Cowork arm
    as the only thing that picks the repository.
    """

    def setUp(self):
        super().setUp()
        self.plugin_root = self.plugin_that_finds_the_binding_anywhere()

    def recall_cowork(self) -> subprocess.CompletedProcess:
        return self.recall(self.plugin_root / "hooks" / "prompt-recall.sh",
                           CLAUDE_PROJECT_DIR=COWORK_PROJECT_DIR)

    def write_transcript(self, *records: dict) -> None:
        self.transcript.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_cowork_recalls_nothing_before_the_session_names_a_repo(self):
        """The bound repo has a token and an answer, so only the Cowork arm stops the call."""

        self.ready()
        self.write_transcript(record("c1", "user", "hello"))
        result = self.recall_cowork()
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.mount.requests, [])
        self.assertEqual(result.stderr,
                         "semanticly[prompt-recall]: this Cowork task named no repository "
                         "in a recall; nothing recalled\n")

    def test_cowork_recalls_the_named_repo_after(self):
        """Drives `session_repo` over the transcript, then `recall --surface cowork`."""

        self.bind(OTHER_REPO)
        self.store_token(REPO)
        self.answer(turn([turn_line("a1", "Refunds run 30 days.")]))
        self.write_transcript(record("c1", "user", "refunds?"), recall_call(REPO))
        result = self.recall_cowork()
        self.assertIn("Refunds run 30 days.", self.context_text(result))
        self.assertEqual([a["repo"] for a in self.recall_arguments()], [REPO])
        names = {r["body"]["params"]["clientInfo"]["name"] for r in self.mount.requests
                 if r["method"] == "POST" and r["body"].get("method") == "initialize"}
        self.assertEqual(names, {"cowork"})


class Failures(PromptRecallTestCase):
    """Each failure injects nothing, writes one stderr line naming its code, and exits 0.

    `run_hook` asserts the exit status and the one-line ceiling on every case.
    """

    def assert_one_line(self, result: subprocess.CompletedProcess, needle: str) -> None:
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("semanticly[prompt-recall]:", result.stderr)
        self.assertIn(needle, result.stderr)

    def test_each_failure_injects_nothing_and_names_its_code(self):
        self.bind()
        self.store_token()
        answers = {
            "invalid_token": {"initialize": fakes.http_answer(401, {"error": {
                "code": "invalid_token", "message": "The token was revoked."}})},
            "rate_limited": {"initialize": fakes.http_answer(429, {"error": {
                "code": "rate_limited", "message": "Slow down."}})},
            "no_branch": {"recall": refusal("no_branch")},
            "utterance_too_long": {"recall": refusal("utterance_too_long")},
            "embedding_failed": {"recall": refusal("embedding_failed")},
            "embedding_busy": {"recall": refusal("embedding_busy")},
        }
        for code, script in answers.items():
            with self.subTest(code=code):
                self.mount.script = {"recall": fakes.tool_result("", turn([])), **script}
                self.assert_one_line(self.recall(), code)
        self.assertFalse(self.checkpoints().exists())

    def test_a_revoked_token_says_to_run_login(self):
        """Review Focus 2."""

        self.bind()
        self.store_token()
        self.mount.script["initialize"] = fakes.http_answer(
            401, {"error": {"code": "invalid_token", "message": "The token was revoked."}})
        self.assert_one_line(self.recall(), "semanticly login")

    def test_an_unreachable_api_injects_nothing_and_says_so(self):
        self.bind()
        self.origin = closed_port_origin()
        self.store_token()
        self.assert_one_line(self.recall(), "Could not reach")

    def test_a_payload_with_no_prompt_calls_nothing(self):
        self.ready()
        self.prompt = "   "
        self.assert_one_line(self.recall(), "prompt")
        self.assertEqual(self.mount.requests, [])

    def test_a_block_with_no_term_or_semantic_line_injects_nothing(self):
        self.ready()
        self.answer(turn([turn_line("e1", "An expansion.", layer="expansion"),
                          turn_line("c1", "A contradiction.", layer="contradiction")]))
        result = self.recall()
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(len(self.recall_arguments()), 1)


    def test_a_cut_prompt_whose_recall_fails_writes_one_line(self):
        """The cut note waits for the outcome and shares its line."""

        self.bind()
        self.store_token()
        self.mount.script["initialize"] = fakes.http_answer(
            401, {"error": {"code": "invalid_token", "message": "The token was revoked."}})
        self.prompt = "a" * (MAX_QUERY_CHARS + 10)
        result = self.recall()
        self.assert_one_line(result, f"first {MAX_QUERY_CHARS}")
        self.assertIn("invalid_token", result.stderr)

    def test_a_broken_input_reader_writes_one_line(self):
        """A crash inside `prompt_inputs.py` reaches the log as one named line."""

        copied = self.home / "plugin"
        shutil.copytree(PLUGIN, copied, ignore=shutil.ignore_patterns("__pycache__"))
        (copied / "hooks" / "session_repo.py").write_text(
            'raise RuntimeError("broken reader")\n', encoding="utf-8")
        self.plugin_root = copied
        self.ready()
        result = self.recall(copied / "hooks" / "prompt-recall.sh")
        self.assert_one_line(result, "input reader failed: RuntimeError: broken reader")
        self.assertEqual(self.mount.requests, [])


class PromptInputs(unittest.TestCase):
    """`prompt_inputs.py`'s pure parts, called directly."""

    def test_the_cut_keeps_an_astral_code_point_at_index_1999_whole(self):
        head = "a" * (MAX_QUERY_CHARS - 1) + "\U0001F600"
        query, note = prompt_inputs.cut_query(head + "b")
        self.assertEqual(query, head)
        self.assertIn(f"first {MAX_QUERY_CHARS}", note)

    def test_a_prompt_at_the_ceiling_is_not_cut(self):
        prompt = "a" * MAX_QUERY_CHARS
        self.assertEqual(prompt_inputs.cut_query(prompt), (prompt, ""))

    def test_a_payload_with_no_session_or_prompt_stops(self):
        for raw in ("not json", "[]", json.dumps({"session_id": "s", "prompt": "  "}),
                    json.dumps({"prompt": "p"})):
            with self.subTest(raw=raw), self.assertRaises(prompt_inputs.Stop):
                prompt_inputs.read_prompt(raw)
        self.assertEqual(
            prompt_inputs.read_prompt(json.dumps(
                {"session_id": " s ", "prompt": " p ", "transcript_path": "/t"})),
            ("s", "p", "/t"))

    def test_the_shell_exit_codes_match_the_input_readers(self):
        """The hook's two numbers are copies of `prompt_inputs`' constants."""

        script = PROMPT_RECALL.read_text(encoding="utf-8")
        for name, value in (("INPUTS_STOP", prompt_inputs.EXIT_STOP),
                            ("INPUTS_OFF", prompt_inputs.EXIT_OFF)):
            self.assertRegex(script, rf"(?m)^{name}={value}$")


if __name__ == "__main__":
    unittest.main()
