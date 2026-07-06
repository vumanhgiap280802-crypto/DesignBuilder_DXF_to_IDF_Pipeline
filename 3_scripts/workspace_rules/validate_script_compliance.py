#!/usr/bin/env python3
"""
Validate that Python scripts in 3_scripts use WorkspaceGuard.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
SCRIPTS_DIR = ROOT / "3_scripts"
EXEMPT = {"__init__.py", "workspace_guard.py"}


def has_workspace_guard_import(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "workspace_guard",
            "workspace_rules.workspace_guard",
        }:
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"workspace_guard", "workspace_rules.workspace_guard"}:
                    return True
    return False


def has_workspace_guard_usage(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "WorkspaceGuard":
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr == "WorkspaceGuard":
                return True
    return False


def main() -> int:
    violations: list[str] = []

    for script in sorted(SCRIPTS_DIR.rglob("*.py")):
        if script.name in EXEMPT:
            continue
        GUARD.assert_read_path(script)
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        if not has_workspace_guard_import(tree):
            violations.append(f"{script.relative_to(ROOT)}: missing import from workspace_guard")
        if not has_workspace_guard_usage(tree):
            violations.append(f"{script.relative_to(ROOT)}: missing WorkspaceGuard usage")

    if violations:
        print("SCRIPT_COMPLIANCE_FAILED")
        for item in violations:
            print(item)
        return 1

    print("SCRIPT_COMPLIANCE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
