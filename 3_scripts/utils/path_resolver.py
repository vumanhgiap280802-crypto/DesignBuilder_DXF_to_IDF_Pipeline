#!/usr/bin/env python3
"""
Project-scoped path helpers used across the DXF -> IDF pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
INPUT_ROOT = ROOT / "1_input"
OUTPUT_ROOT = ROOT / "5_output"
DEFAULT_PROJECT_PATH = ROOT / "2_config" / "default_project.json"
GLOBAL_OUTPUT_CATEGORIES = {
    "normalized",
    "intermediate",
    "csv",
    "idf",
    "reports",
    "packages",
    "projects",
    "_shared",
}


def _normalize_project_id(project_id: str | None) -> str:
    return str(project_id or "").strip()


def resolve_project_id(project_id: str | None = None) -> str:
    normalized_project_id = _normalize_project_id(project_id)
    if normalized_project_id:
        return normalized_project_id
    if not DEFAULT_PROJECT_PATH.exists():
        raise WorkspaceRuleError(f"Missing default project file: {DEFAULT_PROJECT_PATH}")
    payload = json.loads(GUARD.assert_read_path(DEFAULT_PROJECT_PATH).read_text(encoding="utf-8"))
    normalized_project_id = str(payload.get("default_project", "")).strip()
    if not normalized_project_id:
        raise WorkspaceRuleError(f"default_project is blank in {DEFAULT_PROJECT_PATH}")
    return normalized_project_id


def get_input_project_root(project_id: str | None) -> Path:
    return INPUT_ROOT / resolve_project_id(project_id)


def get_output_project_root(project_id: str | None) -> Path:
    return OUTPUT_ROOT / resolve_project_id(project_id)


def get_input_dir(project_id: str | None, stage: str, data_kind: str) -> Path:
    return get_input_project_root(project_id) / str(stage) / str(data_kind)


def _legacy_input_dir(stage: str, data_kind: str) -> Path:
    return INPUT_ROOT / str(stage) / str(data_kind)


def _legacy_output_dir(project_id: str | None, category: str) -> Path:
    return OUTPUT_ROOT / "projects" / resolve_project_id(project_id) / Path(category)


def _contains_glob(pattern: str) -> bool:
    return any(token in pattern for token in ("*", "?", "["))


def _first_existing_glob_match(base_dir: Path, pattern: str) -> Path | None:
    matches = sorted(base_dir.glob(pattern))
    for match in matches:
        if match.exists():
            return match
    return None


def resolve_input_file(
    project_id: str | None,
    stage: str,
    data_kind: str,
    pattern: str,
) -> Path | None:
    project_dir = get_input_dir(project_id, stage, data_kind)
    if project_dir.exists():
        project_match = _first_existing_glob_match(project_dir, pattern)
        if project_match is not None:
            return project_match

    legacy_dir = _legacy_input_dir(stage, data_kind)
    if legacy_dir.exists():
        legacy_match = _first_existing_glob_match(legacy_dir, pattern)
        if legacy_match is not None:
            return legacy_match
    return None


def resolve_project_dxf_text_input(project_id: str | None) -> Path | None:
    resolved_project_id = resolve_project_id(project_id)
    return (
        resolve_input_file(resolved_project_id, "clean", "txt_dxf", "*.txt")
        or resolve_input_file(resolved_project_id, "clean", "txt_dxf", "*.dxf")
        or resolve_input_file(resolved_project_id, "raw", "txt_dxf", "*.txt")
        or resolve_input_file(resolved_project_id, "raw", "txt_dxf", "*.dxf")
    )


def resolve_output_file(project_id: str | None, category: str, *parts: str) -> Path:
    base_dir = get_output_project_root(project_id) / Path(category)
    return base_dir.joinpath(*parts) if parts else base_dir


def resolve_output_dir_for_read(project_id: str | None, category: str) -> Path | None:
    resolved_project_id = resolve_project_id(project_id)
    candidates = [
        get_output_project_root(resolved_project_id) / Path(category),
        _legacy_output_dir(resolved_project_id, category),
        OUTPUT_ROOT / Path(category),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_output_file_for_read(
    project_id: str | None,
    category: str,
    filename: str,
) -> Path | None:
    output_dir = resolve_output_dir_for_read(project_id, category)
    if output_dir is None:
        return None
    if _contains_glob(filename):
        return _first_existing_glob_match(output_dir, filename)
    candidate = output_dir / filename
    if candidate.exists():
        return candidate
    return None


def assert_output_in_project_scope(project_id: str | None, path: str | Path) -> Path:
    resolved_path = Path(path).resolve()
    project_root = get_output_project_root(project_id).resolve()
    try:
        resolved_path.relative_to(project_root)
    except ValueError as exc:
        raise WorkspaceRuleError(
            f"Output path must stay inside project scope {project_root}: {resolved_path}"
        ) from exc
    return resolved_path
