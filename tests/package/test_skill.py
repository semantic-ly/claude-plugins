"""Tests for the script that packages the plugin's skill for Claude.ai.

Stdlib `unittest`, run from the repo root:

    python3 -m unittest discover -s tests/package -t tests/package

The zip root is pinned by RUNNING `package.sh` and reading what it wrote. A test
that built its own archive would assert that `zipfile` works. The skill itself
is authored once, in the plugin, and its frontmatter rules are pinned by
`tests/plugin/test_skill.py`.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "scripts"
PACKAGE = PACKAGE_DIR / "package.sh"
SKILL_RELATIVE = Path("plugins") / "semanticly" / "skills" / "semanticly" / "SKILL.md"
SKILL = REPO_ROOT / SKILL_RELATIVE

ARCHIVE_ROOT = "semanticly"
ARCHIVE_FILE = "semanticly/SKILL.md"


def stage_tree(root: Path, *, with_skill: bool) -> Path:
    """Copy the script into a temporary repo tree and return its directory.

    The script derives every path from its own location, so the copy runs the
    real code without touching this tree.
    """
    here = root / "scripts"
    here.mkdir(parents=True)
    shutil.copy2(PACKAGE, here / "package.sh")
    if with_skill:
        skill = root / SKILL_RELATIVE
        skill.parent.mkdir(parents=True)
        shutil.copy2(SKILL, skill)
    return here


class Package(unittest.TestCase):
    """Every test here runs the real `package.sh` and reads what it produced."""

    def run_package(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        done = subprocess.run(
            [str(PACKAGE), *args],
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def test_the_archives_internal_root_is_the_skills_own_name(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "skill.zip"
            self.run_package(str(out))
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                files = [n for n in names if not n.endswith("/")]
                self.assertEqual(files, [ARCHIVE_FILE])
                for name in names:
                    self.assertTrue(
                        name == f"{ARCHIVE_ROOT}/" or name.startswith(f"{ARCHIVE_ROOT}/"),
                        f"{name} sits outside {ARCHIVE_ROOT}/",
                    )
                self.assertEqual(
                    archive.read(ARCHIVE_FILE),
                    SKILL.read_bytes(),
                    "the archived skill is not the one in the tree",
                )

    def test_the_default_destination_is_the_repos_dist_directory(self):
        # Driven in a COPY of the tree, so the default path's own code runs
        # without leaving a build artifact here.
        with TemporaryDirectory() as tmp:
            here = stage_tree(Path(tmp), with_skill=True)
            done = subprocess.run(
                [str(here / "package.sh")],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            written = Path(done.stdout.strip()).resolve()
            self.assertEqual(written, (Path(tmp) / "dist" / "semanticly-skill.zip").resolve())
            self.assertTrue(written.is_file())

    def test_packaging_twice_does_not_accumulate_entries(self):
        # `zip` ADDS to an archive that already exists, so a stale first run
        # would ship inside the second one's upload.
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "skill.zip"
            self.run_package(str(out))
            with zipfile.ZipFile(out, "a") as archive:
                archive.writestr("semanticly/STALE.md", "left over from a previous run")
            self.run_package(str(out))
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(
                    [n for n in archive.namelist() if not n.endswith("/")],
                    [ARCHIVE_FILE],
                )

    def test_a_missing_skill_is_refused_rather_than_packaged_empty(self):
        with TemporaryDirectory() as tmp:
            here = stage_tree(Path(tmp), with_skill=False)
            done = subprocess.run(
                [str(here / "package.sh"), str(Path(tmp) / "skill.zip")],
                cwd=tmp,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("SKILL.md", done.stderr)
            self.assertFalse((Path(tmp) / "skill.zip").exists())

    def test_the_packaged_skill_is_byte_identical_to_the_plugins_authored_skill(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "skill.zip"
            self.run_package(str(out))
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(archive.read(ARCHIVE_FILE), SKILL.read_bytes())


class Layout(unittest.TestCase):
    def test_the_package_directory_holds_only_its_packaging(self):
        names = {p.name for p in PACKAGE_DIR.iterdir()}
        self.assertEqual(names, {"package.sh"})

    def test_one_authored_skill_exists(self):
        found = sorted(
            p
            for p in (REPO_ROOT / "plugins").rglob("SKILL.md")
            if p.parent.name == "semanticly"
        )
        self.assertEqual(found, [SKILL])


if __name__ == "__main__":
    unittest.main()
