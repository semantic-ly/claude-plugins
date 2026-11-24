"""`tests/testdata/relative_time.json` pins the CLI's `ago_full`.

The console prints the same strings from its own copy of the cases.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from test_semanticly import REPO_ROOT, cli

CASES = json.loads((REPO_ROOT / "tests" / "testdata" / "relative_time.json").read_text())["cases"]


class AgoFullMatchesTheFixture(unittest.TestCase):
    def test_ago_full_matches_the_fixture(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        for case in CASES:
            with self.subTest(seconds=case["seconds"]):
                when = now - timedelta(seconds=case["seconds"])
                self.assertEqual(cli.ago_full(when, now), case["expected"])

    def test_a_future_moment_reads_as_now(self):
        now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        self.assertEqual(cli.ago_full(now + timedelta(seconds=30), now), "now")


if __name__ == "__main__":
    unittest.main()
