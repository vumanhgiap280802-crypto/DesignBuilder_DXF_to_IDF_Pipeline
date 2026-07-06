#!/usr/bin/env python3
"""
Scaffold a new project-scoped DXF case from the shared template.
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
TEMPLATE_ROOT = ROOT / "2_config" / "projects" / "_template_dxf_case"
DEFAULT_PROJECT_FILE = ROOT / "2_config" / "default_project.json"
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TEMPLATE_FILENAMES = (
    "pipeline_case.template.json",
    "naming_rules.template.json",
    "geometry_policy.template.json",
)


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


def assert_template_root() -> Path:
    template_root = GUARD.assert_read_path(TEMPLATE_ROOT)
    for filename in TEMPLATE_FILENAMES:
        GUARD.assert_read_path(template_root / filename)
    return template_root


def case_config_root(project_id: str) -> Path:
    return ROOT / "2_config" / "projects" / project_id


def case_input_root(project_id: str) -> Path:
    return ROOT / "1_input" / project_id


def assert_case_targets_available(project_id: str) -> None:
    occupied_paths: list[Path] = []
    for candidate in (case_config_root(project_id), case_input_root(project_id)):
        if candidate.exists():
            occupied_paths.append(candidate)
    if occupied_paths:
        formatted = ", ".join(workspace_path(path) for path in occupied_paths)
        raise WorkspaceRuleError(f"Case scaffold target already exists for '{project_id}': {formatted}")


def make_token_map(context: ScaffoldContext) -> dict[str, str]:
    return {
        "__PROJECT_ID__": context.project_id,
        "__CASE_NAME__": context.case_name,
        "__FILE_SLUG__": context.file_slug,
        "__SOURCE_CAD_FILENAME__": context.source_cad_filename,
        "__READY_TEXT_FILENAME__": context.ready_text_filename,
        "__ZONE_OUTPUT_PREFIX__": context.zone_output_prefix,
        "__OBJECT_OUTPUT_PREFIX__": context.object_output_prefix,
        "__CEILING_HEIGHT_M__": f"{context.ceiling_height_m:.3f}",
    }


def render_template(path: Path, replacements: dict[str, str]) -> str:
    rendered = GUARD.assert_read_path(path).read_text(encoding="utf-8")
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


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


def write_case_config(path: Path, content: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    GUARD.write_text(
        path,
        content,
        allowed_roots=["2_config"],
        allow_create=True,
        allow_overwrite=False,
    )


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
    template_root = assert_template_root()
    assert_case_targets_available(context.project_id)
    replacements = make_token_map(context)

    written_files: list[Path] = []
    for template_filename in TEMPLATE_FILENAMES:
        template_path = template_root / template_filename
        target_filename = template_filename.replace(".template", "")
        target_path = case_config_root(context.project_id) / target_filename
        write_case_config(target_path, render_template(template_path, replacements), dry_run=context.dry_run)
        written_files.append(target_path)

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
    for path in written_files:
        print(f"Config: {workspace_path(path)}")
    for path in created_dirs:
        print(f"Input dir: {workspace_path(path)}")
    if context.set_default:
        print(f"Default project file: {workspace_path(DEFAULT_PROJECT_FILE)}")
    print("Next command: python 3_scripts/pipeline/run_case_pipeline.py --project " + context.project_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scaffold a new DXF case from the shared template.")
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
