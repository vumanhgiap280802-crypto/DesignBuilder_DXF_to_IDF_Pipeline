#!/usr/bin/env python3
"""
Migrate legacy type-based input/output folders into the project-based layout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import path_resolver  # noqa: E402
from utils.common import workspace_path  # noqa: E402
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
INPUT_ROOT = ROOT / "1_input"
OUTPUT_ROOT = ROOT / "5_output"
LEGACY_TXT_DXF_DIR = "txt (dxf)"


@dataclass(frozen=True)
class MigrationOperation:
    scope: str
    source: Path
    destination: Path
    reason: str


def load_project_case_config(project_id: str) -> dict[str, Any]:
    case_config_path = ROOT / "2_config" / "projects" / project_id / "pipeline_case.json"
    if not case_config_path.exists():
        return {}
    try:
        payload = json.loads(case_config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceRuleError(f"Invalid pipeline_case.json for project '{project_id}': {case_config_path}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"pipeline_case.json must contain a JSON object: {case_config_path}")
    return payload


def iter_string_paths(payload: Any, *, prefixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, str):
            normalized = value.replace("\\", "/").strip()
            if normalized.startswith(prefixes):
                found.append(ROOT / normalized)

    visit(payload)
    return found


def normalize_input_destination(project_id: str, source: Path) -> Path | None:
    source = source.resolve()
    raw_cad_root = INPUT_ROOT / "raw" / "cad" / project_id
    raw_txt_dxf_root = INPUT_ROOT / "raw" / LEGACY_TXT_DXF_DIR / project_id
    clean_txt_dxf_review_root = INPUT_ROOT / "clean" / LEGACY_TXT_DXF_DIR / "review" / project_id
    clean_txt_dxf_ready_root = INPUT_ROOT / "clean" / LEGACY_TXT_DXF_DIR / "ready" / project_id
    if source == raw_cad_root or source.is_relative_to(raw_cad_root):
        relative = source.relative_to(raw_cad_root)
        return path_resolver.get_input_dir(project_id, "raw", "cad") / relative
    if source == raw_txt_dxf_root or source.is_relative_to(raw_txt_dxf_root):
        relative = source.relative_to(raw_txt_dxf_root)
        return path_resolver.get_input_dir(project_id, "raw", "txt_dxf") / relative
    if source == clean_txt_dxf_review_root or source.is_relative_to(clean_txt_dxf_review_root):
        relative = source.relative_to(clean_txt_dxf_review_root)
        return path_resolver.get_input_dir(project_id, "clean", "txt_dxf") / relative
    if source == clean_txt_dxf_ready_root or source.is_relative_to(clean_txt_dxf_ready_root):
        relative = source.relative_to(clean_txt_dxf_ready_root)
        return path_resolver.get_input_dir(project_id, "clean", "txt_dxf") / relative
    if source == INPUT_ROOT / "raw" / "idf" or source.is_relative_to(INPUT_ROOT / "raw" / "idf"):
        if source.is_dir():
            return path_resolver.get_input_dir(project_id, "raw", "idf")
        return path_resolver.get_input_dir(project_id, "raw", "idf") / source.name
    if source == INPUT_ROOT / "clean" / "idf" or source.is_relative_to(INPUT_ROOT / "clean" / "idf"):
        if source.is_dir():
            return path_resolver.get_input_dir(project_id, "clean", "idf")
        return path_resolver.get_input_dir(project_id, "clean", "idf") / source.name
    if source == INPUT_ROOT / "clean" / "csv" or source.is_relative_to(INPUT_ROOT / "clean" / "csv"):
        if source.is_dir():
            return path_resolver.get_input_dir(project_id, "clean", "csv")
        return path_resolver.get_input_dir(project_id, "clean", "csv") / source.name
    if source == INPUT_ROOT / "raw" / LEGACY_TXT_DXF_DIR or source.is_relative_to(INPUT_ROOT / "raw" / LEGACY_TXT_DXF_DIR):
        if source.is_dir():
            return path_resolver.get_input_dir(project_id, "raw", "txt_dxf")
        return path_resolver.get_input_dir(project_id, "raw", "txt_dxf") / source.name
    if source == INPUT_ROOT / "clean" / LEGACY_TXT_DXF_DIR or source.is_relative_to(INPUT_ROOT / "clean" / LEGACY_TXT_DXF_DIR):
        if source.is_dir():
            return path_resolver.get_input_dir(project_id, "clean", "txt_dxf")
        return path_resolver.get_input_dir(project_id, "clean", "txt_dxf") / source.name
    return None


def normalize_output_destination(project_id: str, source: Path) -> Path | None:
    source = source.resolve()
    if source == OUTPUT_ROOT / "projects" / project_id or source.is_relative_to(OUTPUT_ROOT / "projects" / project_id):
        relative = source.relative_to(OUTPUT_ROOT / "projects" / project_id)
        return path_resolver.get_output_project_root(project_id) / relative
    if source == OUTPUT_ROOT / "normalized" or source.is_relative_to(OUTPUT_ROOT / "normalized"):
        relative = source.relative_to(OUTPUT_ROOT / "normalized")
        return path_resolver.get_output_project_root(project_id) / "normalized" / relative
    if source == OUTPUT_ROOT / "intermediate" or source.is_relative_to(OUTPUT_ROOT / "intermediate"):
        relative = source.relative_to(OUTPUT_ROOT / "intermediate")
        return path_resolver.get_output_project_root(project_id) / "intermediate" / relative
    if source == OUTPUT_ROOT / "csv" or source.is_relative_to(OUTPUT_ROOT / "csv"):
        relative = source.relative_to(OUTPUT_ROOT / "csv")
        return path_resolver.get_output_project_root(project_id) / "csv" / relative
    if source == OUTPUT_ROOT / "idf" or source.is_relative_to(OUTPUT_ROOT / "idf"):
        relative = source.relative_to(OUTPUT_ROOT / "idf")
        return path_resolver.get_output_project_root(project_id) / "idf" / relative
    if source == OUTPUT_ROOT / "reports" or source.is_relative_to(OUTPUT_ROOT / "reports"):
        relative = source.relative_to(OUTPUT_ROOT / "reports")
        return path_resolver.get_output_project_root(project_id) / "reports" / relative
    if source == OUTPUT_ROOT / "packages" or source.is_relative_to(OUTPUT_ROOT / "packages"):
        relative = source.relative_to(OUTPUT_ROOT / "packages")
        return path_resolver.get_output_project_root(project_id) / "packages" / relative
    return None


def collect_project_migration_plan(project_id: str) -> dict[str, Any]:
    case_config = load_project_case_config(project_id)
    referenced_input_paths = iter_string_paths(case_config, prefixes=("1_input/",))
    referenced_output_paths = iter_string_paths(case_config, prefixes=("5_output/",))
    input_project_root = path_resolver.get_input_project_root(project_id)
    output_project_root = path_resolver.get_output_project_root(project_id)

    candidate_inputs: set[Path] = set(referenced_input_paths)
    candidate_outputs: set[Path] = set(referenced_output_paths)

    legacy_project_input_roots = [
        INPUT_ROOT / "raw" / "cad" / project_id,
        INPUT_ROOT / "raw" / LEGACY_TXT_DXF_DIR / project_id,
        INPUT_ROOT / "clean" / LEGACY_TXT_DXF_DIR / "review" / project_id,
        INPUT_ROOT / "clean" / LEGACY_TXT_DXF_DIR / "ready" / project_id,
    ]
    legacy_project_output_root = OUTPUT_ROOT / "projects" / project_id

    for path in legacy_project_input_roots:
        if path.exists():
            candidate_inputs.add(path)
    if legacy_project_output_root.exists():
        candidate_outputs.add(legacy_project_output_root)

    operations: dict[tuple[str, str], MigrationOperation] = {}
    manual_review: list[dict[str, str]] = []

    for source in sorted(candidate_inputs):
        if not source.exists():
            continue
        if source == input_project_root or source.is_relative_to(input_project_root):
            continue
        destination = normalize_input_destination(project_id, source)
        if destination is None:
            manual_review.append(
                {
                    "scope": "input",
                    "path": workspace_path(source),
                    "reason": "legacy input path could not be mapped deterministically",
                }
            )
            continue
        key = (str(source.resolve()), str(destination.resolve()))
        operations[key] = MigrationOperation(
            scope="input",
            source=source,
            destination=destination,
            reason="config_reference_or_legacy_project_root",
        )

    for source in sorted(candidate_outputs):
        if not source.exists():
            continue
        if source == output_project_root or source.is_relative_to(output_project_root):
            continue
        destination = normalize_output_destination(project_id, source)
        if destination is None:
            manual_review.append(
                {
                    "scope": "output",
                    "path": workspace_path(source),
                    "reason": "legacy output path could not be mapped deterministically",
                }
            )
            continue
        key = (str(source.resolve()), str(destination.resolve()))
        operations[key] = MigrationOperation(
            scope="output",
            source=source,
            destination=destination,
            reason="config_reference_or_legacy_project_root",
        )

    review_roots = [
        INPUT_ROOT / "clean" / "csv",
        INPUT_ROOT / "raw" / "idf",
        OUTPUT_ROOT / "reports",
        OUTPUT_ROOT / "packages",
    ]
    for review_root in review_roots:
        if not review_root.exists():
            continue
        for item in sorted(review_root.iterdir()):
            manual_review.append(
                {
                    "scope": "review",
                    "path": workspace_path(item),
                    "reason": "shared legacy artifact requires manual review before project mapping",
                }
            )

    operations_payload = [
        {
            "scope": operation.scope,
            "source": workspace_path(operation.source),
            "destination": workspace_path(operation.destination),
            "reason": operation.reason,
            "source_type": "directory" if operation.source.is_dir() else "file",
        }
        for operation in sorted(operations.values(), key=lambda item: (item.scope, workspace_path(item.source)))
    ]
    return {
        "project_id": project_id,
        "operations": operations_payload,
        "manual_review": manual_review,
        "case_config_present": bool(case_config),
        "summary": {
            "operation_count": len(operations_payload),
            "manual_review_count": len(manual_review),
        },
    }


def copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def move_path(source: Path, destination: Path) -> None:
    copy_path(source, destination)
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()


def execute_migration(plan: dict[str, Any], *, mode: str, dry_run: bool) -> None:
    for operation in plan["operations"]:
        source = ROOT / operation["source"]
        destination = ROOT / operation["destination"]
        if dry_run:
            continue
        if mode == "copy":
            copy_path(source, destination)
        else:
            move_path(source, destination)


def write_review_report(plan: dict[str, Any], *, mode: str, dry_run: bool) -> Path:
    project_id = str(plan["project_id"])
    report_path = path_resolver.resolve_output_file(project_id, "reports", "migration_review.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **plan,
        "mode": mode,
        "dry_run": dry_run,
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate legacy type-based workspace folders into project-based layout.")
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID to migrate. If omitted, reads 2_config/default_project.json.",
    )
    parser.add_argument(
        "--mode",
        choices=("copy", "move"),
        default="copy",
        help="How to apply the migration after planning. Default: copy",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the migration and write the review report without copying or moving files.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)

    plan = collect_project_migration_plan(project_id)
    execute_migration(plan, mode=args.mode, dry_run=args.dry_run)
    report_path = write_review_report(plan, mode=args.mode, dry_run=args.dry_run)

    print("PROJECT_LAYOUT_MIGRATION_COMPLETE")
    print(f"Project: {project_id}")
    print(f"Mode: {args.mode}")
    print(f"Dry run: {args.dry_run}")
    print(f"Operations: {plan['summary']['operation_count']}")
    print(f"Manual review items: {plan['summary']['manual_review_count']}")
    print(f"Review report: {workspace_path(report_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
