"""Pins the CLI and the plugin hooks' Python to Python 3.9.

Stock macOS ships `/usr/bin/python3` at 3.9, and on a Mac without another
Python every CLI command and hook runs under it. The production path is the
source itself. An AST scan refuses the 3.10+ statements, names and keywords
this code could reach for. Where `/usr/bin/python3` is 3.9, the files also
compile under it, which catches the rest of the newer syntax, and
`semanticly --help` runs under it.
"""

from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "plugins" / "semanticly" / "bin" / "semanticly"
HOOKS = REPO_ROOT / "plugins" / "semanticly" / "hooks"
FLOOR = (3, 9)
STOCK_PYTHON = Path("/usr/bin/python3")

HEREDOC = re.compile(r"<<'PY'\n(.*?)\nPY\n", re.S)

# Names that first exist after 3.9, by the module that holds them.
NEWER_NAMES = {
    "datetime": {"UTC"},
    "enum": {"StrEnum", "ReprEnum", "verify", "member", "nonmember"},
    "itertools": {"pairwise", "batched"},
    "typing": {"TypeAlias", "ParamSpec", "Concatenate", "TypeGuard", "Self",
               "LiteralString", "Never", "assert_never", "reveal_type", "Required",
               "NotRequired", "Unpack", "TypeVarTuple", "dataclass_transform",
               "override", "is_typeddict", "assert_type"},
}
NEWER_MODULES = {"tomllib", "wsgiref.types"}
NEWER_BUILTINS = {"ExceptionGroup", "BaseExceptionGroup", "aiter", "anext",
                  "EncodingWarning"}
# Keyword arguments that first exist after 3.9, by the callable's name.
NEWER_KEYWORDS = {"zip": {"strict"}, "dataclass": {"kw_only", "slots", "match_args"},
                  "field": {"kw_only"}}
NEWER_NODES = tuple(getattr(ast, name) for name in ("Match", "TryStar", "TypeAlias")
                    if hasattr(ast, name))
TYPE_NAMES = {"int", "str", "bytes", "float", "bool", "dict", "list", "tuple", "set",
              "frozenset", "type", "object"}


def sources() -> dict[str, str]:
    """The CLI, each hook's Python file and each `<<'PY'` heredoc, by label."""

    found = {"bin/semanticly": CLI_PATH.read_text(encoding="utf-8")}
    for path in sorted(HOOKS.glob("*.py")):
        found[f"hooks/{path.name}"] = path.read_text(encoding="utf-8")
    for path in sorted(HOOKS.glob("*.sh")):
        for index, block in enumerate(HEREDOC.findall(path.read_text(encoding="utf-8"))):
            found[f"hooks/{path.name} heredoc {index}"] = block + "\n"
    return found


def annotation_nodes(tree: ast.AST) -> set[int]:
    """The ids of every node inside an annotation."""

    roots = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.arg) and node.annotation:
            roots.append(node.annotation)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(inner) for root in roots for inner in ast.walk(root)}


def is_type(node: ast.AST) -> bool:
    return ((isinstance(node, ast.Name) and node.id in TYPE_NAMES)
            or (isinstance(node, ast.Constant) and node.value is None)
            or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                and is_type(node.left)))


def callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def violations(source: str) -> list[str]:
    """Every 3.10+ construct in `source`, as `line: what`."""

    tree = ast.parse(source)
    postponed = any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                    and any(alias.name == "annotations" for alias in node.names)
                    for node in tree.body)
    in_annotation = annotation_nodes(tree)
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, NEWER_NODES):
            found.append(f"{line}: {type(node).__name__} statement")
        elif isinstance(node, ast.ImportFrom):
            newer = NEWER_NAMES.get(node.module or "", set())
            for alias in node.names:
                if alias.name in newer:
                    found.append(f"{line}: from {node.module} import {alias.name}")
            if node.module in NEWER_MODULES:
                found.append(f"{line}: from {node.module} import")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in NEWER_MODULES:
                    found.append(f"{line}: import {alias.name}")
        elif isinstance(node, ast.Attribute):
            if (isinstance(node.value, ast.Name)
                    and node.attr in NEWER_NAMES.get(node.value.id, set())):
                found.append(f"{line}: {node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in NEWER_BUILTINS:
            found.append(f"{line}: {node.id}")
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in NEWER_KEYWORDS.get(callee(node), set()):
                    found.append(f"{line}: {callee(node)}({keyword.arg}=)")
        elif (isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
                and (is_type(node.left) or is_type(node.right))
                and not (postponed and id(node) in in_annotation)):
            found.append(f"{line}: a `X | Y` type union evaluated at runtime")
    return found


def stock_is_the_floor() -> bool:
    if not STOCK_PYTHON.exists():
        return False
    version = subprocess.run(
        [str(STOCK_PYTHON), "-c", "import sys; print(*sys.version_info[:2])"],
        capture_output=True, text=True, timeout=60).stdout.split()
    return tuple(int(part) for part in version) == FLOOR


class PythonFloor(unittest.TestCase):

    def test_the_scan_finds_the_constructs_it_names(self):
        """The scan's own negative control, so a silent scan cannot pass."""

        newer = ("from datetime import UTC\n"
                 "import tomllib\n"
                 "x = zip([1], [2], strict=True)\n"
                 "y = isinstance(1, int | str)\n"
                 "match x:\n    case _:\n        pass\n")
        self.assertEqual(len(violations(newer)), 5, violations(newer))
        allowed = ("from __future__ import annotations\n"
                   "def f(a: int | None) -> str | None:\n    return None\n"
                   "flags = 1 | 2\n")
        self.assertEqual(violations(allowed), [])

    def test_the_cli_and_hooks_use_nothing_newer_than_the_floor(self):
        for label, source in sources().items():
            with self.subTest(source=label):
                self.assertEqual(violations(source), [])

    @unittest.skipUnless(stock_is_the_floor(), "/usr/bin/python3 is absent or not 3.9")
    def test_the_cli_and_hooks_compile_under_stock_python(self):
        with TemporaryDirectory() as scratch:
            paths = []
            for index, source in enumerate(sources().values()):
                path = Path(scratch) / f"source_{index}.py"
                path.write_text(source, encoding="utf-8")
                paths.append(str(path))
            result = subprocess.run(
                [str(STOCK_PYTHON), "-c",
                 "import sys\nfor p in sys.argv[1:]: compile(open(p).read(), p, 'exec')",
                 *paths], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(stock_is_the_floor(), "/usr/bin/python3 is absent or not 3.9")
    def test_the_cli_runs_under_stock_python(self):
        result = subprocess.run([str(STOCK_PYTHON), str(CLI_PATH), "--help"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: semanticly", result.stdout)


if __name__ == "__main__":
    unittest.main()
