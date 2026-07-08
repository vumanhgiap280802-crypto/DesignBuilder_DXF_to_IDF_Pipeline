#!/usr/bin/env python3
"""
Scaffold a new project-scoped DXF case in the compact case registry.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils.common import workspace_path  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
CASES_REGISTRY_PATH = ROOT / "2_config" / "cases.json"
DEFAULT_PROJECT_FILE = ROOT / "2_config" / "default_project.json"
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ScaffoldContext:
    project_id: str
    case_name: str
    file_slug: str
    source_cad_filename: str
    ready_text_filename: str
    zone_output_prefix: str
    object_output_prefix: str
    ceiling_height_m: float
    set_default: bool
    dry_run: bool


def validate_project_id(project_id: str) -> str:
    normalized = str(project_id or "").strip()
    if not PROJECT_ID_RE.fullmatch(normalized):
        raise WorkspaceRuleError(
            "project_id must be lower_snake_case and start with a letter, for example: sample_case"
        )
    return normalized


def title_case_from_project_id(project_id: str) -> str:
    return " ".join(part.capitalize() for part in project_id.split("_"))


def file_slug_from_project_id(project_id: str) -> str:
    return "_".join(part.capitalize() for part in project_id.split("_"))


def zone_prefix_from_project_id(project_id: str) -> str:
    return f"{project_id.upper()}_"


def normalize_filename(value: str, *, fallback_stem: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        normalized = f"{fallback_stem}.dxf"
    if Path(normalized).name != normalized:
        raise WorkspaceRuleError(f"Filename must not contain directory segments: {value}")
    return normalized


def build_context(args: argparse.Namespace) -> ScaffoldContext:
    project_id = validate_project_id(args.project_id)
    case_name = str(args.case_name or "").strip() or title_case_from_project_id(project_id)
    file_slug = str(args.file_slug or "").strip() or file_slug_from_project_id(project_id)
    source_cad_filename = normalize_filename(
        args.source_cad_filename,
        fallback_stem=project_id,
    )
    ready_text_filename = normalize_filename(
        args.ready_text_filename,
        fallback_stem=project_id,
    )
    zone_output_prefix = "" if args.zone_output_prefix is None else str(args.zone_output_prefix).strip()
    object_output_prefix = (
        str(args.object_output_prefix or "").strip().rstrip("_")
        or zone_prefix_from_project_id(project_id).rstrip("_")
    )
    if args.ceiling_height_m is None:
        raise WorkspaceRuleError("Case scaffold requires human-provided --ceiling-height-m.")
    ceiling_height_m = float(args.ceiling_height_m)
    if ceiling_height_m <= 0.0:
        raise WorkspaceRuleError("ceiling_height_m must be greater than 0.")
    return ScaffoldContext(
        project_id=project_id,
        case_name=case_name,
        file_slug=file_slug,
        source_cad_filename=source_cad_filename,
        ready_text_filename=ready_text_filename,
        zone_output_prefix=zone_output_prefix,
        object_output_prefix=object_output_prefix,
        ceiling_height_m=ceiling_height_m,
        set_default=bool(args.set_default),
        dry_run=bool(args.dry_run),
    )


def case_input_root(project_id: str) -> Path:
    return ROOT / "1_input" / project_id


def assert_case_targets_available(project_id: str) -> None:
    occupied_paths: list[Path] = []
    input_root = case_input_root(project_id)
    if input_root.exists():
        occupied_paths.append(input_root)

    registry = read_cases_registry()
    cases = registry.get("cases", {})
    if isinstance(cases, dict) and project_id in cases:
        occupied_paths.append(CASES_REGISTRY_PATH)

    if occupied_paths:
        formatted = ", ".join(workspace_path(path) for path in occupied_paths)
        raise WorkspaceRuleError(f"Case scaffold target already exists for '{project_id}': {formatted}")


def ensure_case_input_dirs(project_id: str, *, dry_run: bool) -> list[Path]:
    input_root = case_input_root(project_id)
    targets = [
        input_root / "raw" / "cad",
        input_root / "clean" / "txt_dxf",
    ]
    for target in targets:
        try:
            target.resolve().relative_to(input_root.resolve())
        except ValueError as exc:
            raise WorkspaceRuleError(f"Input scaffold path escaped project scope: {target}") from exc
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
    return targets


def read_cases_registry() -> dict[str, object]:
    if not CASES_REGISTRY_PATH.exists():
        return {
            "schema_version": 1,
            "description": "Compact case registry. Shared defaults live in 2_config/case_defaults.json.",
            "cases": {},
        }
    resolved = GUARD.assert_read_path(CASES_REGISTRY_PATH)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"Cases registry must be a JSON object: {workspace_path(resolved)}")
    cases = payload.get("cases")
    if not isinstance(cases, dict):
        raise WorkspaceRuleError(f"Cases registry must contain a cases object: {workspace_path(resolved)}")
    return payload


def build_case_registry_entry(context: ScaffoldContext) -> dict[str, object]:
    entry: dict[str, object] = {
        "case_id": context.project_id,
        "case_name": context.case_name,
        "file_slug": context.file_slug,
        "object_output_prefix": context.object_output_prefix,
        "geometry_policy": {
            "policy_name": f"{context.case_name} Geometry Policy",
            "ceiling_height_m": round(context.ceiling_height_m, 3),
            "zone_name_aliases": {},
            "zone_merge_groups": [],
            "remembered_zone_targets": {},
        },
    }
    if context.source_cad_filename == context.ready_text_filename:
        entry["dxf_filename"] = context.ready_text_filename
    else:
        entry["raw_cad_filename"] = context.source_cad_filename
        entry["ready_text_filename"] = context.ready_text_filename
    if context.zone_output_prefix:
        entry["zone_output_prefix"] = context.zone_output_prefix
    return entry


def write_case_registry_entry(context: ScaffoldContext) -> Path:
    registry = read_cases_registry()
    cases = registry["cases"]
    if not isinstance(cases, dict):
        raise WorkspaceRuleError("Cases registry must contain a cases object.")
    if context.project_id in cases:
        raise WorkspaceRuleError(f"Case already exists in registry: {context.project_id}")
    cases[context.project_id] = build_case_registry_entry(context)
    if not context.dry_run:
        GUARD.write_json(
            CASES_REGISTRY_PATH,
            registry,
            allowed_roots=["2_config"],
            allow_create=True,
            allow_overwrite=True,
        )
    return CASES_REGISTRY_PATH


def write_default_project(project_id: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    GUARD.write_json(
        DEFAULT_PROJECT_FILE,
        {"default_project": project_id},
        allowed_roots=["2_config"],
        allow_create=True,
        allow_overwrite=True,
    )


def scaffold_case(context: ScaffoldContext) -> None:
    assert_case_targets_available(context.project_id)
    registry_path = write_case_registry_entry(context)

    created_dirs = ensure_case_input_dirs(context.project_id, dry_run=context.dry_run)

    if context.set_default:
        write_default_project(context.project_id, dry_run=context.dry_run)

    status = "SCAFFOLD_CASE_DRY_RUN" if context.dry_run else "SCAFFOLD_CASE_COMPLETE"
    print(status)
    print(f"Project ID: {context.project_id}")
    print(f"Case name: {context.case_name}")
    print(f"File slug: {context.file_slug}")
    print(f"Source CAD filename: {context.source_cad_filename}")
    print(f"Ready DXF text filename: {context.ready_text_filename}")
    print(f"Zone output prefix: {context.zone_output_prefix}")
    print(f"Object output prefix: {context.object_output_prefix}")
    print(f"Ceiling height: {context.ceiling_height_m:.3f} m")
    print(f"Config registry: {workspace_path(registry_path)}")
    for path in created_dirs:
        print(f"Input dir: {workspace_path(path)}")
    if context.set_default:
        print(f"Default project file: {workspace_path(DEFAULT_PROJECT_FILE)}")
    print("Next command: python 3_scripts/pipeline/run_case_pipeline.py --project " + context.project_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scaffold a new DXF case in 2_config/cases.json.")
    parser.add_argument("--project-id", required=True, help="Lower snake case project id, for example: sample_case")
    parser.add_argument("--case-name", default=None, help="Optional human-readable case name")
    parser.add_argument("--file-slug", default=None, help="Optional output slug, for example: Sample_Case")
    parser.add_argument(
        "--source-cad-filename",
        default=None,
        help="Optional raw CAD filename. Default: <project_id>.dxf",
    )
    parser.add_argument(
        "--ready-text-filename",
        default=None,
        help="Optional parser-readable DXF filename. Default: <project_id>.dxf",
    )
    parser.add_argument(
        "--zone-output-prefix",
        default=None,
        help="Optional zone output prefix. Default: empty compact names such as PN01.",
    )
    parser.add_argument(
        "--object-output-prefix",
        default=None,
        help="Optional non-zone object prefix. Default: <PROJECT_ID> without trailing underscore.",
    )
    parser.add_argument(
        "--ceiling-height-m",
        type=float,
        required=True,
        help="Human-provided ceiling height in meters written to the case geometry policy.",
    )
    parser.add_argument(
        "--set-default",
        action="store_true",
        help="Also update 2_config/default_project.json to the new project_id",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview generated paths and config values without writing files",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    context = build_context(args)
    scaffold_case(context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
