import ast
import sys
import json
import tempfile
import subprocess
from functools import cache
from pathlib import Path

# Guards the isolation of warehouse_sources in CI's test selection.
# When only an isolated product's unwatched files change, turbo-discover.js skips the Django suite
# (Core/CorePOE). That is sound only if every warehouse_sources file a Core/CorePOE file depends on
# is in the product's watched set, which `hoglint watched` derives from the tree. Otherwise a change
# to that file would skip the Core test that exercises it (a silent coverage hole).
#
# The coupling is NOT direct: a direct runtime `import ...sources.<vendor>` from Core is a
# `public-surface` finding that hoglint blocks. What remains is Core reaching source internals THROUGH the facade's lazy re-exports
# (facade/source_management.py's _LAZY map, facade/sources.py's imports). This test resolves
# every facade re-export a Core/CorePOE file actually consumes back to the module the facade names,
# and fails if hoglint does not watch that module. It reads the facade with stdlib ast, so it checks
# hoglint's answer by a second route rather than repeating it.
#
# Registry lookups resolve vendors dynamically, so this guard cannot enumerate their
# dependencies. The PostHog preset declares the ones core relies on as permanent interfaces, so
# hoglint watches them: MSSQL, because data_warehouse's SQL migration tests depend on its optional
# schema field. Other registry dependencies can be skipped on PRs and narrowed merge-queue runs; the
# hourly full master run is their backstop.
#
# Deliberately uses stdlib ast over a path walk, NOT the repo's `grimp` dependency: grimp
# does not descend products/warehouse_sources/backend/temporal/data_imports/ (an implicit namespace package — no
# __init__.py), so its graph contains zero source modules and the guard would pass blind.

_FACADE_DIR = "products/warehouse_sources/backend/facade"
_SOURCE_MGMT = "products.warehouse_sources.backend.facade.source_management"
_SOURCES = "products.warehouse_sources.backend.facade.sources"
_FACADE_MODULES = frozenset({_SOURCE_MGMT, _SOURCES})
# The package the _LAZY targets of source_management.py are relative to.
_DATA_IMPORTS = "products.warehouse_sources.backend.temporal.data_imports"

# Roots collected by the Django Core/CorePOE segments. posthog/temporal is excluded — it is
# the Temporal segment, which always runs alongside the product's own temporal job.
_SCAN_ROOTS = ("posthog", "ee", "products/product_analytics")


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".depot" / "workflows" / "ci-backend.yml").exists():
            return parent
    raise RuntimeError("repo root not found")


@cache
def _watched_files() -> frozenset[str]:
    """The files hoglint watches for warehouse_sources. One cold run takes about a minute, so it
    runs once per process. A run that fails or is incomplete fails the test: an unknown set never
    passes."""
    root = _repo_root()
    with tempfile.TemporaryDirectory() as work:
        out = Path(work) / "watched.json"
        run = subprocess.run(
            [sys.executable, "-m", "hoglint", "watched", "--out", str(out)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, f"hoglint watched exited {run.returncode}:\n{run.stderr[-4000:]}"
        answer = json.loads(out.read_text())
    assert answer["complete"], f"hoglint's watched answer is incomplete: {answer['problems']}"
    return frozenset(answer["groups"]["warehouse_sources"]["watched"])


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _facade_symbol_to_module(root: Path) -> dict[str, str]:
    """Map each name the two source-re-exporting facade modules hand out to the dotted module the
    facade names for it."""
    mapping: dict[str, str] = {}

    # source_management.py: `_LAZY = {"Symbol": "sources.<vendor>...."}`, relative to the
    # data_imports package.
    sm_tree = ast.parse((root / _FACADE_DIR / "source_management.py").read_text())
    for node in ast.walk(sm_tree):
        if not (
            isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_LAZY" for t in node.targets)
        ):
            continue
        assert isinstance(node.value, ast.Dict)
        for key, value in zip(node.value.keys, node.value.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                mapping[key.value] = f"{_DATA_IMPORTS}.{value.value}"

    # sources.py: `from products.warehouse_sources...sources.<vendor>... import (A, B, ...)`.
    src_tree = ast.parse((root / _FACADE_DIR / "sources.py").read_text())
    for node in ast.walk(src_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = node.module

    assert mapping, "parsed no re-exports from the warehouse_sources facade"
    return mapping


def _module_path(root: Path, module: str) -> str:
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if (root / candidate).exists():
            return candidate
    raise AssertionError(f"the warehouse_sources facade names {module}, which is no file in the repo")


def _core_consumed_facade_symbols(tree: ast.AST) -> set[str]:
    """Names a module imports from the source-re-exporting facade modules, excluding
    TYPE_CHECKING-only imports (those don't affect runtime test behavior)."""
    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            if _is_type_checking(node.test):
                for stmt in node.orelse:
                    self.visit(stmt)
                return
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module in _FACADE_MODULES:
                for alias in node.names:
                    found.add(alias.name)

    Visitor().visit(tree)
    return found


def test_core_facade_coupled_sources_are_watched():
    root = _repo_root()
    excluded = root / "posthog" / "temporal"  # the Temporal segment; always runs
    symbol_to_module = _facade_symbol_to_module(root)

    consumed: set[str] = set()
    for rel in _SCAN_ROOTS:
        base = root / rel
        if not base.exists():
            continue
        for file in base.rglob("*.py"):
            if file.is_relative_to(excluded):
                continue
            text = file.read_text(errors="ignore")
            if "facade.source_management" not in text and "facade.sources" not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(file))
            except SyntaxError:
                continue
            consumed |= _core_consumed_facade_symbols(tree)

    coupled = {_module_path(root, symbol_to_module[s]) for s in consumed if s in symbol_to_module}
    assert coupled, "found no Core/CorePOE use of the warehouse_sources facade's re-exports"
    missing = coupled - _watched_files()
    assert not missing, (
        f"Core/CorePOE reaches {sorted(missing)} through the warehouse_sources facade, but `hoglint watched` "
        "does not watch them for warehouse_sources. A change to those files would skip the Core tests that "
        "exercise them. hoglint's reached-watched rule should ask for every file on the path from a facade name "
        "to its definition: run `hoglint watched` and report the gap to the owners of hoglint."
    )
