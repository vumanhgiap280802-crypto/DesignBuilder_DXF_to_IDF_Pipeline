#!/usr/bin/env python3
"""
Config-driven pipeline runner for multi-case execution.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.apartment_a_pipeline import (
    DEFAULT_DXF_LAYER_PROFILE_PATH,
    run_pipeline,
)
from utils import path_resolver
from utils.common import designbuilder_adiabatic_construction_name
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
CASE_DEFAULTS_PATH = ROOT / "2_config" / "case_defaults.json"
CASES_REGISTRY_PATH = ROOT / "2_config" / "cases.json"


def load_json_object(path: Path | str, *, label: str) -> dict[str, Any]:
    resolved = GUARD.assert_read_path(path)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"{label} must be a JSON object: {resolved}")
    return payload


def get_path_value(payload: dict[str, Any], key: str, default: Path | str | None) -> Path | str | None:
    value = payload.get(key, default)
    if value in {None, ""}:
        return None
    return str(value)


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def require_dict(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceRuleError(f"{label} must be a JSON object.")
    return dict(value)


def compact_case_from_registry(project_id: str) -> dict[str, Any]:
    if not CASES_REGISTRY_PATH.exists():
        raise WorkspaceRuleError(f"Cases registry is required: {CASES_REGISTRY_PATH.relative_to(ROOT)}")
    registry = load_json_object(CASES_REGISTRY_PATH, label="Cases registry")
    cases = require_dict(registry.get("cases", {}), label="cases")
    case_payload = cases.get(project_id)
    if case_payload is None:
        raise WorkspaceRuleError(
            f"Project '{project_id}' must be defined in {CASES_REGISTRY_PATH.relative_to(ROOT)}."
        )
    return require_dict(case_payload, label=f"cases.{project_id}")


def build_case_paths(*, project_id: str, file_slug: str, ready_text_filename: str) -> dict[str, str]:
    return {
        "case_output_root": f"5_output/{project_id}",
        "dxf_input": f"1_input/{project_id}/clean/txt_dxf/{ready_text_filename}",
        "dxf_normalized_output_dir": f"5_output/{project_id}/normalized/dxf",
        "extract_output": f"5_output/{project_id}/normalized/dxf/{file_slug}_filtered_extract.txt",
        "schema_output": f"5_output/{project_id}/normalized/dxf/{file_slug}_filtered_extract_schema.json",
        "mapping_output_dir": f"5_output/{project_id}/intermediate/mapping",
        "geometry_output_dir": f"5_output/{project_id}/intermediate/geometry",
        "surface_output_dir": f"5_output/{project_id}/intermediate/surfaces",
        "wall_output_dir": f"5_output/{project_id}/intermediate/walls",
        "fenestration_output_dir": f"5_output/{project_id}/intermediate/fenestration",
        "bundle_output_dir": f"5_output/{project_id}/csv/{file_slug}_idf_input_bundle",
        "rebuilt_idf_output": f"5_output/{project_id}/idf/{file_slug}_generated_from_bundle.idf",
        "reports_output_dir": f"5_output/{project_id}/reports",
    }


def build_compact_case_config(
    project_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    case_payload = compact_case_from_registry(project_id)

    defaults = load_json_object(CASE_DEFAULTS_PATH, label="Case defaults")
    default_config_files = require_dict(defaults.get("config_files", {}), label="case_defaults.config_files")
    default_options = require_dict(defaults.get("options", {}), label="case_defaults.options")
    default_intake = require_dict(defaults.get("intake", {}), label="case_defaults.intake")
    default_naming = require_dict(defaults.get("naming", {}), label="case_defaults.naming")
    default_geometry_policy = require_dict(
        defaults.get("geometry_policy", {}),
        label="case_defaults.geometry_policy",
    )

    case_id = str(case_payload.get("case_id", project_id) or project_id).strip()
    geometry_project_id = str(case_payload.get("geometry_project_id", project_id) or project_id).strip()
    case_name = str(case_payload.get("case_name", project_id) or project_id).strip()
    file_slug = str(case_payload.get("file_slug", project_id) or project_id).strip()
    dxf_filename = str(case_payload.get("dxf_filename", f"{file_slug}.dxf") or f"{file_slug}.dxf").strip()
    raw_cad_filename = str(case_payload.get("raw_cad_filename", dxf_filename) or dxf_filename).strip()
    ready_text_filename = str(case_payload.get("ready_text_filename", dxf_filename) or dxf_filename).strip()
    object_output_prefix = str(case_payload.get("object_output_prefix", geometry_project_id.upper()) or "").strip()

    if not all([case_id, geometry_project_id, case_name, file_slug, raw_cad_filename, ready_text_filename]):
        raise WorkspaceRuleError(f"Compact case '{project_id}' is missing required identity or filename values.")

    naming_override = require_dict(case_payload.get("naming", {}), label=f"cases.{project_id}.naming")
    naming_override = deep_merge_dict(
        naming_override,
        {
            "case_id": geometry_project_id,
            "case_name": case_name,
            "file_slug": file_slug,
            "object_output_prefix": object_output_prefix,
            "primary_input_patterns": {
                "raw_cad_file": raw_cad_filename,
                "ready_text_input": ready_text_filename,
            },
        },
    )
    if "zone_output_prefix" in case_payload:
        naming_override["zone_output_prefix"] = str(case_payload.get("zone_output_prefix", ""))
    naming_rules = deep_merge_dict(default_naming, naming_override)

    geometry_policy_override = require_dict(
        case_payload.get("geometry_policy", {}),
        label=f"cases.{project_id}.geometry_policy",
    )
    if "ceiling_height_m" in case_payload:
        geometry_policy_override["ceiling_height_m"] = case_payload["ceiling_height_m"]
    geometry_policy = deep_merge_dict(default_geometry_policy, geometry_policy_override)

    config_files = dict(default_config_files)
    options = dict(default_options)
    case_config = {
        "case_id": case_id,
        "geometry_project_id": geometry_project_id,
        "model_id": str(case_payload.get("model_id", "") or "").strip(),
        "case_name": case_name,
        "config_source": str(CASES_REGISTRY_PATH.relative_to(ROOT)).replace("\\", "/"),
        "config_files": config_files,
        "options": options,
        "intake": {
            "parser_input_standard": str(default_intake.get("parser_input_standard", "parser_readable_dxf_text")),
            "raw_cad_dir": f"1_input/{geometry_project_id}/raw/cad",
            "ready_text_dir": f"1_input/{geometry_project_id}/clean/txt_dxf",
            "source_cad_file": f"1_input/{geometry_project_id}/raw/cad/{raw_cad_filename}",
            "source_ready_text_file": f"1_input/{geometry_project_id}/clean/txt_dxf/{ready_text_filename}",
        },
        "paths": build_case_paths(
            project_id=geometry_project_id,
            file_slug=file_slug,
            ready_text_filename=ready_text_filename,
        ),
    }
    return case_config, naming_rules, geometry_policy, CASES_REGISTRY_PATH


def write_resolved_geometry_policy(project_id: str, geometry_policy: dict[str, Any]) -> Path:
    output_path = path_resolver.resolve_output_file(
        project_id,
        "intermediate/config",
        "geometry_policy_resolved.json",
    )
    GUARD.assert_write_path(output_path, allowed_roots=["5_output"], allow_create=True, allow_overwrite=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(geometry_policy, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return output_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def collect_adiabatic_wall_targets(bundle_output_dir: Path) -> list[dict[str, str]]:
    surface_csv = bundle_output_dir / "BuildingSurface_Detailed.csv"
    if not surface_csv.is_file():
        return []
    targets = []
    for row in read_csv_dicts(surface_csv):
        if row.get("surface_type") != "Wall":
            continue
        if row.get("outside_boundary_condition") != "Adiabatic":
            continue
        source_construction = row.get("construction_name", "")
        targets.append(
            {
                "surface_name": row.get("surface_name", ""),
                "zone_name": row.get("zone_name", ""),
                "surface_type": row.get("surface_type", ""),
                "outside_boundary_condition": row.get("outside_boundary_condition", ""),
                "source_construction": source_construction,
                "target_construction": designbuilder_adiabatic_construction_name(source_construction),
            }
        )
    return targets


def write_idf_handoff_manifest(
    *,
    case_config: dict[str, Any],
    case_config_path: Path,
    result: dict[str, Any],
    case_id: str,
    geometry_project_id: str,
) -> Path:
    paths = case_config.get("paths", {})
    reports_output_dir = GUARD.resolve(
        get_path_value(paths, "reports_output_dir", None)
        or path_resolver.resolve_output_file(geometry_project_id, "reports")
    )
    path_resolver.assert_output_in_project_scope(geometry_project_id, reports_output_dir)
    GUARD.assert_write_path(reports_output_dir, allowed_roots=["5_output"], allow_create=True)
    reports_output_dir.mkdir(parents=True, exist_ok=True)

    idf_output_path = GUARD.assert_read_path(result["rebuilt_idf_output_path"])
    bundle_output_dir = GUARD.assert_read_path(result["bundle_output_dir"])
    adiabatic_wall_targets = collect_adiabatic_wall_targets(bundle_output_dir)
    target_constructions = sorted({row["target_construction"] for row in adiabatic_wall_targets if row["target_construction"]})
    manifest = {
        "manifest_type": "idf_handoff",
        "manifest_version": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "case_id": case_id,
        "geometry_project_id": geometry_project_id,
        "model_id": str(case_config.get("model_id", "")).strip(),
        "case_name": str(case_config.get("case_name", "")).strip(),
        "case_config_path": str(case_config_path.relative_to(ROOT)).replace("\\", "/"),
        "idf_output_path": str(idf_output_path.relative_to(ROOT)).replace("\\", "/"),
        "idf_sha256": sha256_file(idf_output_path),
        "bundle_output_dir": str(bundle_output_dir.relative_to(ROOT)).replace("\\", "/"),
        "adiabatic_wall_count": len(adiabatic_wall_targets),
        "adiabatic_target_constructions": target_constructions,
        "adiabatic_wall_targets": adiabatic_wall_targets,
        "qa_status": "NOT_RUN_BY_PIPELINE",
        "notes": [
            "This manifest is generated by DesignBuilder_DXF_to_IDF_Pipeline after rebuilding the IDF.",
            "Script workspace should copy this manifest and the IDF into the active run generated folder before DesignBuilder import/simulation.",
        ],
    }
    output_path = reports_output_dir / "idf_handoff_manifest.json"
    GUARD.assert_write_path(output_path, allowed_roots=["5_output"], allow_create=True, allow_overwrite=True)
    output_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a configured case pipeline.")
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID to run from 2_config/cases.json. If omitted, reads 2_config/default_project.json.",
    )
    parser.add_argument(
        "--ceiling-height-m",
        type=float,
        default=None,
        help="Optional zone/model ceiling height in meters. Defaults to 2_config/cases.json or case defaults.",
    )
    args = parser.parse_args()

    resolved_project_id = path_resolver.resolve_project_id(args.project)
    case_config, naming_rules, geometry_policy_payload, case_config_path = build_compact_case_config(resolved_project_id)

    case_id = str(case_config.get("case_id", "")).strip()
    if not case_id:
        raise WorkspaceRuleError(f"Case config is missing case_id: {case_config_path}")
    geometry_project_id = str(case_config.get("geometry_project_id", "") or resolved_project_id or case_id).strip()
    if not geometry_project_id:
        raise WorkspaceRuleError(f"Case config is missing geometry_project_id: {case_config_path}")

    paths = case_config.get("paths", {})
    if not isinstance(paths, dict):
        raise WorkspaceRuleError(f"Case config paths must be an object: {case_config_path}")

    options = case_config.get("options", {})
    if not isinstance(options, dict):
        raise WorkspaceRuleError(f"Case config options must be an object: {case_config_path}")

    config_files = case_config.get("config_files", {})
    if not isinstance(config_files, dict):
        raise WorkspaceRuleError(f"Case config config_files must be an object: {case_config_path}")

    layer_profile_path = get_path_value(config_files, "dxf_layer_profile", DEFAULT_DXF_LAYER_PROFILE_PATH)

    ceiling_height_m = args.ceiling_height_m
    if ceiling_height_m is None:
        policy_height = geometry_policy_payload.get("ceiling_height_m")
        ceiling_height_m = float(policy_height) if policy_height not in {None, ""} else None
    if ceiling_height_m is None or ceiling_height_m <= 0.0:
        raise WorkspaceRuleError(
            "A positive ceiling height is required. Pass --ceiling-height-m or set geometry_policy.ceiling_height_m."
        )

    geometry_policy_payload["ceiling_height_m"] = round(float(ceiling_height_m), 3)
    geometry_policy_path = write_resolved_geometry_policy(geometry_project_id, geometry_policy_payload)
    config_files["geometry_policy"] = str(geometry_policy_path.relative_to(ROOT)).replace("\\", "/")
    config_files["case_defaults"] = str(CASE_DEFAULTS_PATH.relative_to(ROOT)).replace("\\", "/")
    config_files["cases"] = str(CASES_REGISTRY_PATH.relative_to(ROOT)).replace("\\", "/")

    room_pattern_texts = list(naming_rules.get("room_anchor_patterns", []))
    title_pattern_texts = list(naming_rules.get("title_anchor_patterns", []))
    zone_output_prefix = str(naming_rules.get("zone_output_prefix", ""))
    object_output_prefix = str(naming_rules.get("object_output_prefix", "") or "").strip().rstrip("_")

    result = run_pipeline(
        project_id=geometry_project_id,
        input_path=get_path_value(paths, "dxf_input", None),
        output_path=get_path_value(paths, "extract_output", None),
        schema_output_path=get_path_value(paths, "schema_output", None),
        mapping_output_path=get_path_value(paths, "mapping_output", None),
        mapping_output_dir=get_path_value(paths, "mapping_output_dir", None),
        idf_bundle_output_dir=get_path_value(paths, "bundle_output_dir", None),
        rebuilt_idf_output_path=get_path_value(paths, "rebuilt_idf_output", None),
        padding=float(options.get("padding", 2500.0) or 2500.0),
        room_pattern_texts=room_pattern_texts,
        title_pattern_texts=title_pattern_texts,
        geometry_policy_path=geometry_policy_path,
        ceiling_height_m=float(ceiling_height_m),
        layer_profile_path=layer_profile_path or DEFAULT_DXF_LAYER_PROFILE_PATH,
        zone_output_prefix=zone_output_prefix,
        object_output_prefix=object_output_prefix,
        dxf_normalized_output_dir=get_path_value(paths, "dxf_normalized_output_dir", None),
        geometry_output_dir=get_path_value(paths, "geometry_output_dir", None),
        surface_output_dir=get_path_value(paths, "surface_output_dir", None),
        wall_output_dir=get_path_value(paths, "wall_output_dir", None),
        fenestration_output_dir=get_path_value(paths, "fenestration_output_dir", None),
    )
    manifest_path = write_idf_handoff_manifest(
        case_config=case_config,
        case_config_path=case_config_path,
        result=result,
        case_id=case_id,
        geometry_project_id=geometry_project_id,
    )

    print("CASE_PIPELINE_COMPLETE")
    print(f"Case: {case_id}")
    print(f"Geometry project: {geometry_project_id}")
    print(f"Case config: {case_config_path.relative_to(ROOT)}")
    print(f"Extract output: {Path(result['extract_output_path']).relative_to(ROOT)}")
    if result.get("bundle_output_dir") is not None:
        print(f"Bundle output: {Path(result['bundle_output_dir']).relative_to(ROOT)}")
    if result.get("rebuilt_idf_output_path") is not None:
        print(f"Rebuilt IDF: {Path(result['rebuilt_idf_output_path']).relative_to(ROOT)}")
    print(f"Handoff manifest: {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
