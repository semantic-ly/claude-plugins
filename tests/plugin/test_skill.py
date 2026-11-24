"""Tests for the plugin's one authored skill.

Stdlib `unittest`. The Claude.ai package ships this file unchanged, so
Anthropic's upload rules apply to it here, and `tests/package`
pins that the package is a byte-for-byte copy.

The frontmatter rules are Anthropic's, read from the Agent Skills page on
2026-09-12. `name` is
capped at 64 characters, is lowercase letters, numbers and hyphens only, and may
not contain the reserved words "anthropic" or "claude". `description` is capped
at 1024, is non-empty, and must state both what the skill does and when Claude
should use it. Nothing in this tree enforces either cap at runtime, because the
upload rejects a violation far from the edit, so this file catches it.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "semanticly"
SKILL = PLUGIN / "skills" / "semanticly" / "SKILL.md"
COMMANDS = {name: PLUGIN / "commands" / f"{name}.md" for name in ("recall", "remember", "init")}

NAME_MAX = 64
DESCRIPTION_MAX = 1024
NAME_SHAPE = re.compile(r"^[a-z0-9-]+$")
RESERVED = ("anthropic", "claude")
XML_TAG = re.compile(r"<[^>]+>")


def frontmatter() -> dict[str, str]:
    """The real SKILL.md's YAML frontmatter, as a flat mapping.

    Hand-parsed rather than handed to PyYAML, because this suite has no
    dependencies by design. The skill's frontmatter is two scalar keys and the
    parser is allowed to be exactly that narrow — a nested value here would fail
    the shape assertion below rather than parse into something surprising.
    """
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must open with a --- delimited frontmatter block"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(": ")
        assert sep and key and not key.startswith(" "), f"not a scalar frontmatter line: {line!r}"
        fields[key] = value
    return fields


class Frontmatter(unittest.TestCase):
    def setUp(self):
        self.fields = frontmatter()

    def test_the_two_required_fields_are_present_and_non_empty(self):
        for key in ("name", "description"):
            self.assertIn(key, self.fields)
            self.assertTrue(self.fields[key].strip(), f"{key} is empty")

    def test_the_name_is_lowercase_letters_numbers_and_hyphens_within_its_cap(self):
        name = self.fields["name"]
        self.assertLessEqual(len(name), NAME_MAX)
        self.assertRegex(name, NAME_SHAPE)

    def test_the_name_carries_neither_reserved_word(self):
        # The failure this catches is naming a skill for the surface it ships
        # to. `claude-ai-memory` reads like the obvious name and is rejected at
        # upload, where nothing points back at this file.
        name = self.fields["name"]
        for word in RESERVED:
            self.assertNotIn(word, name, f"{word!r} is reserved in a skill name")

    def test_the_description_is_within_its_cap(self):
        self.assertLessEqual(len(self.fields["description"]), DESCRIPTION_MAX)

    def test_the_description_says_what_the_skill_does_and_when_to_use_it(self):
        # The rule asks for two halves and this pins their SHAPE, which is all a
        # test can reach. The when-half is a "Use when …" clause; the what-half
        # is whatever stands before it, and it has to name the surface this
        # skill drives rather than being an empty run-up to the trigger.
        description = self.fields["description"]
        trigger = description.find("Use when")
        self.assertNotEqual(trigger, -1, "the description states no trigger")
        does = description[:trigger].strip()
        self.assertTrue(does, "the description states a trigger and no capability")
        self.assertTrue(
            any(tool in does for tool in ("recall", "remember", "list_repos", "resolve_terms")),
            "the capability half names none of the tools the skill is about",
        )

    def test_neither_field_carries_an_xml_tag(self):
        for key in ("name", "description"):
            self.assertNotRegex(self.fields[key], XML_TAG, f"{key} carries markup")


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def json_blocks(text: str) -> list[object]:
    """Every fenced ```json block in `text`, parsed."""
    return [json.loads(body) for body in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)]


# The five tools and the labels `recall`'s block carries.
TOOLS = ("recall", "remember", "ingest", "list_repos", "resolve_terms")
LABELS = ("[standing]", "[term]", "[term:contrast]", "[term:mistake]",
          "[term:superseded_by]", "[term:use_instead]", "[term:mentions]",
          "[semantic]", "[contradicts #N]")

# Withdrawn variables, and the remedies no string a model reads may carry.
# Each entry is a regex matched case-insensitively.
WITHDRAWN = (
    r"SEMANTICLY_REPO\b",
    r"SEMANTICLY_PROMPT_RECALL",
    r"SEMANTICLY_SESSION_BUDGET",
    r"writer branch",
    r"agents/",
    r"propos",
    r"ask an admin",
    r"request access",
    r"connect to /mcp/",
    r"list_repos to",
    r"send it again with a reason",
    r"token whose",
    r"(another|different|other) token",
    r"swap",
    r"another branch",
    r"branch by name",
    r"name a branch",
    r"as a ref",
    r"recall[^\n]*--topic",
)

# What a step-5 remember example may not carry beside `facts`.
PER_STATEMENT = ("term", "aliases", "rationale", "external_id", "external_ids",
                 "supersedes", "supports", "contradicts")


class Teaching(unittest.TestCase):
    """What the skill teaches."""

    def test_the_skill_names_the_five_tools_and_the_labels(self):
        text = skill_text()
        for tool in TOOLS:
            self.assertIn(f"`{tool}`", text, tool)
        for label in LABELS:
            self.assertIn(f"`{label}`", text, label)
        self.assertIn("UNREVIEWED", text)
        paragraphs = [p for p in text.split("\n\n") if p.startswith("A `[contradicts #N]` line")]
        self.assertEqual(len(paragraphs), 1, "no paragraph teaches the contradiction line")
        self.assertIn("never drop one silently", paragraphs[0])

    def test_the_skill_teaches_the_first_recall_exclusions(self):
        text = skill_text()
        self.assertRegex(text, r"[Ll]eave `standing` unset")
        self.assertIn("Claude Code session", text)
        self.assertIn("`exclude_facts`", text)
        self.assertIn("`standing: false`", text)
        self.assertNotIn("injected.json", text)

    def test_the_skill_teaches_one_object_per_statement(self):
        calls = [b for b in json_blocks(skill_text()) if isinstance(b, dict) and "facts" in b]
        self.assertEqual(len(calls), 1, "the skill carries exactly one remember example")
        call = calls[0]
        self.assertIn("repo", call)
        self.assertIn("topics", call)
        for key in PER_STATEMENT:
            self.assertNotIn(key, call, f"{key} sits beside facts")
        facts = call["facts"]
        self.assertGreater(len(facts), 1, "one call, several statements")
        for fact in facts:
            self.assertIsInstance(fact, dict)
            self.assertIn("body", fact)
        carried = {key for fact in facts for key in fact} - {"body"}
        self.assertTrue(carried & set(PER_STATEMENT), "no statement carries its own field")

    def test_the_skill_relays_no_branch_and_names_the_write_surface(self):
        text = skill_text()
        self.assertIn("`no_branch`", text)
        self.assertRegex(text, r"\| `no_branch` \|[^\n]*Relay the sentence[^\n]*do not retry")
        self.assertRegex(text, r"`(422 )?refused_content`")
        self.assertRegex(text, r"no `remember` or `ingest`")

    def test_the_skill_tells_a_cowork_task_to_recall_at_its_start(self):
        self.assertRegex(skill_text(), r"Cowork task[^.]*`recall`[^.]*start")

    def test_the_skill_names_no_host_only_path(self):
        text = skill_text()
        for path in ("CLAUDE_PLUGIN_ROOT", "bin/semanticly", "~/.semanticly", "checkpoints/"):
            self.assertNotIn(path, text, path)

    def test_no_file_names_a_withdrawn_variable_or_a_widening_remedy(self):
        for path in (SKILL, *COMMANDS.values()):
            text = path.read_text(encoding="utf-8")
            for pattern in WITHDRAWN:
                self.assertIsNone(
                    re.search(pattern, text, re.IGNORECASE),
                    f"{path.name} matches {pattern!r}")


class Commands(unittest.TestCase):
    """The three commands run the bundled CLI and let `.semanticly` decide."""

    def test_each_command_carries_a_description(self):
        for name, path in COMMANDS.items():
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), name)
            front = text.split("---\n", 2)[1]
            self.assertRegex(front, r"(?m)^description: \S", name)

    def test_each_command_runs_the_bundled_cli_without_naming_a_repo(self):
        for name, path in COMMANDS.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn(f'"$CLAUDE_PLUGIN_ROOT/bin/semanticly" {name}', text, name)
            # Naming the flag to say leave it out is the instruction. Passing
            # it, with a value, is what this catches.
            self.assertNotRegex(text, r"--repo[ =]", name)

    def test_init_says_it_binds_nothing_without_a_terminal(self):
        text = COMMANDS["init"].read_text(encoding="utf-8")
        self.assertRegex(text, r"binds nothing")
        self.assertIn("terminal", text)


class Prose(unittest.TestCase):
    def test_the_skill_never_mentions_governance(self):
        # There is no `propose` tool, agents do not see
        # proposals, and a model told one exists will try to drive it. This is
        # client-facing prose, so the word belongs nowhere in the file.
        hits = re.findall(r"propos\w*", SKILL.read_text(encoding="utf-8"), re.IGNORECASE)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
