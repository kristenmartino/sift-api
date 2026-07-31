"""Meta-tests: deterministic checks that the test suite itself is sound.

The suite certifies everything else in this repo; nothing certifies the suite.
These do — cheaply, in CI, forever.

The motivating case was real. `tests/test_rss.py::test_known_values` was named
"known values" but asserted only `stable_hash("hello") == stable_hash("hello")`
— a tautology that passes for ANY implementation, guarding a function that
generates the primary key of every row in `articles`. Nothing flagged it,
because no linter checks whether a test can fail.

Ruff's PT/B rules catch adjacent problems (PT015 `assert <constant>`, B011
`assert False`, PT011 over-broad `pytest.raises`) but none of them catch a test
function that simply never asserts anything.
"""

from __future__ import annotations

import ast
import json
import pathlib

TESTS_DIR = pathlib.Path(__file__).parent
REPO_ROOT = TESTS_DIR.parent
EVAL_DIR = REPO_ROOT / "data" / "eval"

# Escape hatch: put this in a test's docstring, with a reason, when a test
# genuinely asserts nothing (e.g. it only checks that an import or a call does
# not raise). Requiring the reason in prose keeps it deliberate.
PRAGMA = "no-assert-ok:"


def _has_assertion(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function body contains anything that can fail a test."""
    for node in ast.walk(fn):
        # Plain `assert ...`
        if isinstance(node, ast.Assert):
            return True
        # `with pytest.raises(...)` / `pytest.warns(...)`
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    func = call.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in {"raises", "warns", "deprecated_call"}:
                        return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                # mock.assert_called_once_with(...), .assert_not_called(), etc.
                if func.attr.startswith("assert"):
                    return True
                # TestClient response helpers used as the assertion, e.g.
                # `response.raise_for_status()`
                if func.attr == "raise_for_status":
                    return True
            # pytest.fail(...) / pytest.xfail(...)
            elif isinstance(func, ast.Name) and func.id in {"fail", "xfail"}:
                return True
    return False


def _test_functions() -> list[tuple[pathlib.Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every test function in the suite, including methods inside test classes."""
    found = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                found.append((path, node))
    return found


def _module_level_test_functions() -> list[tuple[pathlib.Path, str]]:
    """Only `def test_x` at module scope — `ast.walk` would flatten methods out
    of their classes, and two classes may each legitimately define `test_empty`."""
    found = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                found.append((path, node.name))
    return found


def test_the_meta_guard_actually_sees_the_suite():
    """Guard the guard: if the glob or the AST walk silently stops matching,
    every check below would pass vacuously — which is the exact failure mode
    this file exists to prevent."""
    fns = _test_functions()
    assert len(fns) > 100, f"expected the full suite, only found {len(fns)} test functions"
    assert len({p.name for p, _ in fns}) > 10, "expected many test modules"


def test_every_test_function_asserts_something():
    offenders = []
    for path, fn in _test_functions():
        if _has_assertion(fn):
            continue
        if PRAGMA in (ast.get_docstring(fn) or ""):
            continue
        offenders.append(f"{path.name}::{fn.name}")

    assert not offenders, (
        "These test functions contain no assertion, no pytest.raises, and no "
        "mock assert_* call — they pass no matter what the code does. Add a "
        f"real assertion, or a '{PRAGMA} <reason>' line to the docstring if "
        f"that is genuinely intended: {offenders}"
    )


def test_no_duplicate_test_names_within_a_module():
    """A duplicated `def test_x` silently shadows the earlier one — the first
    body never runs, and the suite still reports a passing count."""
    seen: dict[str, set[str]] = {}
    dupes = []
    for path, name in _module_level_test_functions():
        names = seen.setdefault(path.name, set())
        if name in names:
            dupes.append(f"{path.name}::{name}")
        names.add(name)

    assert not dupes, (
        "These test names are defined twice at module level — the second "
        "definition shadows the first, so the first body never runs and the "
        f"suite still reports it as passing: {dupes}"
    )


def test_eval_corpora_parse_and_have_unique_ids():
    """Every labeled corpus under data/eval must be valid JSONL with unique
    ids. A duplicated id silently pads the row count that
    tests/test_quality_gate.py asserts a floor on."""
    if not EVAL_DIR.exists():
        return

    for path in sorted(EVAL_DIR.glob("*.jsonl")):
        rows = []
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError as e:
                raise AssertionError(f"{path.name}:{lineno} is not valid JSON: {e}") from e

        assert rows, f"{path.name} is empty"

        id_key = "id" if "id" in rows[0] else "batch_id" if "batch_id" in rows[0] else None
        assert id_key, f"{path.name} rows have neither 'id' nor 'batch_id'"

        ids = [r.get(id_key) for r in rows]
        assert all(ids), f"{path.name} has a row with a missing/empty {id_key}"
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"{path.name} has duplicate {id_key} values: {sorted(dupes)}"
