"""Ground rule 1: no live orders, ever.

These tests fail if an order-signing or wallet library is installed in the
project environment, or imported anywhere under ``src/mm``.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import mm

# Top-level import names of order-signing, wallet and trading-client packages.
BANNED_MODULES = (
    "py_clob_client",
    "py_order_utils",
    "eth_account",
    "eth_keys",
    "web3",
)

SRC = Path(mm.__file__).resolve().parent


@pytest.mark.parametrize("module", BANNED_MODULES)
def test_banned_module_is_not_installed(module: str) -> None:
    assert importlib.util.find_spec(module) is None, (
        f"{module} is importable; this project must not be able to sign or send orders"
    )


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_source_tree_imports_no_banned_module() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert files, "no source files found under src/mm"
    offenders = {
        str(path.relative_to(SRC)): sorted(_imported_roots(path) & set(BANNED_MODULES))
        for path in files
        if _imported_roots(path) & set(BANNED_MODULES)
    }
    assert not offenders, f"banned imports found: {offenders}"


def test_paper_trading_loads_no_order_client() -> None:
    """Importing every module under mm.paper must not pull in an order-signing package."""
    code = (
        "import importlib, pkgutil, sys, mm.paper\n"
        "for mod in pkgutil.walk_packages(mm.paper.__path__, 'mm.paper.'):\n"
        "    importlib.import_module(mod.name)\n"
        f"banned = {BANNED_MODULES!r}\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in banned)\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"mm.paper loaded: {result.stdout.strip()}"
