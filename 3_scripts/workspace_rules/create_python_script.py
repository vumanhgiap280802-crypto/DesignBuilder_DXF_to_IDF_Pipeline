#!/usr/bin/env python3
"""
Create a new Python script in 3_scripts from the workspace template.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
TARGET_ROOT = ROOT / "3_scripts"


def build_template(script_name: str) -> str:
    return f'''#!/usr/bin/env python3
"""
{script_name}

Workspace rule bootstrap must stay above business logic.
"""

from __future__ import annotations

from workspace_rules.workspace_guard import WorkspaceGuard


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root


def main() -> int:
    # Add business logic only after the workspace rule bootstrap above.
    # Example:
    # input_path = GUARD.assert_read_path("1_input/my_project/raw/idf/example.idf")
    # output_path = GUARD.assert_write_path(
    #     "5_output/my_project/reports/example.json",
    #     allowed_roots=["5_output"],
    #     allow_create=True,
    # )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Python script template with workspace guard.")
    parser.add_argument("name", help="Script filename, for example my_script.py")
    parser.add_argument(
        "--subdir",
        default="",
        help="Optional subdirectory inside 3_scripts, for example parsers, context, transformers, or pipeline.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite the target if it exists.")
    args = parser.parse_args()

    filename = args.name.strip()
    if not filename.endswith(".py"):
        raise WorkspaceRuleError("Script name must end with .py")

    subdir = args.subdir.strip().strip("/\\")
    target_dir = (TARGET_ROOT / subdir).resolve() if subdir else TARGET_ROOT.resolve()
    try:
        target_dir.relative_to(TARGET_ROOT.resolve())
    except ValueError as exc:
        raise WorkspaceRuleError("Script subdir must stay inside 3_scripts") from exc

    target = target_dir / filename
    payload = build_template(filename)
    GUARD.write_text(
        target,
        payload,
        allowed_roots=["3_scripts"],
        allow_create=True,
        allow_overwrite=args.force,
    )
    print(f"PYTHON_SCRIPT_CREATED {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
