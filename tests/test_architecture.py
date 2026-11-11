"""Structural rules that keep the write path small and reviewable."""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "ha_insights_mcp"

# The only module allowed to change device states.
WRITERS = {"actions/executor.py"}


def _modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in SRC.rglob("*.py")
    }


def test_call_service_is_used_only_by_the_executor() -> None:
    callers = {
        name
        for name, tree in _modules().items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "call_service"
    }
    assert callers <= WRITERS, f"call_service used outside the executor: {callers - WRITERS}"


def test_ha_package_does_not_import_mcp() -> None:
    for name, tree in _modules().items():
        if not name.startswith("ha/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            assert not any(m == "mcp" or m.startswith("mcp.") for m in imported), name
