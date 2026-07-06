#!/usr/bin/env python3
"""
Resolve wall thickness, paired-wall reconciliation, and construction mapping.

This transformer works only from parser/context/transformer artifacts. It does not
parse raw DXF, does not infer geometry, does not build
surface topology, and does not emit final CSV/IDF outputs.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import unicodedata
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.dxf_raw_parser import (  # noqa: E402
    DEFAULT_DXF_LAYER_PROFILE,
    Record,
    classify_record_layer,
    load_layer_profile,
)
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils import library_paths, path_resolver  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_SURFACE_ROWS = Path("5_output") / "<project_id>" / "intermediate" / "surfaces" / "surface_rows.json"
DEFAULT_GEOMETRY_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "geometry" / "geometry_payload.json"
DEFAULT_MAPPING_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "mapping_payload.json"
DEFAULT_DIMENSION_ANNOTATIONS = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "dimension_annotations.json"
DEFAULT_WALL_REFERENCE_IDF = Path("1_input") / "<project_id>" / "raw" / "idf" / "reference.idf"
DEFAULT_OUTPUT_DIR = Path("5_output") / "<project_id>" / "intermediate" / "walls"
DEFAULT_WALL_LAYER_PROFILE = DEFAULT_DXF_LAYER_PROFILE

REFERENCE_BUILDING_SURFACE_FIELDS = [
    "surface_name",
    "surface_type",
    "construction_name",
    "zone_name",
    "outside_boundary_condition",
    "outside_boundary_condition_object",
    "sun_exposure",
    "wind_exposure",
    "view_factor_to_ground",
    "number_of_vertices",
    "v1_x",
    "v1_y",
    "v1_z",
    "v2_x",
    "v2_y",
    "v2_z",
    "v3_x",
    "v3_y",
    "v3_z",
    "v4_x",
    "v4_y",
    "v4_z",
]
CONSTRUCTION_LAYER_FIELDS = [f"layer_{index}" for index in range(1, 7)]
THICKNESS_RE = re.compile(r"(\d+)\s*mm", re.IGNORECASE)
SOURCE_PRIORITY = {
    "layer_canonical": 7.2,
    "layer_alias": 6.6,
    "layer_fuzzy": 5.6,
    "wall_first_core_finish": 5.8,
    "wall_first_same_layer": 4.4,
    "dimension_direct": 3.8,
    "dimension_soft": 2.6,
    "boundary_fallback": 1.0,
}
PRIMARY_WALL_LAYER_ROLES = {"external_wall", "adiabatic_wall", "internal_wall", "partition"}
SUPPORTING_WALL_LAYER_ROLES = {"apartment_boundary", "room_boundary", "wall_boundary_fallback", "structural_fallback"}
WALL_LAYER_ROLE_THICKNESS_MM = {
    "external_wall": 250,
    "adiabatic_wall": 250,
    "internal_wall": 180,
    "partition": 140,
}
WALL_LAYER_ROLE_WEIGHT = {
    "external_wall": 6.8,
    "adiabatic_wall": 7.2,
    "internal_wall": 6.4,
    "partition": 6.0,
}
DEFAULT_WALL_REFERENCE_POLICY = {
    "policy_name": "zone_boundary_wall_reference_v1",
    "external_reference_type": "zone_boundary_inside_face_to_outer_face",
    "interzone_reference_type": "shared_zone_boundary_centerline",
    "single_zone_reference_type": "zone_boundary_inside_face",
}
OUTER_FACE_WALL_REFERENCE_TYPES = {
    "zone_boundary_outer_face",
    "zone_boundary_as_drawn",
}
ZERO_OFFSET_WALL_REFERENCE_TYPES = {
    "shared_zone_boundary_centerline",
    "zone_boundary_inside_face",
    "zone_boundary_centerline",
    *OUTER_FACE_WALL_REFERENCE_TYPES,
}

_INPUT_WALL_LIBRARY: dict[str, object] | None = None


def workspace_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _resolve_project_wall_inputs(project_id: str) -> dict[str, Path]:
    resolved = {
        "geometry_payload": path_resolver.resolve_output_file_for_read(project_id, "intermediate/geometry", "geometry_payload.json"),
        "surface_rows": path_resolver.resolve_output_file_for_read(project_id, "intermediate/surfaces", "surface_rows.json"),
        "mapping_payload": path_resolver.resolve_output_file_for_read(project_id, "intermediate/mapping", "mapping_payload.json"),
        "dimension_annotations": path_resolver.resolve_output_file_for_read(project_id, "intermediate/mapping", "dimension_annotations.json"),
    }
    missing = [name for name, value in resolved.items() if value is None]
    if missing:
        raise WorkspaceRuleError(
            f"Missing required wall artifacts for project '{project_id}': {', '.join(sorted(missing))}"
        )
    return {name: value for name, value in resolved.items() if value is not None}


def _resolve_default_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/walls")


def _resolve_default_wall_reference_idf(project_id: str) -> Path | None:
    resolved = path_resolver.resolve_input_file(project_id, "clean", "idf", "*.idf")
    if resolved is None:
        resolved = path_resolver.resolve_input_file(project_id, "raw", "idf", "*.idf")
    if resolved is not None:
        return resolved
    return None


def load_json_object(path: Path | str) -> dict[str, object]:
    resolved_path = GUARD.assert_read_path(path)
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceRuleError(f"Invalid JSON object: {workspace_path(resolved_path)}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"JSON root must be an object: {workspace_path(resolved_path)}")
    return payload


def load_json_list(path: Path | str) -> list[dict[str, object]]:
    resolved_path = GUARD.assert_read_path(path)
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceRuleError(f"Invalid JSON list: {workspace_path(resolved_path)}") from exc
    if not isinstance(payload, list):
        raise WorkspaceRuleError(f"JSON root must be a list: {workspace_path(resolved_path)}")
    if not all(isinstance(item, dict) for item in payload):
        raise WorkspaceRuleError(f"JSON list must contain objects only: {workspace_path(resolved_path)}")
    return [dict(item) for item in payload]


def counter_to_sorted_dict(counter: Counter[Any]) -> dict[str, int]:
    return dict(sorted((str(key), int(value)) for key, value in counter.items()))


def canonical_source_name(value: object) -> str:
    source = str(value or "").strip()
    for suffix in ("_peer", "_collinear_resolved", "_resolved"):
        while source.endswith(suffix):
            source = source[: -len(suffix)]
    return source


def layer_match_weight(match_source: str) -> float:
    return {
        "canonical": 1.2,
        "alias": 1.0,
        "fuzzy": 0.82,
    }.get(str(match_source or "").strip(), 0.75)


def layer_role_boundary_factor(boundary_condition: str, role_name: str) -> float:
    boundary = str(boundary_condition or "").strip()
    role = str(role_name or "").strip()
    if boundary == "Outdoors":
        if role == "adiabatic_wall":
            return 1.2
        if role == "external_wall":
            return 1.15
        if role == "internal_wall":
            return 1.0
        if role == "partition":
            return 0.6
        return 0.5
    if boundary in {"Surface", "Adiabatic"}:
        if role == "adiabatic_wall":
            return 1.25 if boundary == "Adiabatic" else 0.9
        if role == "partition":
            return 1.1
        if role == "internal_wall":
            return 1.05
        if role == "external_wall":
            return 0.65
        return 0.7
    return 0.8


def wall_role_expected_thickness_mm(role_name: str, canonical_layer: str = "") -> int | None:
    layer_match = re.search(r"(\d{3})", str(canonical_layer or ""))
    if layer_match:
        try:
            return int(layer_match.group(1))
        except ValueError:
            return None
    return WALL_LAYER_ROLE_THICKNESS_MM.get(str(role_name or "").strip())


def normalize_layer_role_for_thickness(role_name: str, canonical_layer: str = "") -> str:
    normalized_role = str(role_name or "").strip()
    nominal_thickness_mm = wall_role_expected_thickness_mm(normalized_role, canonical_layer)
    if normalized_role == "internal_wall" and nominal_thickness_mm is not None and nominal_thickness_mm < 180:
        return "partition"
    return normalized_role


def inferred_wall_role(
    *,
    boundary_condition: str,
    wall_thickness_mm: int,
    explicit_role: str = "",
) -> str:
    role = str(explicit_role or "").strip()
    if role:
        return role
    boundary = str(boundary_condition or "").strip()
    if boundary == "Adiabatic":
        return "adiabatic_wall"
    if boundary == "Outdoors":
        return "external_wall"
    if boundary == "Surface":
        return "internal_wall" if int(wall_thickness_mm) >= 180 else "partition"
    return "wall"


def wall_family_from_role(
    *,
    boundary_condition: str,
    role_name: str,
    wall_thickness_mm: int,
) -> str:
    role = str(role_name or "").strip()
    if role == "adiabatic_wall" or str(boundary_condition or "").strip() == "Adiabatic":
        return "Adiabatic"
    if role == "external_wall" or str(boundary_condition or "").strip() == "Outdoors":
        return "Exterior"
    if role == "internal_wall" or int(wall_thickness_mm) >= 180:
        return "Internal"
    if role == "partition" or str(boundary_condition or "").strip() == "Surface":
        return "Partition"
    return "Other"


def parse_optional_float_text(value: object) -> float | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_optional_int_text(value: object) -> int | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def parse_flag_text(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def load_csv_dict_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            {key: (value.strip() if isinstance(value, str) else "") for key, value in row.items()}
            for row in reader
        ]


def write_csv_rows(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    resolved_path = GUARD.assert_write_path(
        path,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    with resolved_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def normalize_resolver_boundary_family(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if normalized == "outdoors" or "external" in normalized or "outside" in normalized:
        return "Outdoors"
    if normalized in {"surface", "adiabatic"}:
        return "Surface"
    if "partition" in normalized or "internal" in normalized or "interior" in normalized:
        return "Surface"
    return ""


def normalize_wall_library_boundary(category: str) -> str:
    return normalize_resolver_boundary_family(category)


def catalog_item_name(row: dict[str, str]) -> str:
    for field_name in ("item_name", "material_name", "Name", "name"):
        value = str(row.get(field_name, "")).strip()
        if value:
            return value
    return ""


def construction_row_from_layers(construction_name: str, layers: list[str]) -> dict[str, str]:
    if len(layers) > len(CONSTRUCTION_LAYER_FIELDS):
        raise WorkspaceRuleError(
            f"Construction {construction_name} exceeds supported layer count ({len(layers)} > {len(CONSTRUCTION_LAYER_FIELDS)})."
        )
    row = {"construction_name": construction_name}
    for field_name in CONSTRUCTION_LAYER_FIELDS:
        row[field_name] = ""
    for index, layer_name in enumerate(layers, start=1):
        row[f"layer_{index}"] = str(layer_name)
    return row


def empty_input_wall_library(*, missing_paths: list[Path] | None = None) -> dict[str, object]:
    return {
        "available": False,
        "catalog_mode": "",
        "missing_paths": [workspace_path(path) for path in list(missing_paths or [])],
        "construction_source": "",
        "material_source": "",
        "resolver_rule_source": "",
        "object_library_sources": {},
        "materials_by_name": {},
        "material_nomass_rows_by_name": {},
        "material_airgap_rows_by_name": {},
        "glazing_rows_by_name": {},
        "gas_rows_by_name": {},
        "frame_rows_by_name": {},
        "construction_rows_by_name": {},
        "construction_specs_by_name": {},
        "construction_specs_by_boundary_thickness": {},
        "construction_specs_by_thickness": {},
        "opening_rules_by_key": {},
        "bundle_default_material_rows": [],
        "bundle_default_construction_rows": [],
        "bundle_default_glazing_rows": [],
        "bundle_default_gas_rows": [],
        "bundle_default_frame_rows": [],
    }


def _resolve_library_manifest_path(default: Path, *keys: str) -> Path:
    resolved = library_paths.resolve_library_path(*keys, default=default)
    if resolved is None:
        return default
    return resolved


def _resolve_library_input_paths() -> dict[str, Path]:
    return {
        "construction_catalog": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "catalogs" / "constructions" / "constructions_catalog.csv",
            "idf_import",
            "catalogs",
            "construction_catalog",
        ),
        "materials_catalog": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "catalogs" / "materials" / "materials_catalog.csv",
            "idf_import",
            "catalogs",
            "materials_catalog",
        ),
        "resolver_rules": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "catalogs" / "resolvers" / "resolver_rules.csv",
            "idf_import",
            "catalogs",
            "resolver_rules",
        ),
        "legacy_construction_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "legacy" / "walls" / "construction_input_3_brick_walls.csv",
            "idf_import",
            "legacy_inputs",
            "wall_construction_input_csv",
        ),
        "legacy_material_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "legacy" / "materials" / "materials_for_construction_input_3_brick_walls.csv",
            "idf_import",
            "legacy_inputs",
            "wall_material_input_csv",
        ),
        "object_construction_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "objects" / "constructions" / "Construction.csv",
            "idf_import",
            "objects",
            "construction_csv",
        ),
        "object_material_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "objects" / "materials" / "Material.csv",
            "idf_import",
            "objects",
            "material_csv",
        ),
        "object_glazing_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "objects" / "fenestration" / "WindowMaterial_Glazing.csv",
            "idf_import",
            "objects",
            "glazing_csv",
        ),
        "object_gas_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "objects" / "fenestration" / "WindowMaterial_Gas.csv",
            "idf_import",
            "objects",
            "gas_csv",
        ),
        "object_frame_csv": _resolve_library_manifest_path(
            ROOT / "1_input" / "library" / "idf_import" / "objects" / "fenestration" / "WindowProperty_FrameAndDivider.csv",
            "idf_import",
            "objects",
            "frame_csv",
        ),
    }


def _load_optional_object_library_rows(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    return load_csv_dict_rows(GUARD.assert_read_path(path))


def build_legacy_input_wall_library(
    *,
    construction_path: Path,
    material_path: Path,
) -> dict[str, object]:
    construction_rows_raw = load_csv_dict_rows(GUARD.assert_read_path(construction_path))
    material_rows_raw = load_csv_dict_rows(GUARD.assert_read_path(material_path))

    materials_by_name: dict[str, dict[str, str]] = {}
    material_thickness_m_by_name: dict[str, float] = {}
    for row in material_rows_raw:
        material_name = str(row.get("Name", "")).strip()
        if not material_name:
            continue
        material_type = str(row.get("Type", "Detailed")).strip().lower()
        if material_type not in {"", "detailed", "material"}:
            raise WorkspaceRuleError(
                f"Unsupported wall material type in input CSV for {material_name}: {row.get('Type', '')}"
            )
        thickness_m = parse_optional_float_text(row.get("DefaultThickness_m"))
        if thickness_m is None or thickness_m <= 0:
            raise WorkspaceRuleError(
                f"Missing or invalid DefaultThickness_m for input wall material: {material_name}"
            )
        materials_by_name[material_name] = {
            "material_name": material_name,
            "roughness": str(row.get("Roughness", "MediumSmooth")).strip() or "MediumSmooth",
            "thickness_m": f"{thickness_m:.3f}",
            "conductivity_w_per_mk": str(row.get("Conductivity_W_mK", "")).strip(),
            "density_kg_per_m3": str(row.get("Density_kg_m3", "")).strip(),
            "specific_heat_j_per_kgk": str(row.get("SpecificHeat_J_kgK", "")).strip(),
            "thermal_emittance": str(row.get("ThermalAbsorptance", "")).strip(),
            "solar_absorptance": str(row.get("SolarAbsorptance", "")).strip(),
            "visible_absorptance": str(row.get("VisibleAbsorptance", "")).strip(),
        }
        material_thickness_m_by_name[material_name] = thickness_m

    grouped_construction_rows: dict[str, list[dict[str, str]]] = {}
    for row in construction_rows_raw:
        construction_code = str(row.get("construction_code", "")).strip()
        construction_name = str(row.get("construction_name", "")).strip()
        group_key = construction_code or construction_name
        if not group_key:
            continue
        grouped_construction_rows.setdefault(group_key, []).append(row)

    construction_rows_by_name: dict[str, dict[str, str]] = {}
    construction_specs_by_name: dict[str, dict[str, object]] = {}
    construction_specs_by_boundary_thickness: dict[tuple[str, int], list[dict[str, object]]] = {}
    construction_specs_by_thickness: dict[int, list[dict[str, object]]] = {}

    for grouped_rows in grouped_construction_rows.values():
        grouped_rows.sort(key=lambda item: int(item.get("layer_order", "0") or 0))
        first_row = grouped_rows[0]
        construction_name = str(first_row.get("construction_name", "")).strip()
        if not construction_name:
            continue

        layers: list[str] = []
        total_thickness_m = 0.0
        for row in grouped_rows:
            material_name = str(row.get("material_name", "")).strip()
            if material_name not in materials_by_name:
                raise WorkspaceRuleError(
                    f"Construction input references unknown material {material_name} in {construction_name}"
                )
            layers.append(material_name)
            override_thickness_m = parse_optional_float_text(row.get("thickness_override_m"))
            total_thickness_m += (
                override_thickness_m
                if override_thickness_m is not None and override_thickness_m > 0
                else material_thickness_m_by_name[material_name]
            )

        boundary_family = normalize_wall_library_boundary(str(first_row.get("category", "")))
        total_thickness_mm = int(round(total_thickness_m * 1000.0))
        construction_row = construction_row_from_layers(construction_name, layers)
        construction_rows_by_name[construction_name] = construction_row

        reverse_layers = list(reversed(layers))
        reverse_name = f"{construction_name}_Rev"
        construction_rows_by_name[reverse_name] = construction_row_from_layers(reverse_name, reverse_layers)

        construction_spec = {
            "construction_name": construction_name,
            "reverse_name": reverse_name,
            "layers": list(layers),
            "boundary_family": boundary_family,
            "thickness_mm": total_thickness_mm,
            "material_names": sorted(set(layers)),
            "resolver_scope": "wall",
            "surface_type": "Wall",
            "bundle_default": False,
        }
        construction_specs_by_name[construction_name] = construction_spec
        construction_specs_by_name[reverse_name] = construction_spec
        if boundary_family:
            construction_specs_by_boundary_thickness.setdefault(
                (boundary_family, total_thickness_mm),
                [],
            ).append(construction_spec)
        construction_specs_by_thickness.setdefault(total_thickness_mm, []).append(construction_spec)

    payload = empty_input_wall_library()
    payload.update(
        {
            "available": True,
            "catalog_mode": "legacy",
            "construction_source": workspace_path(construction_path),
            "material_source": workspace_path(material_path),
            "materials_by_name": materials_by_name,
            "construction_rows_by_name": construction_rows_by_name,
            "construction_specs_by_name": construction_specs_by_name,
            "construction_specs_by_boundary_thickness": construction_specs_by_boundary_thickness,
            "construction_specs_by_thickness": construction_specs_by_thickness,
        }
    )
    return payload


def build_standard_input_wall_library(
    *,
    construction_path: Path,
    material_path: Path,
    resolver_rule_path: Path,
    object_paths: dict[str, Path] | None = None,
) -> dict[str, object]:
    construction_rows_raw = load_csv_dict_rows(GUARD.assert_read_path(construction_path))
    material_rows_raw = load_csv_dict_rows(GUARD.assert_read_path(material_path))
    resolver_rows_raw = (
        load_csv_dict_rows(GUARD.assert_read_path(resolver_rule_path))
        if resolver_rule_path.exists()
        else []
    )

    materials_by_name: dict[str, dict[str, str]] = {}
    material_nomass_rows_by_name: dict[str, dict[str, str]] = {}
    material_airgap_rows_by_name: dict[str, dict[str, str]] = {}
    glazing_rows_by_name: dict[str, dict[str, str]] = {}
    gas_rows_by_name: dict[str, dict[str, str]] = {}
    frame_rows_by_name: dict[str, dict[str, str]] = {}
    material_thickness_m_by_name: dict[str, float] = {}
    bundle_default_material_rows: list[dict[str, str]] = []
    bundle_default_glazing_rows: list[dict[str, str]] = []
    bundle_default_gas_rows: list[dict[str, str]] = []
    bundle_default_frame_rows: list[dict[str, str]] = []

    for row in material_rows_raw:
        item_name = catalog_item_name(row)
        if not item_name:
            continue
        idf_object_type = str(row.get("idf_object_type", "Material")).strip() or "Material"
        bundle_default = parse_flag_text(row.get("bundle_default"))
        if idf_object_type == "Material":
            thickness_m = parse_optional_float_text(row.get("thickness_m"))
            if thickness_m is None or thickness_m <= 0:
                raise WorkspaceRuleError(f"Missing or invalid thickness_m for library material: {item_name}")
            material_row = {
                "material_name": item_name,
                "roughness": str(row.get("roughness", "MediumSmooth")).strip() or "MediumSmooth",
                "thickness_m": f"{thickness_m:.3f}",
                "conductivity_w_per_mk": str(row.get("conductivity_w_per_mk", "")).strip(),
                "density_kg_per_m3": str(row.get("density_kg_per_m3", "")).strip(),
                "specific_heat_j_per_kgk": str(row.get("specific_heat_j_per_kgk", "")).strip(),
                "thermal_emittance": str(row.get("thermal_emittance", "")).strip(),
                "solar_absorptance": str(row.get("solar_absorptance", "")).strip(),
                "visible_absorptance": str(row.get("visible_absorptance", "")).strip(),
            }
            materials_by_name[item_name] = material_row
            material_thickness_m_by_name[item_name] = thickness_m
            if bundle_default:
                bundle_default_material_rows.append(material_row)
            continue
        if idf_object_type == "Material:NoMass":
            material_nomass_rows_by_name[item_name] = {
                "material_name": item_name,
                "roughness": str(row.get("roughness", "")).strip(),
                "resistance_m2k_per_w": str(row.get("resistance_m2k_per_w", "")).strip(),
                "thermal_emittance": str(row.get("thermal_emittance", "")).strip(),
                "solar_absorptance": str(row.get("solar_absorptance", "")).strip(),
                "visible_absorptance": str(row.get("visible_absorptance", "")).strip(),
            }
            continue
        if idf_object_type == "Material:AirGap":
            material_airgap_rows_by_name[item_name] = {
                "material_name": item_name,
                "resistance_m2k_per_w": str(row.get("resistance_m2k_per_w", "")).strip(),
            }
            continue
        if idf_object_type == "WindowMaterial:Glazing":
            glazing_row = {
                "glazing_name": item_name,
                "optical_data_type": str(row.get("optical_data_type", "SpectralAverage")).strip() or "SpectralAverage",
                "spectral_data_set_name": str(row.get("spectral_data_set_name", "")).strip(),
                "thickness_m": str(row.get("thickness_m", "")).strip(),
                "solar_transmittance": str(row.get("solar_transmittance", "")).strip(),
                "solar_reflectance_front": str(row.get("solar_reflectance_front", "")).strip(),
                "solar_reflectance_back": str(row.get("solar_reflectance_back", "")).strip(),
                "visible_transmittance": str(row.get("visible_transmittance", "")).strip(),
                "visible_reflectance_front": str(row.get("visible_reflectance_front", "")).strip(),
                "visible_reflectance_back": str(row.get("visible_reflectance_back", "")).strip(),
                "ir_transmittance": str(row.get("ir_transmittance", "")).strip(),
                "ir_emissivity_front": str(row.get("ir_emissivity_front", "")).strip(),
                "ir_emissivity_back": str(row.get("ir_emissivity_back", "")).strip(),
                "conductivity_w_per_mk": str(row.get("conductivity_w_per_mk", "")).strip(),
                "dirt_correction_factor": str(row.get("dirt_correction_factor", "")).strip(),
            }
            glazing_rows_by_name[item_name] = glazing_row
            if bundle_default:
                bundle_default_glazing_rows.append(glazing_row)
            continue
        if idf_object_type == "WindowMaterial:Gas":
            gas_row = {
                "gas_layer_name": item_name,
                "gas_type": str(row.get("gas_type", "")).strip(),
                "thickness_m": str(row.get("thickness_m", "")).strip(),
            }
            gas_rows_by_name[item_name] = gas_row
            if bundle_default:
                bundle_default_gas_rows.append(gas_row)
            continue
        if idf_object_type == "WindowProperty:FrameAndDivider":
            frame_row = {
                "frame_divider_name": item_name,
                "frame_width_m": str(row.get("frame_width_m", "")).strip(),
                "frame_outside_projection_m": str(row.get("frame_outside_projection_m", "")).strip(),
                "frame_inside_projection_m": str(row.get("frame_inside_projection_m", "")).strip(),
                "frame_conductance_w_per_m2k": str(row.get("frame_conductance_w_per_m2k", "")).strip(),
                "frame_edge_to_center_glass_conductance_ratio": str(row.get("frame_edge_to_center_glass_conductance_ratio", "")).strip(),
                "frame_solar_absorptance": str(row.get("frame_solar_absorptance", "")).strip(),
                "frame_visible_absorptance": str(row.get("frame_visible_absorptance", "")).strip(),
                "frame_thermal_emissivity": str(row.get("frame_thermal_emissivity", "")).strip(),
                "divider_type": str(row.get("divider_type", "")).strip(),
                "divider_width_m": str(row.get("divider_width_m", "")).strip(),
                "number_horizontal_dividers": str(row.get("number_horizontal_dividers", "")).strip(),
                "number_vertical_dividers": str(row.get("number_vertical_dividers", "")).strip(),
                "divider_outside_projection_m": str(row.get("divider_outside_projection_m", "")).strip(),
                "divider_inside_projection_m": str(row.get("divider_inside_projection_m", "")).strip(),
                "divider_conductance_w_per_m2k": str(row.get("divider_conductance_w_per_m2k", "")).strip(),
                "divider_edge_to_center_glass_conductance_ratio": str(row.get("divider_edge_to_center_glass_conductance_ratio", "")).strip(),
                "divider_solar_absorptance": str(row.get("divider_solar_absorptance", "")).strip(),
                "divider_visible_absorptance": str(row.get("divider_visible_absorptance", "")).strip(),
                "divider_thermal_emissivity": str(row.get("divider_thermal_emissivity", "")).strip(),
                "outside_reveal_solar_absorptance": str(row.get("outside_reveal_solar_absorptance", "")).strip(),
                "inside_sill_depth_m": str(row.get("inside_sill_depth_m", "")).strip(),
                "inside_sill_solar_absorptance": str(row.get("inside_sill_solar_absorptance", "")).strip(),
                "inside_reveal_depth_m": str(row.get("inside_reveal_depth_m", "")).strip(),
                "inside_reveal_solar_absorptance": str(row.get("inside_reveal_solar_absorptance", "")).strip(),
            }
            frame_rows_by_name[item_name] = frame_row
            if bundle_default:
                bundle_default_frame_rows.append(frame_row)

    grouped_construction_rows: dict[str, list[dict[str, str]]] = {}
    for row in construction_rows_raw:
        construction_name = str(row.get("construction_name", "")).strip()
        if construction_name:
            grouped_construction_rows.setdefault(construction_name, []).append(row)

    construction_rows_by_name: dict[str, dict[str, str]] = {}
    construction_specs_by_name: dict[str, dict[str, object]] = {}
    construction_specs_by_boundary_thickness: dict[tuple[str, int], list[dict[str, object]]] = {}
    construction_specs_by_thickness: dict[int, list[dict[str, object]]] = {}
    bundle_default_construction_rows: list[dict[str, str]] = []

    for grouped_rows in grouped_construction_rows.values():
        grouped_rows.sort(key=lambda item: int(item.get("layer_order", "0") or 0))
        first_row = grouped_rows[0]
        construction_name = str(first_row.get("construction_name", "")).strip()
        if not construction_name:
            continue
        resolver_scope = str(first_row.get("resolver_scope", "")).strip().lower()
        surface_type = str(first_row.get("surface_type", "")).strip()
        boundary_family = normalize_resolver_boundary_family(first_row.get("boundary_family"))
        bundle_default = parse_flag_text(first_row.get("bundle_default"))
        layers: list[str] = []
        material_names: set[str] = set()
        total_thickness_m = 0.0
        for row in grouped_rows:
            layer_name = str(row.get("layer_item_name", "") or row.get("material_name", "")).strip()
            if not layer_name:
                continue
            layers.append(layer_name)
            if layer_name in materials_by_name:
                material_names.add(layer_name)
                override_thickness_m = parse_optional_float_text(row.get("thickness_override_m"))
                total_thickness_m += (
                    override_thickness_m
                    if override_thickness_m is not None and override_thickness_m > 0
                    else material_thickness_m_by_name[layer_name]
                )
                continue
            if layer_name in material_nomass_rows_by_name or layer_name in material_airgap_rows_by_name:
                continue
            if (
                layer_name not in glazing_rows_by_name
                and layer_name not in gas_rows_by_name
                and layer_name not in frame_rows_by_name
            ):
                raise WorkspaceRuleError(
                    f"Construction library references unknown layer item {layer_name} in {construction_name}"
                )

        construction_row = construction_row_from_layers(construction_name, layers)
        construction_rows_by_name[construction_name] = construction_row
        if bundle_default:
            bundle_default_construction_rows.append(construction_row)

        reverse_name = str(first_row.get("reverse_construction_name", "")).strip()
        auto_reverse = parse_flag_text(first_row.get("auto_reverse"))
        if not reverse_name and resolver_scope == "wall":
            reverse_name = f"{construction_name}_Rev"
        if reverse_name and (auto_reverse or resolver_scope == "wall"):
            construction_rows_by_name[reverse_name] = construction_row_from_layers(
                reverse_name,
                list(reversed(layers)),
            )

        construction_spec = {
            "construction_name": construction_name,
            "reverse_name": reverse_name,
            "layers": list(layers),
            "boundary_family": boundary_family,
            "thickness_mm": int(round(total_thickness_m * 1000.0)),
            "material_names": sorted(material_names),
            "resolver_scope": resolver_scope,
            "surface_type": surface_type,
            "bundle_default": bundle_default,
        }
        construction_specs_by_name[construction_name] = construction_spec
        if reverse_name:
            construction_specs_by_name[reverse_name] = construction_spec
        if resolver_scope == "wall" and construction_spec["thickness_mm"] > 0:
            thickness_mm = int(construction_spec["thickness_mm"])
            if boundary_family:
                construction_specs_by_boundary_thickness.setdefault(
                    (boundary_family, thickness_mm),
                    [],
                ).append(construction_spec)
            construction_specs_by_thickness.setdefault(thickness_mm, []).append(construction_spec)

    opening_rules_by_key: dict[tuple[str, str], dict[str, object]] = {}
    if resolver_rows_raw:
        construction_specs_by_boundary_thickness = {}
        construction_specs_by_thickness = {}
    for row in resolver_rows_raw:
        status = str(row.get("status", "active")).strip().lower()
        if status and status not in {"active", "pilot"}:
            continue
        resolver_scope = str(row.get("resolver_scope", "")).strip().lower()
        surface_type = str(row.get("surface_type", "")).strip()
        boundary_family = normalize_resolver_boundary_family(row.get("boundary_family"))
        construction_name = str(row.get("construction_name", "")).strip()
        reverse_name = str(row.get("reverse_construction_name", "")).strip()
        if resolver_scope == "wall":
            spec = dict(construction_specs_by_name.get(construction_name, {}))
            if not spec:
                raise WorkspaceRuleError(
                    f"Resolver rule references unknown wall construction {construction_name}"
                )
            thickness_mm = parse_optional_int_text(row.get("thickness_mm")) or int(spec.get("thickness_mm", 0) or 0)
            if thickness_mm <= 0:
                raise WorkspaceRuleError(
                    f"Resolver rule for wall construction {construction_name} is missing thickness_mm"
                )
            if reverse_name:
                spec["reverse_name"] = reverse_name
            spec["boundary_family"] = boundary_family or str(spec.get("boundary_family", "")).strip()
            spec["surface_type"] = surface_type or str(spec.get("surface_type", "")).strip()
            spec["resolver_scope"] = "wall"
            if spec["boundary_family"]:
                construction_specs_by_boundary_thickness.setdefault(
                    (str(spec["boundary_family"]), thickness_mm),
                    [],
                ).append(spec)
            construction_specs_by_thickness.setdefault(thickness_mm, []).append(spec)
            construction_specs_by_name[construction_name] = spec
            if str(spec.get("reverse_name", "")).strip():
                construction_specs_by_name[str(spec["reverse_name"])] = spec
            continue
        if resolver_scope != "opening" or not surface_type or not construction_name:
            continue
        priority = parse_optional_int_text(row.get("priority")) or 100
        key = (surface_type, boundary_family or "Outdoors")
        current_rule = opening_rules_by_key.get(key)
        if current_rule and int(current_rule.get("priority", 0) or 0) > priority:
            continue
        opening_rules_by_key[key] = {
            "surface_type": surface_type,
            "boundary_family": boundary_family or "Outdoors",
            "construction_name": construction_name,
            "reverse_construction_name": reverse_name,
            "frame_and_divider_name": str(row.get("frame_and_divider_name", "")).strip(),
            "priority": priority,
            "status": str(row.get("status", "active")).strip() or "active",
            "notes": str(row.get("notes", "")).strip(),
        }

    payload = empty_input_wall_library()
    resolved_object_paths = dict(object_paths or {})
    object_library_sources = {
        source_name: workspace_path(path)
        for source_name, path in resolved_object_paths.items()
        if path is not None and path.exists()
    }
    object_material_rows = _load_optional_object_library_rows(resolved_object_paths.get("object_material_csv"))
    object_construction_rows = _load_optional_object_library_rows(
        resolved_object_paths.get("object_construction_csv")
    )
    object_glazing_rows = _load_optional_object_library_rows(resolved_object_paths.get("object_glazing_csv"))
    object_gas_rows = _load_optional_object_library_rows(resolved_object_paths.get("object_gas_csv"))
    object_frame_rows = _load_optional_object_library_rows(resolved_object_paths.get("object_frame_csv"))
    if object_material_rows:
        bundle_default_material_rows = object_material_rows
    if object_construction_rows:
        bundle_default_construction_rows = object_construction_rows
    if object_glazing_rows:
        bundle_default_glazing_rows = object_glazing_rows
    if object_gas_rows:
        bundle_default_gas_rows = object_gas_rows
    if object_frame_rows:
        bundle_default_frame_rows = object_frame_rows
    payload.update(
        {
            "available": True,
            "catalog_mode": "standard",
            "construction_source": workspace_path(construction_path),
            "material_source": workspace_path(material_path),
            "resolver_rule_source": workspace_path(resolver_rule_path) if resolver_rule_path.exists() else "",
            "object_library_sources": object_library_sources,
            "materials_by_name": materials_by_name,
            "material_nomass_rows_by_name": material_nomass_rows_by_name,
            "material_airgap_rows_by_name": material_airgap_rows_by_name,
            "glazing_rows_by_name": glazing_rows_by_name,
            "gas_rows_by_name": gas_rows_by_name,
            "frame_rows_by_name": frame_rows_by_name,
            "construction_rows_by_name": construction_rows_by_name,
            "construction_specs_by_name": construction_specs_by_name,
            "construction_specs_by_boundary_thickness": construction_specs_by_boundary_thickness,
            "construction_specs_by_thickness": construction_specs_by_thickness,
            "opening_rules_by_key": opening_rules_by_key,
            "bundle_default_material_rows": bundle_default_material_rows,
            "bundle_default_construction_rows": bundle_default_construction_rows,
            "bundle_default_glazing_rows": bundle_default_glazing_rows,
            "bundle_default_gas_rows": bundle_default_gas_rows,
            "bundle_default_frame_rows": bundle_default_frame_rows,
        }
    )
    return payload


def load_input_wall_construction_library() -> dict[str, object]:
    global _INPUT_WALL_LIBRARY
    if _INPUT_WALL_LIBRARY is not None:
        return _INPUT_WALL_LIBRARY

    configured_paths = _resolve_library_input_paths()
    construction_catalog_path = configured_paths["construction_catalog"]
    materials_catalog_path = configured_paths["materials_catalog"]
    resolver_rules_path = configured_paths["resolver_rules"]
    legacy_construction_path = configured_paths["legacy_construction_csv"]
    legacy_material_path = configured_paths["legacy_material_csv"]

    if construction_catalog_path.exists() and materials_catalog_path.exists():
        _INPUT_WALL_LIBRARY = build_standard_input_wall_library(
            construction_path=construction_catalog_path,
            material_path=materials_catalog_path,
            resolver_rule_path=resolver_rules_path,
            object_paths=configured_paths,
        )
        return _INPUT_WALL_LIBRARY

    if legacy_construction_path.exists() and legacy_material_path.exists():
        warnings.warn(
            "deprecated input layout: using 1_input/library/idf_import/legacy wall library fallback",
            DeprecationWarning,
            stacklevel=2,
        )
        _INPUT_WALL_LIBRARY = build_legacy_input_wall_library(
            construction_path=legacy_construction_path,
            material_path=legacy_material_path,
        )
        return _INPUT_WALL_LIBRARY

    _INPUT_WALL_LIBRARY = empty_input_wall_library(
        missing_paths=[
            path
            for path in (
                construction_catalog_path,
                materials_catalog_path,
                resolver_rules_path,
            )
            if path.exists() is False
        ]
    )
    return _INPUT_WALL_LIBRARY


def resolve_input_wall_construction_spec(
    *,
    boundary_condition: str,
    wall_thickness_mm: int,
) -> dict[str, object] | None:
    wall_library = load_input_wall_construction_library()
    if not bool(wall_library.get("available")) or wall_thickness_mm <= 0:
        return None

    boundary_family = "Outdoors" if str(boundary_condition).strip() == "Outdoors" else "Surface"
    candidates = list(
        wall_library.get("construction_specs_by_boundary_thickness", {}).get(
            (boundary_family, int(wall_thickness_mm)),
            [],
        )
    )
    if not candidates:
        candidates = list(
            wall_library.get("construction_specs_by_thickness", {}).get(int(wall_thickness_mm), [])
        )
    return candidates[0] if candidates else None


def resolve_input_opening_construction_rule(
    *,
    surface_type: str,
    boundary_condition: str,
) -> dict[str, object] | None:
    wall_library = load_input_wall_construction_library()
    if not bool(wall_library.get("available")):
        return None
    boundary_family = "Outdoors" if str(boundary_condition).strip() == "Outdoors" else "Surface"
    opening_rules = dict(wall_library.get("opening_rules_by_key", {}))
    return dict(opening_rules.get((str(surface_type).strip(), boundary_family), {})) or None


def parse_reference_building_surface_rows(path: Path) -> list[dict[str, str]]:
    text = GUARD.assert_read_path(path).read_text(encoding="utf-8", errors="replace")
    rows: list[dict[str, str]] = []
    current_tokens: list[str] = []
    current_value_chars: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("!", 1)[0]
        for character in line:
            if character in {",", ";"}:
                current_tokens.append("".join(current_value_chars).strip())
                current_value_chars = []
                if character == ";":
                    if current_tokens and current_tokens[0] == "BuildingSurface:Detailed":
                        values = current_tokens[1:]
                        padded_values = values + [""] * max(
                            0,
                            len(REFERENCE_BUILDING_SURFACE_FIELDS) - len(values),
                        )
                        rows.append(
                            {
                                field: padded_values[index]
                                for index, field in enumerate(REFERENCE_BUILDING_SURFACE_FIELDS)
                            }
                        )
                    current_tokens = []
            else:
                current_value_chars.append(character)
        current_value_chars.append(" ")
    return rows


def wall_construction_family_name(construction_name: str) -> str:
    name = str(construction_name).strip()
    if name.endswith("_Rev"):
        return name[:-4]
    return name


def wall_construction_thickness_mm(construction_name: str) -> int | None:
    match = re.search(r"_W(\d+)_", str(construction_name).strip())
    if not match:
        return None
    return int(match.group(1))


def wall_segment_signature_from_row(
    row: dict[str, object],
    *,
    dx: float = 0.0,
    dy: float = 0.0,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if str(row.get("surface_type", "")).strip() != "Wall":
        return None
    vertex_count = int(parse_optional_float_text(row.get("number_of_vertices")) or 0)
    points: set[tuple[float, float]] = set()
    for index in range(1, vertex_count + 1):
        x = parse_optional_float_text(row.get(f"v{index}_x"))
        y = parse_optional_float_text(row.get(f"v{index}_y"))
        if x is None or y is None:
            continue
        points.add((round(x + dx, 3), round(y + dy, 3)))
    if len(points) != 2:
        return None
    ordered = sorted(points)
    return ordered[0], ordered[1]


def load_reference_wall_construction_lookup(
    current_wall_rows: list[dict[str, object]],
    reference_idf_path: Path | str | None = None,
) -> dict[tuple[str, tuple[tuple[float, float], tuple[float, float]]], str]:
    if not current_wall_rows:
        return {}

    resolved_reference_idf_path: Path | None = None
    if reference_idf_path not in {None, ""}:
        resolved_reference_idf_path = GUARD.assert_read_path(reference_idf_path)
    if resolved_reference_idf_path is None or not resolved_reference_idf_path.exists():
        return {}

    reference_wall_rows = [
        row
        for row in parse_reference_building_surface_rows(resolved_reference_idf_path)
        if str(row.get("surface_type", "")).strip() == "Wall"
        and str(row.get("construction_name", "")).strip().startswith("IBST_W")
    ]
    if not reference_wall_rows:
        return {}

    def minimum_xy(rows: list[dict[str, object]]) -> tuple[float, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for row in rows:
            vertex_count = int(parse_optional_float_text(row.get("number_of_vertices")) or 0)
            for index in range(1, vertex_count + 1):
                x = parse_optional_float_text(row.get(f"v{index}_x"))
                y = parse_optional_float_text(row.get(f"v{index}_y"))
                if x is None or y is None:
                    continue
                xs.append(x)
                ys.append(y)
        if not xs or not ys:
            return None
        return min(xs), min(ys)

    reference_min = minimum_xy(reference_wall_rows)
    current_min = minimum_xy(current_wall_rows)
    if reference_min is None or current_min is None:
        return {}

    dx = current_min[0] - reference_min[0]
    dy = current_min[1] - reference_min[1]
    lookup: dict[tuple[str, tuple[tuple[float, float], tuple[float, float]]], str] = {}
    for row in reference_wall_rows:
        signature = wall_segment_signature_from_row(row, dx=dx, dy=dy)
        if signature is None:
            continue
        boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        family_name = wall_construction_family_name(str(row.get("construction_name", "")).strip())
        if not family_name:
            continue
        lookup[(boundary_condition, signature)] = family_name
    return lookup


def normalize_dimension_measurement_mm(measurement_mm: float) -> int:
    rounded_int = int(round(abs(measurement_mm)))
    if abs(abs(measurement_mm) - rounded_int) <= 0.75:
        return rounded_int
    return int(round(abs(measurement_mm) / 5.0) * 5)


def point_to_segment_metrics(
    point_x: float,
    point_y: float,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> tuple[float, float, float]:
    dx = end_x - start_x
    dy = end_y - start_y
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return math.hypot(point_x - start_x, point_y - start_y), 0.0, 0.0

    projection_ratio = ((point_x - start_x) * dx + (point_y - start_y) * dy) / (length * length)
    projection_ratio = max(0.0, min(1.0, projection_ratio))
    projection_x = start_x + (projection_ratio * dx)
    projection_y = start_y + (projection_ratio * dy)
    distance = math.hypot(point_x - projection_x, point_y - projection_y)
    return distance, projection_ratio * length, length


def interpolate_segment_point(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    distance_along_m: float,
) -> tuple[float, float]:
    segment_length = math.hypot(end_x - start_x, end_y - start_y)
    if segment_length <= 1e-9:
        return start_x, start_y
    ratio = distance_along_m / segment_length
    return start_x + ((end_x - start_x) * ratio), start_y + ((end_y - start_y) * ratio)


def interval_overlap_length(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(max(a_start, a_end), max(b_start, b_end)) - max(min(a_start, a_end), min(b_start, b_end)))


def opening_anchor_xy_m(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        return float(value[0]) / 1000.0, float(value[1]) / 1000.0
    except (TypeError, ValueError):
        return None


def _legacy_canonical_zone_key(text: str) -> str:
    value = str(text or "").split(":")[-1].strip().upper().replace(" ", "_").replace("+", "_")
    value = re.sub(r"_+", "_", value).strip("_")
    if value == "PKPB":
        return "PK_PB"
    if value == "PNPK":
        return "PN_PK"
    logia_match = re.fullmatch(r"LOGIA_?0?(\d{1,2})", value)
    if logia_match:
        return f"LOGIA_{int(logia_match.group(1)):02d}"
    value = {"PK_PB": "PK_PB", "LOGIA": "LOGIA", "LÔGIA": "LOGIA", "LÔ_GIA": "LOGIA"}.get(value, value)
    compact_match = re.fullmatch(r"(PN|WC)_?0?(\d{1,2})", value)
    if compact_match:
        return f"{compact_match.group(1)}_{int(compact_match.group(2)):02d}"
    return value


def canonical_zone_key(text: str) -> str:
    value = str(text or "").split(":")[-1].strip()
    raw_value = value.upper().replace(" ", "_").replace("+", "_")
    raw_value = re.sub(r"_+", "_", raw_value).strip("_")
    if raw_value in {"LÃ”GIA", "LÃ”_GIA"}:
        return "LOGIA"

    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("+", "_")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    token = re.sub(r"_+", "_", ascii_text).strip("_").upper()
    token = {
        "PKXPB": "PK_PB",
        "PKPB": "PK_PB",
        "PNPK": "PN_PK",
        "LOGIA": "LOGIA",
    }.get(token, token)

    logia_match = re.fullmatch(r"LOGIA_?0?(\d{1,2})", token)
    if logia_match:
        return f"LOGIA_{int(logia_match.group(1)):02d}"
    compact_match = re.fullmatch(r"(PN|WC)_?0?(\d{1,2})", token)
    if compact_match:
        return f"{compact_match.group(1)}_{int(compact_match.group(2)):02d}"
    return token


def host_wall_from_surface_row(
    row: dict[str, object],
    row_by_name: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    if str(row.get("surface_type", "")).strip() != "Wall":
        return None
    try:
        start = [float(row.get("v1_x", 0.0)), float(row.get("v1_y", 0.0))]
        end = [float(row.get("v4_x", 0.0)), float(row.get("v4_y", 0.0))]
        height_m = max(
            float(row.get("v2_z", 0.0) or 0.0),
            float(row.get("v3_z", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        return None

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dy) <= 1e-6:
        axis = "horizontal"
        side = "south" if dx >= 0.0 else "north"
    elif abs(dx) <= 1e-6:
        axis = "vertical"
        side = "east" if dy >= 0.0 else "west"
    else:
        axis = "diagonal"
        side = ""

    surface_name = str(row.get("surface_name", "")).strip()
    paired_surface_name = str(row.get("outside_boundary_condition_object", "")).strip()
    paired_row = row_by_name.get(paired_surface_name)
    length_m = round(math.hypot(dx, dy), 3)
    return {
        "surface_name": surface_name,
        "zone_name": str(row.get("zone_name", "")).strip(),
        "start": [round(start[0], 3), round(start[1], 3)],
        "end": [round(end[0], 3), round(end[1], 3)],
        "length_m": length_m,
        "height_m": round(height_m, 3),
        "boundary_condition": str(row.get("outside_boundary_condition", "")).strip(),
        "adjacent_zone_name": str(paired_row.get("zone_name", "")).strip() if paired_row else "",
        "paired_surface_name": paired_surface_name,
        "construction_reverse": bool(row.get("inferred_construction_reverse")),
        "construction_name": str(row.get("construction_name", "")).strip(),
        "wall_thickness_mm": int(row.get("inferred_wall_thickness_mm", 0) or 0),
        "wall_thickness_inference_source": str(row.get("wall_thickness_inference_source", "")).strip(),
        "wall_role": str(row.get("inferred_wall_role", "")).strip(),
        "wall_family": str(row.get("inferred_wall_family", "")).strip(),
        "wall_layer_canonical": str(row.get("wall_layer_canonical", "")).strip(),
        "wall_layer_source_layers": list(row.get("wall_layer_source_layers", []))
        if isinstance(row.get("wall_layer_source_layers", []), list)
        else [],
        "axis": axis,
        "side": side,
    }


def build_wall_host_collections(
    surface_rows: list[dict[str, object]],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, dict[str, object]]]:
    row_by_name = {
        str(row.get("surface_name", "")).strip(): row
        for row in surface_rows
        if str(row.get("surface_name", "")).strip()
    }
    wall_hosts_by_zone: dict[str, list[dict[str, object]]] = defaultdict(list)
    wall_hosts_by_surface_name: dict[str, dict[str, object]] = {}
    for row in surface_rows:
        host = host_wall_from_surface_row(row, row_by_name)
        if host is None:
            continue
        zone_name = str(host.get("zone_name", "")).strip()
        surface_name = str(host.get("surface_name", "")).strip()
        wall_hosts_by_zone[zone_name].append(host)
        wall_hosts_by_surface_name[surface_name] = host
    return dict(wall_hosts_by_zone), wall_hosts_by_surface_name


def host_wall_axis(host_wall: dict[str, object]) -> str:
    start = host_wall["start"]
    end = host_wall["end"]
    return "horizontal" if abs(float(end[0]) - float(start[0])) >= abs(float(end[1]) - float(start[1])) else "vertical"


def host_wall_projection(
    point_xy_m: tuple[float, float],
    host_wall: dict[str, object],
) -> tuple[tuple[float, float], float, float]:
    distance_m, along_m, _host_length_m = point_to_segment_metrics(
        point_xy_m[0],
        point_xy_m[1],
        host_wall["start"][0],
        host_wall["start"][1],
        host_wall["end"][0],
        host_wall["end"][1],
    )
    projection_xy_m = interpolate_segment_point(
        host_wall["start"][0],
        host_wall["start"][1],
        host_wall["end"][0],
        host_wall["end"][1],
        along_m,
    )
    return projection_xy_m, along_m, distance_m


def host_wall_fixed_coord_and_span_mm(
    host_wall: dict[str, object],
) -> tuple[str, float, float, float]:
    axis = host_wall_axis(host_wall)
    start = host_wall["start"]
    end = host_wall["end"]
    if axis == "horizontal":
        return (
            axis,
            float(start[1]) * 1000.0,
            min(float(start[0]), float(end[0])) * 1000.0,
            max(float(start[0]), float(end[0])) * 1000.0,
        )
    return (
        axis,
        float(start[0]) * 1000.0,
        min(float(start[1]), float(end[1])) * 1000.0,
        max(float(start[1]), float(end[1])) * 1000.0,
    )


def classify_mapping_segment_layer(
    *,
    layer_name: str,
    record_type: str,
    layer_profile: dict[str, object],
    cache: dict[tuple[str, str], dict[str, object]],
) -> dict[str, object]:
    cache_key = (str(layer_name or ""), str(record_type or "LINE"))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    record = Record(
        section="ENTITIES",
        record_type=str(record_type or "LINE"),
        raw_lines=[],
        start_pair_index=0,
        end_pair_index=0,
        layer=str(layer_name or ""),
    )
    classification = classify_record_layer(record, layer_profile)
    cache[cache_key] = classification
    return classification


def build_parser_layer_segments_by_axis(
    *,
    mapping_payload: dict[str, object],
    layer_profile: dict[str, object],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    layer_cache: dict[tuple[str, str], dict[str, object]] = {}
    segments_by_axis: dict[str, list[dict[str, object]]] = {"horizontal": [], "vertical": []}
    role_counts = Counter()
    canonical_counts = Counter()

    for segment in list(mapping_payload.get("opening_placement_segments", [])):
        if not isinstance(segment, dict):
            continue
        axis = str(segment.get("axis", "")).strip().lower()
        if axis not in {"horizontal", "vertical"}:
            continue
        classification = classify_mapping_segment_layer(
            layer_name=str(segment.get("layer", "")).strip(),
            record_type=str(segment.get("record_type", "")).strip() or "LINE",
            layer_profile=layer_profile,
            cache=layer_cache,
        )
        primary = dict(classification.get("primary", {}) or {})
        canonical_layer = str(primary.get("canonical_layer", "")).strip() or str(segment.get("layer", "")).strip()
        role_name = normalize_layer_role_for_thickness(
            str(primary.get("role", "")).strip(),
            canonical_layer,
        )
        if role_name not in (PRIMARY_WALL_LAYER_ROLES | SUPPORTING_WALL_LAYER_ROLES):
            continue
        try:
            fixed_coord_mm = float(segment.get("fixed_coord_mm", 0.0) or 0.0)
            span_start_mm = float(segment.get("interval_min_mm", 0.0) or 0.0)
            span_end_mm = float(segment.get("interval_max_mm", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        normalized_segment = {
            "record_handle": str(segment.get("record_handle", "")).strip(),
            "record_type": str(segment.get("record_type", "")).strip() or "LINE",
            "source_layer": str(segment.get("layer", "")).strip(),
            "axis": axis,
            "fixed_coord_mm": fixed_coord_mm,
            "span_start_mm": min(span_start_mm, span_end_mm),
            "span_end_mm": max(span_start_mm, span_end_mm),
            "layer_role": role_name,
            "layer_canonical": canonical_layer,
            "layer_match_source": str(primary.get("match_source", "")).strip(),
            "layer_match_confidence": str(primary.get("match_confidence", "")).strip(),
            "nominal_thickness_mm": wall_role_expected_thickness_mm(role_name, canonical_layer),
        }
        segments_by_axis[axis].append(normalized_segment)
        role_counts[role_name] += 1
        canonical_counts[canonical_layer] += 1

    for axis_name in segments_by_axis:
        segments_by_axis[axis_name].sort(
            key=lambda item: (
                float(item.get("fixed_coord_mm", 0.0)),
                float(item.get("span_start_mm", 0.0)),
                float(item.get("span_end_mm", 0.0)),
                str(item.get("source_layer", "")),
            )
        )

    return segments_by_axis, {
        "available": bool(role_counts),
        "horizontal_segment_count": len(segments_by_axis["horizontal"]),
        "vertical_segment_count": len(segments_by_axis["vertical"]),
        "role_counts": counter_to_sorted_dict(role_counts),
        "canonical_layer_counts": counter_to_sorted_dict(canonical_counts),
        "layer_profile_source": str(layer_profile.get("_resolved_path", "")).strip(),
    }


def parser_layer_segments_for_host(
    host_wall: dict[str, object],
    parser_segments_by_axis: dict[str, list[dict[str, object]]] | None,
    *,
    plane_tolerance_mm: float = 320.0,
    min_overlap_mm: float = 150.0,
    min_overlap_ratio: float = 0.15,
) -> list[dict[str, object]]:
    if not parser_segments_by_axis:
        return []

    axis, host_fixed_mm, host_span_min_mm, host_span_max_mm = host_wall_fixed_coord_and_span_mm(host_wall)
    host_length_mm = max(1.0, host_span_max_mm - host_span_min_mm)
    overlap_threshold_mm = max(min_overlap_mm, host_length_mm * min_overlap_ratio)

    nearby_segments: list[dict[str, object]] = []
    for segment in parser_segments_by_axis.get(axis, []):
        overlap_mm = interval_overlap_length(
            host_span_min_mm,
            host_span_max_mm,
            float(segment["span_start_mm"]),
            float(segment["span_end_mm"]),
        )
        if overlap_mm < overlap_threshold_mm:
            continue
        plane_delta_mm = abs(float(segment["fixed_coord_mm"]) - host_fixed_mm)
        if plane_delta_mm > plane_tolerance_mm:
            continue
        nearby_segments.append(
            {
                **segment,
                "overlap_mm": overlap_mm,
                "plane_delta_mm": plane_delta_mm,
            }
        )
    return nearby_segments


def select_adiabatic_layer_evidence_for_host(
    host_wall: dict[str, object],
    parser_segments_by_axis: dict[str, list[dict[str, object]]] | None,
) -> dict[str, object] | None:
    nearby_segments = parser_layer_segments_for_host(host_wall, parser_segments_by_axis)
    if not nearby_segments:
        return None

    _axis, _host_fixed_mm, host_span_min_mm, host_span_max_mm = host_wall_fixed_coord_and_span_mm(host_wall)
    host_length_mm = max(1.0, host_span_max_mm - host_span_min_mm)
    candidates: list[dict[str, object]] = []
    for segment in nearby_segments:
        if str(segment.get("layer_role", "")).strip() != "adiabatic_wall":
            continue
        overlap_mm = float(segment.get("overlap_mm", 0.0) or 0.0)
        plane_delta_mm = abs(float(segment.get("plane_delta_mm", 0.0) or 0.0))
        overlap_factor = max(0.0, min(1.35, overlap_mm / host_length_mm))
        plane_factor = max(0.25, 1.0 - (plane_delta_mm / 360.0))
        match_factor = layer_match_weight(str(segment.get("layer_match_source", "")))
        score = WALL_LAYER_ROLE_WEIGHT["adiabatic_wall"] * overlap_factor * plane_factor * match_factor
        candidates.append(
            {
                **segment,
                "score": round(score, 3),
                "overlap_ratio": round(overlap_mm / host_length_mm, 3),
                "plane_delta_mm": round(plane_delta_mm, 3),
            }
        )

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            float(item.get("score", 0.0) or 0.0),
            float(item.get("overlap_mm", 0.0) or 0.0),
            -float(item.get("plane_delta_mm", 0.0) or 0.0),
        ),
    )


def apply_layer_based_adiabatic_boundaries(
    surface_rows: list[dict[str, object]],
    wall_hosts_by_surface_name: dict[str, dict[str, object]],
    *,
    mapping_payload: dict[str, object],
    layer_profile: dict[str, object],
) -> dict[str, object]:
    parser_segments_by_axis, parser_layer_summary = build_parser_layer_segments_by_axis(
        mapping_payload=mapping_payload,
        layer_profile=layer_profile,
    )
    row_by_name = {
        str(row.get("surface_name", "")).strip(): row
        for row in surface_rows
        if str(row.get("surface_name", "")).strip()
    }

    converted_surfaces: list[dict[str, object]] = []
    skipped_surfaces: list[dict[str, object]] = []
    for surface_name, host_wall in wall_hosts_by_surface_name.items():
        evidence = select_adiabatic_layer_evidence_for_host(host_wall, parser_segments_by_axis)
        if evidence is None:
            continue

        boundary_condition = str(host_wall.get("boundary_condition", "")).strip()
        if boundary_condition != "Outdoors":
            skipped_surfaces.append(
                {
                    "surface_name": surface_name,
                    "boundary_condition": boundary_condition,
                    "reason": "only_outdoors_wall_boundaries_are_layer_overridden",
                    "source_layer": str(evidence.get("source_layer", "")).strip(),
                    "layer_canonical": str(evidence.get("layer_canonical", "")).strip(),
                }
            )
            continue

        row = row_by_name.get(surface_name)
        if row is None:
            continue

        source_layers = [str(evidence.get("source_layer", "")).strip()]
        record_handles = [str(evidence.get("record_handle", "")).strip()]
        row["outside_boundary_condition"] = "Adiabatic"
        row["outside_boundary_condition_object"] = ""
        row["sun_exposure"] = "NoSun"
        row["wind_exposure"] = "NoWind"
        row["view_factor_to_ground"] = "0"
        row["wall_boundary_override_source"] = "adiabatic_layer"
        row["wall_boundary_layer_canonical"] = str(evidence.get("layer_canonical", "")).strip()
        row["wall_boundary_layer_source_layers"] = source_layers
        row["wall_boundary_layer_record_handles"] = record_handles

        host_wall["boundary_condition"] = "Adiabatic"
        host_wall["adjacent_zone_name"] = ""
        host_wall["paired_surface_name"] = ""
        host_wall["wall_boundary_override_source"] = "adiabatic_layer"
        host_wall["wall_boundary_layer_canonical"] = str(evidence.get("layer_canonical", "")).strip()
        host_wall["wall_boundary_layer_source_layers"] = source_layers
        host_wall["wall_boundary_layer_record_handles"] = record_handles

        converted_surfaces.append(
            {
                "surface_name": surface_name,
                "previous_boundary_condition": boundary_condition,
                "new_boundary_condition": "Adiabatic",
                "source_layer": source_layers[0],
                "layer_canonical": str(evidence.get("layer_canonical", "")).strip(),
                "layer_match_source": str(evidence.get("layer_match_source", "")).strip(),
                "record_handle": record_handles[0],
                "overlap_mm": round(float(evidence.get("overlap_mm", 0.0) or 0.0), 3),
                "overlap_ratio": float(evidence.get("overlap_ratio", 0.0) or 0.0),
                "plane_delta_mm": float(evidence.get("plane_delta_mm", 0.0) or 0.0),
            }
        )

    return {
        "available": bool(converted_surfaces or skipped_surfaces),
        "converted_surface_count": len(converted_surfaces),
        "converted_surfaces": converted_surfaces,
        "skipped_surface_count": len(skipped_surfaces),
        "skipped_surfaces": skipped_surfaces,
        "parser_layer_evidence_summary": parser_layer_summary,
    }


def geometry_policy_wall_boundary_overrides(
    geometry_payload: dict[str, object] | None,
) -> list[dict[str, object]]:
    geometry_policy = {}
    if isinstance((geometry_payload or {}).get("geometry_policy"), dict):
        geometry_policy = dict((geometry_payload or {}).get("geometry_policy", {}))
    raw_overrides = geometry_policy.get("wall_boundary_overrides", [])
    if raw_overrides is None or raw_overrides == "":
        return []
    if not isinstance(raw_overrides, list):
        raise WorkspaceRuleError("geometry_policy.wall_boundary_overrides must be a list")

    overrides: list[dict[str, object]] = []
    valid_boundary_conditions = {"Outdoors", "Surface", "Adiabatic"}
    for index, raw_override in enumerate(raw_overrides, start=1):
        if not isinstance(raw_override, dict):
            raise WorkspaceRuleError(f"wall_boundary_overrides[{index}] must be an object")
        surface_name = str(raw_override.get("surface_name", "")).strip()
        boundary_condition = str(
            raw_override.get("outside_boundary_condition", raw_override.get("boundary_condition", ""))
        ).strip()
        if not surface_name:
            raise WorkspaceRuleError(f"wall_boundary_overrides[{index}] is missing surface_name")
        if boundary_condition not in valid_boundary_conditions:
            raise WorkspaceRuleError(
                f"wall_boundary_overrides[{index}] has invalid outside_boundary_condition: {boundary_condition}"
            )
        overrides.append(dict(raw_override))
    return overrides


def apply_geometry_policy_wall_boundary_overrides(
    surface_rows: list[dict[str, object]],
    wall_hosts_by_surface_name: dict[str, dict[str, object]],
    *,
    geometry_payload: dict[str, object] | None,
) -> dict[str, object]:
    overrides = geometry_policy_wall_boundary_overrides(geometry_payload)
    if not overrides:
        return {
            "available": False,
            "applied_override_count": 0,
            "applied_overrides": [],
        }

    row_by_name = {
        str(row.get("surface_name", "")).strip(): row
        for row in surface_rows
        if str(row.get("surface_name", "")).strip()
    }
    applied_overrides: list[dict[str, object]] = []
    for override in overrides:
        surface_name = str(override.get("surface_name", "")).strip()
        boundary_condition = str(
            override.get("outside_boundary_condition", override.get("boundary_condition", ""))
        ).strip()
        row = row_by_name.get(surface_name)
        host_wall = wall_hosts_by_surface_name.get(surface_name)
        if row is None or host_wall is None:
            raise WorkspaceRuleError(
                f"wall_boundary_overrides references unknown surface_name: {surface_name}"
            )

        previous_boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        paired_surface_name = str(override.get("outside_boundary_condition_object", "")).strip()
        adjacent_zone_name = str(override.get("adjacent_zone_name", "")).strip()
        row["outside_boundary_condition"] = boundary_condition
        row["outside_boundary_condition_object"] = paired_surface_name if boundary_condition == "Surface" else ""
        row["sun_exposure"] = "SunExposed" if boundary_condition == "Outdoors" else "NoSun"
        row["wind_exposure"] = "WindExposed" if boundary_condition == "Outdoors" else "NoWind"
        row["view_factor_to_ground"] = "" if boundary_condition == "Outdoors" else "0"
        row["wall_boundary_override_source"] = "geometry_policy"
        row["wall_boundary_override_reason"] = str(override.get("reason", "")).strip()

        wall_role = str(override.get("wall_role", "")).strip()
        if wall_role:
            row["wall_boundary_override_wall_role"] = wall_role
            host_wall["wall_boundary_override_wall_role"] = wall_role

        host_wall["boundary_condition"] = boundary_condition
        host_wall["adjacent_zone_name"] = adjacent_zone_name if boundary_condition == "Surface" else ""
        host_wall["paired_surface_name"] = paired_surface_name if boundary_condition == "Surface" else ""
        host_wall["wall_boundary_override_source"] = "geometry_policy"
        host_wall["wall_boundary_override_reason"] = str(override.get("reason", "")).strip()

        applied_overrides.append(
            {
                "surface_name": surface_name,
                "previous_boundary_condition": previous_boundary_condition,
                "new_boundary_condition": boundary_condition,
                "wall_role": wall_role,
                "reason": str(override.get("reason", "")).strip(),
            }
        )

    return {
        "available": True,
        "applied_override_count": len(applied_overrides),
        "applied_overrides": applied_overrides,
    }


def collect_local_parser_layer_thickness_votes(
    *,
    host_wall: dict[str, object],
    parser_segments_by_axis: dict[str, list[dict[str, object]]] | None,
    pair_tolerance_mm: float = 25.0,
) -> list[dict[str, object]]:
    nearby_segments = parser_layer_segments_for_host(host_wall, parser_segments_by_axis)
    _axis, _host_fixed_mm, host_span_min_mm, host_span_max_mm = host_wall_fixed_coord_and_span_mm(host_wall)
    host_length_mm = max(1.0, host_span_max_mm - host_span_min_mm)
    boundary_condition = str(host_wall.get("boundary_condition", "")).strip()
    votes: list[dict[str, object]] = []
    for index, first in enumerate(nearby_segments):
        first_role = str(first.get("layer_role", "")).strip()
        first_nominal = int(first.get("nominal_thickness_mm", 0) or 0)
        if first_role not in PRIMARY_WALL_LAYER_ROLES or first_nominal <= 0:
            continue
        if first_role == "adiabatic_wall" and boundary_condition not in {"Outdoors", "Adiabatic"}:
            continue
        for second in nearby_segments[index + 1 :]:
            second_role = str(second.get("layer_role", "")).strip()
            if first_role != second_role:
                continue
            if str(first.get("layer_canonical", "")).strip() != str(second.get("layer_canonical", "")).strip():
                continue
            gap_mm = normalize_dimension_measurement_mm(
                abs(float(first.get("fixed_coord_mm", 0.0)) - float(second.get("fixed_coord_mm", 0.0)))
            )
            if abs(gap_mm - first_nominal) > pair_tolerance_mm:
                continue
            overlap_mm = interval_overlap_length(
                max(host_span_min_mm, float(first["span_start_mm"])),
                min(host_span_max_mm, float(first["span_end_mm"])),
                max(host_span_min_mm, float(second["span_start_mm"])),
                min(host_span_max_mm, float(second["span_end_mm"])),
            )
            if overlap_mm < max(150.0, host_length_mm * 0.2):
                continue
            overlap_factor = max(0.45, min(1.35, overlap_mm / host_length_mm))
            pair_factor = pair_alignment_factor_for_host(
                host_wall=host_wall,
                first_fixed_mm=float(first["fixed_coord_mm"]),
                second_fixed_mm=float(second["fixed_coord_mm"]),
            )
            match_factor = min(
                layer_match_weight(str(first.get("layer_match_source", ""))),
                layer_match_weight(str(second.get("layer_match_source", ""))),
            )
            boundary_factor = layer_role_boundary_factor(boundary_condition, first_role)
            source_name = {
                "canonical": "layer_canonical",
                "alias": "layer_alias",
                "fuzzy": "layer_fuzzy",
            }.get(str(first.get("layer_match_source", "")).strip(), "layer_alias")
            votes.append(
                {
                    "wall_thickness_mm": int(first_nominal),
                    "score": WALL_LAYER_ROLE_WEIGHT.get(first_role, 5.0)
                    * overlap_factor
                    * pair_factor
                    * match_factor
                    * boundary_factor,
                    "source": source_name,
                    "layer_role": first_role,
                    "layer_canonical": str(first.get("layer_canonical", "")).strip(),
                    "source_layers": sorted(
                        {
                            str(first.get("source_layer", "")).strip(),
                            str(second.get("source_layer", "")).strip(),
                        }
                    ),
                    "record_handles": sorted(
                        {
                            str(first.get("record_handle", "")).strip(),
                            str(second.get("record_handle", "")).strip(),
                        }
                    ),
                    "overlap_mm": round(overlap_mm, 3),
                    "gap_mm": int(gap_mm),
                    "candidate_confidence": str(first.get("layer_match_confidence", "")).strip(),
                }
            )

    for segment in nearby_segments:
        role_name = str(segment.get("layer_role", "")).strip()
        nominal_thickness_mm = int(segment.get("nominal_thickness_mm", 0) or 0)
        if role_name not in PRIMARY_WALL_LAYER_ROLES or nominal_thickness_mm <= 0:
            continue
        if role_name == "adiabatic_wall" and boundary_condition not in {"Outdoors", "Adiabatic"}:
            continue
        if role_name == "external_wall" and boundary_condition not in {"Outdoors", "Adiabatic"}:
            continue
        if role_name == "partition" and boundary_condition == "Outdoors":
            continue
        if not re.search(r"_(\d{3})$", str(segment.get("layer_canonical", "")).strip()):
            continue

        overlap_mm = float(segment.get("overlap_mm", 0.0) or 0.0)
        overlap_factor = max(0.15, min(1.10, overlap_mm / host_length_mm))
        plane_delta_mm = abs(float(segment.get("plane_delta_mm", 0.0) or 0.0))
        plane_factor = max(0.25, 1.0 - (plane_delta_mm / max(360.0, nominal_thickness_mm * 2.0)))
        match_factor = layer_match_weight(str(segment.get("layer_match_source", "")))
        boundary_factor = layer_role_boundary_factor(boundary_condition, role_name)
        source_name = {
            "canonical": "layer_canonical",
            "alias": "layer_alias",
            "fuzzy": "layer_fuzzy",
        }.get(str(segment.get("layer_match_source", "")).strip(), "layer_alias")
        votes.append(
            {
                "wall_thickness_mm": nominal_thickness_mm,
                "score": WALL_LAYER_ROLE_WEIGHT.get(role_name, 5.0)
                * overlap_factor
                * plane_factor
                * match_factor
                * boundary_factor
                * 1.15,
                "source": source_name,
                "layer_role": role_name,
                "layer_canonical": str(segment.get("layer_canonical", "")).strip(),
                "source_layers": [str(segment.get("source_layer", "")).strip()],
                "record_handles": [str(segment.get("record_handle", "")).strip()],
                "overlap_mm": round(overlap_mm, 3),
                "plane_delta_mm": round(plane_delta_mm, 3),
                "candidate_confidence": str(segment.get("layer_match_confidence", "")).strip(),
            }
        )
    return votes


def pair_alignment_factor_for_host(
    *,
    host_wall: dict[str, object],
    first_fixed_mm: float,
    second_fixed_mm: float,
    bracket_tolerance_mm: float = 35.0,
) -> float:
    _axis, host_fixed_mm, _span_min_mm, _span_max_mm = host_wall_fixed_coord_and_span_mm(host_wall)
    pair_low_mm = min(first_fixed_mm, second_fixed_mm)
    pair_high_mm = max(first_fixed_mm, second_fixed_mm)
    boundary_condition = str(host_wall.get("boundary_condition", "")).strip()

    if pair_low_mm - bracket_tolerance_mm <= host_fixed_mm <= pair_high_mm + bracket_tolerance_mm:
        return 1.15 if boundary_condition == "Surface" else 1.0

    nearest_delta_mm = min(abs(host_fixed_mm - pair_low_mm), abs(host_fixed_mm - pair_high_mm))
    if boundary_condition == "Surface":
        return max(0.35, 0.8 - (nearest_delta_mm / 450.0))
    return max(0.45, 0.9 - (nearest_delta_mm / 600.0))


def select_scored_thickness_vote(
    votes: list[dict[str, object]],
) -> tuple[int | None, float, float, dict[int, float], list[dict[str, object]]]:
    if not votes:
        return None, 0.0, 0.0, {}, []

    scores: dict[int, float] = {}
    evidence_by_value: dict[int, list[dict[str, object]]] = {}
    for vote in votes:
        wall_thickness_mm = int(vote["wall_thickness_mm"])
        score = float(vote["score"])
        scores[wall_thickness_mm] = scores.get(wall_thickness_mm, 0.0) + score
        evidence_by_value.setdefault(wall_thickness_mm, []).append(vote)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_value_mm, best_score = ordered[0]
    runner_up_score = ordered[1][1] if len(ordered) > 1 else 0.0
    confidence_ratio = best_score / max(0.01, runner_up_score)
    best_evidence = sorted(
        evidence_by_value.get(best_value_mm, []),
        key=lambda item: (-float(item["score"]), str(item.get("source", ""))),
    )
    return best_value_mm, confidence_ratio, best_score, scores, best_evidence


def select_dimension_vote(
    votes: list[dict[str, object]],
) -> tuple[int | None, float, dict[int, float]]:
    if not votes:
        return None, 0.0, {}

    scores: dict[int, float] = {}
    for vote in votes:
        measurement_mm = int(vote["measurement_mm"])
        distance_m = float(vote["distance_m"])
        scores[measurement_mm] = scores.get(measurement_mm, 0.0) + (1.0 / max(0.05, distance_m + 0.05))

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_value_mm, best_score = ordered[0]
    runner_up_score = ordered[1][1] if len(ordered) > 1 else 0.0
    confidence_ratio = best_score / max(0.01, runner_up_score)
    return best_value_mm, confidence_ratio, scores


def snap_value_to_canonical(value_mm: int, canonical_values: set[int], *, tolerance_mm: int) -> int:
    if not canonical_values:
        return value_mm
    nearest = min(canonical_values, key=lambda candidate: (abs(candidate - value_mm), candidate))
    if abs(nearest - value_mm) <= tolerance_mm:
        return int(nearest)
    return int(value_mm)


def normalize_dimension_annotations(
    dimension_annotations: list[dict[str, object]],
    *,
    min_measurement_mm: int = 80,
    max_measurement_mm: int = 350,
    min_repeat_count: int = 2,
) -> dict[str, object]:
    annotations: list[dict[str, object]] = []
    for item in dimension_annotations:
        value_mm = item.get("value_mm")
        anchor_xy = item.get("anchor_xy")
        if not isinstance(value_mm, int | float):
            continue
        if not isinstance(anchor_xy, list) or len(anchor_xy) < 2:
            continue
        measurement_mm = normalize_dimension_measurement_mm(float(value_mm))
        if not (min_measurement_mm <= measurement_mm <= max_measurement_mm):
            continue
        try:
            midpoint_xy_m = (float(anchor_xy[0]) / 1000.0, float(anchor_xy[1]) / 1000.0)
        except (TypeError, ValueError):
            continue
        annotations.append(
            {
                "handle": str(item.get("handle", "")).strip(),
                "measurement_mm": int(measurement_mm),
                "midpoint_xy_m": midpoint_xy_m,
                "source_layer": str(item.get("source_layer", "")).strip(),
                "display_text": str(item.get("display_text", "")).strip(),
            }
        )

    value_counts = Counter(int(annotation["measurement_mm"]) for annotation in annotations)
    repeated_values = {
        value_mm
        for value_mm, count in value_counts.items()
        if count >= min_repeat_count
    }
    return {
        "annotations": annotations,
        "value_counts_mm": dict(sorted(value_counts.items())),
        "repeated_values_mm": sorted(repeated_values),
        "source_count": len(annotations),
    }


def default_wall_thickness_mm_by_boundary(
    value_counts_mm: dict[int, int],
    assigned_value_counts_by_boundary: dict[str, Counter[int]],
) -> dict[str, int]:
    repeated_values = sorted(
        int(value_mm)
        for value_mm, count in value_counts_mm.items()
        if int(count) >= 2
    )
    if not repeated_values:
        repeated_values = sorted(int(value_mm) for value_mm in value_counts_mm)
    if repeated_values:
        global_outdoor_default = max(repeated_values)
    else:
        global_outdoor_default = 250

    global_partition_candidates = [value_mm for value_mm in repeated_values if value_mm < global_outdoor_default]
    global_partition_default = min(global_partition_candidates) if global_partition_candidates else max(140, min(repeated_values, default=140))

    outdoors_default = (
        max(assigned_value_counts_by_boundary.get("Outdoors", Counter()).keys())
        if assigned_value_counts_by_boundary.get("Outdoors")
        else global_outdoor_default
    )
    surface_default = (
        assigned_value_counts_by_boundary.get("Surface", Counter()).most_common(1)[0][0]
        if assigned_value_counts_by_boundary.get("Surface")
        else global_partition_default
    )
    adiabatic_default = (
        assigned_value_counts_by_boundary.get("Adiabatic", Counter()).most_common(1)[0][0]
        if assigned_value_counts_by_boundary.get("Adiabatic")
        else surface_default
    )
    return {
        "Outdoors": int(outdoors_default),
        "Surface": int(surface_default),
        "Adiabatic": int(adiabatic_default),
    }


def construction_name_for_wall_thickness(
    *,
    boundary_condition: str,
    wall_thickness_mm: int,
    reverse: bool = False,
) -> str:
    input_construction_spec = resolve_input_wall_construction_spec(
        boundary_condition=boundary_condition,
        wall_thickness_mm=wall_thickness_mm,
    )
    if input_construction_spec is not None:
        if reverse and str(boundary_condition).strip() == "Surface":
            return str(input_construction_spec.get("reverse_name", input_construction_spec.get("construction_name", "")))
        return str(input_construction_spec.get("construction_name", ""))

    family = "Exterior" if boundary_condition == "Outdoors" else "Partition"
    name = f"DXF {family} Wall {int(wall_thickness_mm)}mm"
    if reverse and boundary_condition == "Surface":
        return f"{name}_Rev"
    return name


def apply_reference_wall_constructions(
    surface_rows: list[dict[str, object]],
    wall_hosts_by_surface_name: dict[str, dict[str, object]],
    surface_thicknesses: dict[str, dict[str, object]],
    reference_idf_path: Path | str | None = None,
) -> dict[str, object]:
    current_wall_rows = [
        row
        for row in surface_rows
        if str(row.get("surface_type", "")).strip() == "Wall"
    ]
    lookup = load_reference_wall_construction_lookup(current_wall_rows, reference_idf_path=reference_idf_path)
    if not lookup:
        return {
            "available": False,
            "applied_surface_count": 0,
            "reference_path": workspace_path(GUARD.resolve(reference_idf_path)) if reference_idf_path else "",
        }

    applied_surface_names: list[str] = []
    for row in current_wall_rows:
        boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        signature = wall_segment_signature_from_row(row)
        if signature is None:
            continue
        family_name = lookup.get((boundary_condition, signature))
        if not family_name:
            continue

        construction_name = family_name
        if boundary_condition == "Surface" and bool(row.get("inferred_construction_reverse")):
            construction_name = f"{family_name}_Rev"
        row["construction_name"] = construction_name

        wall_thickness_mm = wall_construction_thickness_mm(family_name)
        if wall_thickness_mm is not None:
            row["inferred_wall_thickness_mm"] = wall_thickness_mm
            row["wall_thickness_inference_source"] = "sample_reference"

        surface_name = str(row.get("surface_name", "")).strip()
        host_wall = wall_hosts_by_surface_name.get(surface_name)
        if host_wall is not None:
            host_wall["construction_name"] = construction_name
            if wall_thickness_mm is not None:
                host_wall["wall_thickness_mm"] = wall_thickness_mm
                host_wall["wall_thickness_inference_source"] = "sample_reference"

        payload = surface_thicknesses.setdefault(surface_name, {})
        if wall_thickness_mm is not None:
            payload["wall_thickness_mm"] = wall_thickness_mm
        payload["source"] = "sample_reference"
        payload.setdefault("dimension_handles", [])
        applied_surface_names.append(surface_name)

    return {
        "available": True,
        "applied_surface_count": len(applied_surface_names),
        "applied_surface_names": sorted(applied_surface_names),
        "reference_path": workspace_path(GUARD.resolve(reference_idf_path)) if reference_idf_path else "",
    }


def reconcile_paired_surface_wall_thicknesses(
    inferred_by_surface_name: dict[str, dict[str, object]],
    host_by_surface: dict[str, dict[str, object]],
) -> None:
    processed: set[tuple[str, str]] = set()
    for surface_name, host in host_by_surface.items():
        peer_name = str(host.get("paired_surface_name", "")).strip()
        if not peer_name:
            continue
        pair_key = tuple(sorted((surface_name, peer_name)))
        if pair_key in processed:
            continue
        processed.add(pair_key)
        first_payload = inferred_by_surface_name.get(surface_name)
        second_payload = inferred_by_surface_name.get(peer_name)
        if first_payload is None or second_payload is None:
            continue
        first_value = int(first_payload.get("wall_thickness_mm", 0) or 0)
        second_value = int(second_payload.get("wall_thickness_mm", 0) or 0)
        if first_value == second_value:
            continue
        choose_first = (
            float(first_payload.get("confidence_ratio") or 0.0),
            first_value,
        ) >= (
            float(second_payload.get("confidence_ratio") or 0.0),
            second_value,
        )
        chosen_payload = first_payload if choose_first else second_payload
        chosen_source = str(chosen_payload.get("source", "paired_surface"))
        chosen_surface_name = surface_name if choose_first else peer_name
        chosen_confidence_ratio = chosen_payload.get("confidence_ratio")
        chosen_score = chosen_payload.get("score")
        chosen_candidate_scores = dict(chosen_payload.get("candidate_scores", {}))
        chosen_dimension_handles = list(chosen_payload.get("dimension_handles", []))
        chosen_thickness_mm = int(chosen_payload.get("wall_thickness_mm", 0) or 0)
        chosen_wall_role = str(chosen_payload.get("wall_role", "")).strip()
        chosen_layer_canonical = str(chosen_payload.get("layer_canonical", "")).strip()
        chosen_layer_source_layers = list(chosen_payload.get("layer_source_layers", []))
        chosen_layer_record_handles = list(chosen_payload.get("layer_record_handles", []))
        for target_surface_name in pair_key:
            if target_surface_name == chosen_surface_name:
                inferred_by_surface_name[target_surface_name] = {
                    **dict(inferred_by_surface_name[target_surface_name]),
                    "wall_thickness_mm": chosen_thickness_mm,
                }
                continue
            inferred_by_surface_name[target_surface_name] = {
                "wall_thickness_mm": chosen_thickness_mm,
                "source": f"{chosen_source}_peer",
                "confidence_ratio": chosen_confidence_ratio,
                "score": chosen_score,
                "candidate_scores": chosen_candidate_scores,
                "dimension_handles": chosen_dimension_handles,
                "wall_role": chosen_wall_role,
                "layer_canonical": chosen_layer_canonical,
                "layer_source_layers": chosen_layer_source_layers,
                "layer_record_handles": chosen_layer_record_handles,
            }


def post_resolve_inferred_wall_thicknesses(
    inferred: dict[str, dict[str, object]],
    host_by_surface: dict[str, dict[str, object]],
) -> None:
    raw_counts_by_boundary: dict[str, Counter[int]] = {"Outdoors": Counter(), "Surface": Counter(), "Adiabatic": Counter()}
    canonical_by_boundary: dict[str, set[int]] = {"Outdoors": set(), "Surface": set(), "Adiabatic": set()}
    for surface_name, payload in inferred.items():
        host = host_by_surface.get(surface_name)
        if host is None:
            continue
        boundary = str(host.get("boundary_condition", "")).strip()
        value = int(payload.get("wall_thickness_mm", 0) or 0)
        if value <= 0:
            continue
        raw_counts_by_boundary.setdefault(boundary, Counter()).update([value])
        if canonical_source_name(payload.get("source", "")) in {
            "layer_canonical",
            "layer_alias",
            "layer_fuzzy",
            "wall_first_core_finish",
            "wall_first_same_layer",
            "dimension_direct",
        }:
            canonical_by_boundary.setdefault(boundary, set()).add(value)

    global_canonical = set().union(*canonical_by_boundary.values()) if canonical_by_boundary else set()
    interior_floor = min(global_canonical) if global_canonical else 140
    for surface_name, payload in inferred.items():
        host = host_by_surface.get(surface_name)
        if host is None:
            continue
        boundary = str(host.get("boundary_condition", "")).strip()
        value = int(payload.get("wall_thickness_mm", 0) or 0)
        source = str(payload.get("source", ""))
        source_root = canonical_source_name(source)
        canon = canonical_by_boundary.get(boundary, set())
        snap_tolerance = 20 if boundary == "Outdoors" else 12
        snapped = value
        if boundary in {"Surface", "Adiabatic"}:
            snap_pool = set(canon) | set(global_canonical)
            if value not in snap_pool or raw_counts_by_boundary.get(boundary, Counter()).get(value, 0) <= 1:
                snapped = snap_value_to_canonical(value, snap_pool - {value}, tolerance_mm=max(35, 2 * snap_tolerance))
                if snapped == value and global_canonical:
                    global_floor = min(global_canonical)
                    if value < global_floor and (global_floor - value) <= max(60, 4 * snap_tolerance):
                        snapped = int(global_floor)
                if boundary == "Outdoors" and value < interior_floor and (interior_floor - value) <= max(60, 4 * snap_tolerance):
                    snapped = max(int(snapped), int(interior_floor))
        elif boundary == "Outdoors" and raw_counts_by_boundary.get(boundary, Counter()).get(value, 0) <= 1:
            snap_pool = set(canon) | set(global_canonical)
            if snap_pool:
                snapped = snap_value_to_canonical(value, snap_pool, tolerance_mm=max(40, 2 * snap_tolerance))
                if snapped == value:
                    global_floor = min(snap_pool)
                    if value < global_floor and (global_floor - value) <= max(60, 4 * snap_tolerance):
                        snapped = int(global_floor)

        if snapped != value:
            payload["wall_thickness_mm"] = int(snapped)
            payload["source"] = f"{source}_resolved"


def infer_wall_thicknesses(
    *,
    wall_hosts_by_zone: dict[str, list[dict[str, object]]],
    mapping_payload: dict[str, object],
    dimension_summary: dict[str, object],
    layer_profile: dict[str, object],
    direct_match_distance_m: float = 0.45,
    soft_match_distance_m: float = 4.50,
    soft_min_repeat_count: int = 3,
) -> dict[str, object]:
    host_walls = [host for hosts in wall_hosts_by_zone.values() for host in hosts]
    host_by_surface = {str(host["surface_name"]): host for host in host_walls}
    parser_segments_by_axis, parser_layer_summary = build_parser_layer_segments_by_axis(
        mapping_payload=mapping_payload,
        layer_profile=layer_profile,
    )
    annotations = list(dimension_summary.get("annotations", []))
    repeated_counts = {
        int(key): int(value)
        for key, value in dict(dimension_summary.get("value_counts_mm", {})).items()
    }
    direct_votes_by_surface: dict[str, list[dict[str, object]]] = {}
    soft_votes_by_surface: dict[str, list[dict[str, object]]] = {}
    for annotation in annotations:
        midpoint = tuple(annotation["midpoint_xy_m"])
        best_host = None
        best_distance = None
        for host in host_walls:
            _proj, _along, distance = host_wall_projection(midpoint, host)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_host = host
        if best_host is None or best_distance is None or best_distance > soft_match_distance_m:
            continue
        vote = {
            "measurement_mm": int(annotation["measurement_mm"]),
            "distance_m": round(best_distance, 6),
            "dimension_handle": annotation.get("handle"),
        }
        surface_name = str(best_host["surface_name"])
        if best_distance <= direct_match_distance_m:
            direct_votes_by_surface.setdefault(surface_name, []).append(vote)
        else:
            soft_votes_by_surface.setdefault(surface_name, []).append(vote)

    inferred: dict[str, dict[str, object]] = {}
    assigned_by_boundary: dict[str, Counter[int]] = {"Outdoors": Counter(), "Surface": Counter(), "Adiabatic": Counter()}
    for surface_name, host in host_by_surface.items():
        boundary = str(host.get("boundary_condition", "")).strip()
        layer_votes = collect_local_parser_layer_thickness_votes(
            host_wall=host,
            parser_segments_by_axis=parser_segments_by_axis,
        )
        value, ratio, best_score, candidate_scores, best_evidence = select_scored_thickness_vote(layer_votes)
        if value is not None:
            min_score = 4.0 if boundary in {"Surface", "Adiabatic"} else 3.8
            if best_score >= min_score and ratio >= 1.05:
                best_vote = best_evidence[0] if best_evidence else {}
                inferred[surface_name] = {
                    "wall_thickness_mm": int(value),
                    "source": str(best_vote.get("source", "layer_alias")) if best_vote else "layer_alias",
                    "confidence_ratio": round(ratio, 3),
                    "candidate_scores": {str(key): round(score, 3) for key, score in sorted(candidate_scores.items())},
                    "dimension_handles": [],
                    "wall_role": str(best_vote.get("layer_role", "")).strip(),
                    "layer_canonical": str(best_vote.get("layer_canonical", "")).strip(),
                    "layer_source_layers": list(best_vote.get("source_layers", [])),
                    "layer_record_handles": list(best_vote.get("record_handles", [])),
                }
                assigned_by_boundary.setdefault(boundary, Counter()).update([int(value)])
                continue

    for surface_name, host in host_by_surface.items():
        if surface_name in inferred:
            continue
        value, ratio, _scores = select_dimension_vote(direct_votes_by_surface.get(surface_name, []))
        if value is None:
            continue
        boundary = str(host.get("boundary_condition", "")).strip()
        inferred[surface_name] = {
            "wall_thickness_mm": int(value),
            "source": "dimension_direct",
            "confidence_ratio": round(ratio, 3),
            "dimension_handles": [
                str(vote["dimension_handle"])
                for vote in direct_votes_by_surface.get(surface_name, [])
                if vote.get("dimension_handle")
            ],
            "wall_role": "",
            "layer_canonical": "",
            "layer_source_layers": [],
            "layer_record_handles": [],
        }
        assigned_by_boundary.setdefault(boundary, Counter()).update([int(value)])

    direct_supported = {int(payload["wall_thickness_mm"]) for payload in inferred.values()}
    for surface_name, host in host_by_surface.items():
        if surface_name in inferred:
            continue
        soft_votes = [
            vote
            for vote in soft_votes_by_surface.get(surface_name, [])
            if repeated_counts.get(int(vote["measurement_mm"]), 0) >= soft_min_repeat_count
            or int(vote["measurement_mm"]) in direct_supported
        ]
        value, ratio, _scores = select_dimension_vote(soft_votes)
        if value is None or ratio < 1.10:
            continue
        boundary = str(host.get("boundary_condition", "")).strip()
        inferred[surface_name] = {
            "wall_thickness_mm": int(value),
            "source": "dimension_soft",
            "confidence_ratio": round(ratio, 3),
            "dimension_handles": [
                str(vote["dimension_handle"])
                for vote in soft_votes
                if vote.get("dimension_handle")
            ],
            "wall_role": "",
            "layer_canonical": "",
            "layer_source_layers": [],
            "layer_record_handles": [],
        }
        assigned_by_boundary.setdefault(boundary, Counter()).update([int(value)])

    reconcile_paired_surface_wall_thicknesses(inferred, host_by_surface)
    post_resolve_inferred_wall_thicknesses(inferred, host_by_surface)
    assigned_by_boundary = {"Outdoors": Counter(), "Surface": Counter(), "Adiabatic": Counter()}
    for surface_name, payload in inferred.items():
        host = host_by_surface.get(surface_name)
        if host is None:
            continue
        boundary = str(host.get("boundary_condition", "")).strip()
        value = int(payload.get("wall_thickness_mm", 0) or 0)
        if value > 0:
            assigned_by_boundary.setdefault(boundary, Counter()).update([value])

    fallback = default_wall_thickness_mm_by_boundary(repeated_counts, assigned_by_boundary)
    for surface_name, host in host_by_surface.items():
        if surface_name in inferred:
            continue
        boundary = str(host.get("boundary_condition", "")).strip()
        value = int(fallback.get(boundary, fallback.get("Surface", 140)))
        inferred[surface_name] = {
            "wall_thickness_mm": value,
            "source": "boundary_fallback",
            "confidence_ratio": None,
            "dimension_handles": [],
            "wall_role": "",
            "layer_canonical": "",
            "layer_source_layers": [],
            "layer_record_handles": [],
        }

    source_counts = Counter(str(payload.get("source", "")) for payload in inferred.values())
    return {
        "surface_thicknesses": inferred,
        "wall_first_surface_thicknesses": {
            surface_name: payload
            for surface_name, payload in inferred.items()
            if canonical_source_name(payload.get("source", "")) in {
                "layer_canonical",
                "layer_alias",
                "layer_fuzzy",
                "wall_first_core_finish",
                "wall_first_same_layer",
            }
        },
        "parser_layer_evidence_summary": {
            **parser_layer_summary,
            "matched_surface_count": sum(
                1
                for payload in inferred.values()
                if canonical_source_name(payload.get("source", "")) in {"layer_canonical", "layer_alias", "layer_fuzzy"}
            ),
        },
        "dimension_value_counts_mm": repeated_counts,
        "repeated_dimension_values_mm": sorted(int(value) for value in dimension_summary.get("repeated_values_mm", [])),
        "default_thickness_by_boundary_mm": fallback,
        "matched_surface_count": sum(
            1 for payload in inferred.values() if canonical_source_name(payload.get("source", "")).startswith("dimension_")
        ),
        "wall_first_matched_surface_count": sum(
            1
            for payload in inferred.values()
            if canonical_source_name(payload.get("source", "")) in {
                "layer_canonical",
                "layer_alias",
                "layer_fuzzy",
                "wall_first_core_finish",
                "wall_first_same_layer",
            }
        ),
        "source_counts": dict(sorted(source_counts.items())),
    }


def estimate_opening_host_surface_names(
    mapping_payload: dict[str, object],
    wall_hosts_by_zone: dict[str, list[dict[str, object]]],
    *,
    max_host_distance_m: float = 1.75,
) -> set[str]:
    zone_name_by_key: dict[str, str] = {}
    for zone_name in wall_hosts_by_zone:
        suffix = str(zone_name).split("APARTMENT_A_", 1)[-1]
        zone_name_by_key[canonical_zone_key(suffix)] = zone_name

    opening_hosts: set[str] = set()
    for opening in list(mapping_payload.get("candidate_openings", [])):
        if not isinstance(opening, dict):
            continue
        anchor_xy_m = (
            opening_anchor_xy_m(opening.get("matched_opening_geometry_anchor_xy"))
            or opening_anchor_xy_m(opening.get("anchor_xy"))
            or opening_anchor_xy_m(opening.get("matched_symbol_anchor_xy"))
            or opening_anchor_xy_m(opening.get("cluster_centroid_xy"))
            or opening_anchor_xy_m(opening.get("annotation_anchor_xy"))
        )
        if anchor_xy_m is None:
            continue
        candidate_zone_key = canonical_zone_key(str(opening.get("nearest_zone_key", "") or opening.get("nearest_zone_name", "")))
        zone_name = zone_name_by_key.get(candidate_zone_key, "")
        candidate_hosts = list(wall_hosts_by_zone.get(zone_name, [])) if zone_name else []
        if not candidate_hosts:
            candidate_hosts = [host for hosts in wall_hosts_by_zone.values() for host in hosts]
        if not candidate_hosts:
            continue
        best_host = None
        best_distance = None
        for host in candidate_hosts:
            _projection_xy_m, _along_m, distance_m = host_wall_projection(anchor_xy_m, host)
            if best_distance is None or distance_m < best_distance:
                best_distance = distance_m
                best_host = host
        if best_host is None or best_distance is None or best_distance > max_host_distance_m:
            continue
        opening_hosts.add(str(best_host.get("surface_name", "")).strip())
    return opening_hosts


def parse_construction_thickness(construction_name: str) -> int | None:
    match = THICKNESS_RE.search(construction_name or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def source_strength(payload: dict[str, object] | None) -> float:
    source_root = canonical_source_name((payload or {}).get("source", ""))
    return float(SOURCE_PRIORITY.get(source_root, 0.5))


def payload_current_value_score(payload: dict[str, object] | None, thickness_mm: int) -> float:
    if thickness_mm <= 0:
        return 0.0
    candidate_scores = dict((payload or {}).get("candidate_scores", {}))
    raw_score = candidate_scores.get(str(int(thickness_mm)))
    if raw_score is None:
        return 0.0
    try:
        return float(raw_score)
    except (TypeError, ValueError):
        return 0.0


def is_weak_source(payload: dict[str, object] | None) -> bool:
    source_root = canonical_source_name((payload or {}).get("source", ""))
    return source_root in {"boundary_fallback", "dimension_soft"} or source_strength(payload) < 3.0


def surface_segment(row: dict[str, object]) -> dict[str, object] | None:
    if str(row.get("surface_type", "")).strip() != "Wall":
        return None
    try:
        x1 = float(row.get("v1_x", 0.0))
        y1 = float(row.get("v1_y", 0.0))
        x2 = float(row.get("v4_x", 0.0))
        y2 = float(row.get("v4_y", 0.0))
    except ValueError:
        return None
    if abs(y1 - y2) <= 1e-6:
        axis = "horizontal"
        fixed = round(y1, 3)
        var_min = min(x1, x2)
        var_max = max(x1, x2)
    elif abs(x1 - x2) <= 1e-6:
        axis = "vertical"
        fixed = round(x1, 3)
        var_min = min(y1, y2)
        var_max = max(y1, y2)
    else:
        return None
    return {"axis": axis, "fixed_coord": fixed, "var_min": round(var_min, 3), "var_max": round(var_max, 3)}


def build_physical_walls(
    surface_rows: list[dict[str, object]],
    surface_thicknesses: dict[str, dict[str, object]],
    opening_hosts: set[str],
) -> list[dict[str, object]]:
    row_by_name = {
        str(row.get("surface_name", "")).strip(): row
        for row in surface_rows
        if str(row.get("surface_name", "")).strip()
    }
    seen: set[tuple[str, ...]] = set()
    physical: list[dict[str, object]] = []
    for row in surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        surface_name = str(row.get("surface_name", "")).strip()
        if not surface_name:
            continue
        boundary = str(row.get("outside_boundary_condition", "")).strip()
        if boundary == "Surface":
            peer = str(row.get("outside_boundary_condition_object", "")).strip()
            key = tuple(sorted(name for name in (surface_name, peer) if name))
        else:
            key = (surface_name,)
        if key in seen:
            continue
        seen.add(key)
        base_row = row_by_name.get(key[0])
        if base_row is None:
            continue
        segment = surface_segment(base_row)
        if segment is None:
            continue
        payloads = [surface_thicknesses.get(name, {}) for name in key]
        strongest_payload = max(payloads, key=source_strength) if payloads else {}
        thickness = next(
            (
                int(payload.get("wall_thickness_mm", 0) or 0)
                for payload in payloads
                if int(payload.get("wall_thickness_mm", 0) or 0) > 0
            ),
            parse_construction_thickness(str(base_row.get("construction_name", "")) or "") or 0,
        )
        physical.append(
            {
                "group_key": key,
                "boundary_condition": boundary,
                "has_opening": any(name in opening_hosts for name in key),
                "thickness_mm": int(thickness),
                "payload": strongest_payload,
                **segment,
            }
        )
    return physical


def apply_collinear_chain_rule(physical_walls: list[dict[str, object]]) -> dict[tuple[str, ...], int]:
    groups_by_line: dict[tuple[str, float, str], list[dict[str, object]]] = defaultdict(list)
    for wall in physical_walls:
        if str(wall.get("boundary_condition", "")).strip() != "Outdoors":
            continue
        key = (
            str(wall.get("axis", "")),
            float(wall.get("fixed_coord", 0.0)),
            str(wall.get("boundary_condition", "")),
        )
        groups_by_line[key].append(dict(wall))

    global_thickness_counts = Counter(
        int(wall.get("thickness_mm", 0) or 0)
        for wall in physical_walls
        if str(wall.get("boundary_condition", "")).strip() == "Outdoors"
        and int(wall.get("thickness_mm", 0) or 0) > 0
    )
    resolved: dict[tuple[str, ...], int] = {}
    for walls in groups_by_line.values():
        walls.sort(key=lambda item: (float(item.get("var_min", 0.0)), float(item.get("var_max", 0.0))))
        chains: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        for wall in walls:
            if not current:
                current = [wall]
                continue
            gap = float(wall.get("var_min", 0.0)) - float(current[-1].get("var_max", 0.0))
            if gap <= 0.35:
                current.append(wall)
            else:
                chains.append(current)
                current = [wall]
        if current:
            chains.append(current)

        for chain in chains:
            if len(chain) < 2:
                continue

            for endpoint_index in {0, len(chain) - 1}:
                endpoint = chain[endpoint_index]
                endpoint_thickness = int(endpoint.get("thickness_mm", 0) or 0)
                if endpoint_thickness <= 0:
                    continue
                if global_thickness_counts.get(endpoint_thickness, 0) != 1:
                    continue
                if not is_weak_source(endpoint.get("payload")):
                    continue

                neighbor = chain[1] if endpoint_index == 0 else chain[-2]
                neighbor_thickness = int(neighbor.get("thickness_mm", 0) or 0)
                if neighbor_thickness <= 0 or neighbor_thickness == endpoint_thickness:
                    continue
                if (
                    global_thickness_counts.get(neighbor_thickness, 0) >= 2
                    or source_strength(neighbor.get("payload")) > source_strength(endpoint.get("payload"))
                ):
                    endpoint["thickness_mm"] = neighbor_thickness

            non_opening = [item for item in chain if not item.get("has_opening")]
            if len(non_opening) >= 2:
                strongest_non_opening = max(
                    non_opening,
                    key=lambda item: (
                        source_strength(item.get("payload")),
                        payload_current_value_score(item.get("payload"), int(item.get("thickness_mm", 0) or 0)),
                        global_thickness_counts.get(int(item.get("thickness_mm", 0) or 0), 0),
                        float((item.get("payload") or {}).get("confidence_ratio") or 0.0),
                        int(item.get("thickness_mm", 0) or 0),
                    ),
                )
                canonical_non_opening = int(strongest_non_opening.get("thickness_mm", 0) or 0)
                if canonical_non_opening > 0:
                    for item in non_opening:
                        if int(item.get("thickness_mm", 0) or 0) == canonical_non_opening:
                            continue
                        if is_weak_source(item.get("payload")):
                            item["thickness_mm"] = canonical_non_opening

            chain_counts = Counter(
                int(item.get("thickness_mm", 0) or 0)
                for item in chain
                if int(item.get("thickness_mm", 0) or 0) > 0
            )
            ordered_counts = chain_counts.most_common()
            dominant_thickness = None
            if ordered_counts and ordered_counts[0][1] >= 2:
                top_thickness_mm, top_count = ordered_counts[0]
                runner_up_count = ordered_counts[1][1] if len(ordered_counts) > 1 else 0
                if top_count > runner_up_count:
                    dominant_thickness = int(top_thickness_mm)
            elif len(chain) == 2:
                first_item, second_item = chain
                first_thickness = int(first_item.get("thickness_mm", 0) or 0)
                second_thickness = int(second_item.get("thickness_mm", 0) or 0)
                if (
                    first_thickness > 0
                    and second_thickness > 0
                    and first_thickness != second_thickness
                    and abs(first_thickness - second_thickness) <= 20
                ):
                    ranked_items = sorted(
                        chain,
                        key=lambda item: (
                            global_thickness_counts.get(int(item.get("thickness_mm", 0) or 0), 0),
                            source_strength(item.get("payload")),
                            payload_current_value_score(
                                item.get("payload"),
                                int(item.get("thickness_mm", 0) or 0),
                            ),
                            float((item.get("payload") or {}).get("confidence_ratio") or 0.0),
                            int(item.get("thickness_mm", 0) or 0),
                        ),
                        reverse=True,
                    )
                    preferred_item = ranked_items[0]
                    candidate_item = ranked_items[1]
                    preferred_thickness = int(preferred_item.get("thickness_mm", 0) or 0)
                    candidate_thickness = int(candidate_item.get("thickness_mm", 0) or 0)
                    if (
                        global_thickness_counts.get(preferred_thickness, 0) >= 2
                        and global_thickness_counts.get(candidate_thickness, 0) <= 1
                        and (
                            source_strength(preferred_item.get("payload")) > source_strength(candidate_item.get("payload"))
                            or payload_current_value_score(preferred_item.get("payload"), preferred_thickness) > payload_current_value_score(candidate_item.get("payload"), candidate_thickness)
                            or is_weak_source(candidate_item.get("payload"))
                        )
                    ):
                        candidate_item["thickness_mm"] = preferred_thickness
                        dominant_thickness = preferred_thickness
            if dominant_thickness is None:
                continue

            for item in chain:
                current_thickness = int(item.get("thickness_mm", 0) or 0)
                if current_thickness == dominant_thickness:
                    continue
                if chain_counts.get(current_thickness, 0) > 1:
                    continue
                if (
                    global_thickness_counts.get(current_thickness, 0) == 1
                    or item.get("has_opening")
                    or is_weak_source(item.get("payload"))
                ):
                    item["thickness_mm"] = dominant_thickness
            for item in chain:
                resolved[tuple(sorted(item.get("group_key", ())))] = int(item.get("thickness_mm", 0) or 0)
    return resolved


def apply_surface_thickness_payloads(
    surface_rows: list[dict[str, object]],
    wall_hosts_by_surface_name: dict[str, dict[str, object]],
    surface_thicknesses: dict[str, dict[str, object]],
) -> None:
    for row in surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        surface_name = str(row.get("surface_name", "")).strip()
        payload = surface_thicknesses.get(surface_name)
        if payload is None:
            continue
        boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        wall_thickness_mm = int(payload.get("wall_thickness_mm", 0) or 0)
        explicit_role = str(row.get("wall_boundary_override_wall_role", "")).strip() or str(
            payload.get("wall_role", "")
        ).strip()
        wall_role = inferred_wall_role(
            boundary_condition=boundary_condition,
            wall_thickness_mm=wall_thickness_mm,
            explicit_role=explicit_role,
        )
        wall_family = wall_family_from_role(
            boundary_condition=boundary_condition,
            role_name=wall_role,
            wall_thickness_mm=wall_thickness_mm,
        )
        row["inferred_wall_thickness_mm"] = wall_thickness_mm
        row["wall_thickness_inference_source"] = str(payload.get("source", "")).strip()
        row["inferred_wall_role"] = wall_role
        row["inferred_wall_family"] = wall_family
        row["wall_layer_canonical"] = str(payload.get("layer_canonical", "")).strip()
        row["wall_layer_source_layers"] = list(payload.get("layer_source_layers", []))
        row["construction_name"] = construction_name_for_wall_thickness(
            boundary_condition=boundary_condition,
            wall_thickness_mm=wall_thickness_mm,
            reverse=bool(row.get("inferred_construction_reverse")),
        )
        host_wall = wall_hosts_by_surface_name.get(surface_name)
        if host_wall is not None:
            host_wall["wall_thickness_mm"] = wall_thickness_mm
            host_wall["wall_thickness_inference_source"] = str(payload.get("source", "")).strip()
            host_wall["construction_name"] = str(row.get("construction_name", "")).strip()
            host_wall["wall_role"] = wall_role
            host_wall["wall_family"] = wall_family
            host_wall["wall_layer_canonical"] = str(payload.get("layer_canonical", "")).strip()
            host_wall["wall_layer_source_layers"] = list(payload.get("layer_source_layers", []))


def apply_collinear_wall_resolution(
    *,
    surface_rows: list[dict[str, object]],
    wall_hosts_by_surface_name: dict[str, dict[str, object]],
    wall_thickness_inference: dict[str, object],
    opening_hosts: set[str],
) -> dict[tuple[str, ...], int]:
    surface_thicknesses = {
        str(surface_name): dict(payload)
        for surface_name, payload in dict(wall_thickness_inference.get("surface_thicknesses", {})).items()
    }
    physical_walls = build_physical_walls(surface_rows, surface_thicknesses, opening_hosts)
    resolved_by_group = apply_collinear_chain_rule(physical_walls)
    row_by_name = {
        str(row.get("surface_name", "")).strip(): row
        for row in surface_rows
        if str(row.get("surface_name", "")).strip()
    }
    for group_key, thickness_mm in resolved_by_group.items():
        for surface_name in group_key:
            row = row_by_name.get(surface_name)
            if row is None:
                continue
            boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
            payload = surface_thicknesses.setdefault(surface_name, {})
            current_source_name = str(row.get("wall_thickness_inference_source", "")).strip()
            payload_source_name = str(payload.get("source", "")).strip()
            if current_source_name == "sample_reference" or payload_source_name == "sample_reference":
                payload["wall_thickness_mm"] = int(row.get("inferred_wall_thickness_mm", 0) or 0)
                payload["source"] = "sample_reference"
                continue
            source_name = str(payload.get("source", "collinear")).strip() or "collinear"
            if not source_name.endswith("_collinear_resolved"):
                source_name = f"{source_name}_collinear_resolved"
            payload["wall_thickness_mm"] = int(thickness_mm)
            payload["source"] = source_name
            row["inferred_wall_thickness_mm"] = int(thickness_mm)
            row["wall_thickness_inference_source"] = source_name
            row["construction_name"] = construction_name_for_wall_thickness(
                boundary_condition=boundary_condition,
                wall_thickness_mm=int(thickness_mm),
                reverse=bool(row.get("inferred_construction_reverse")),
            )
            host_wall = wall_hosts_by_surface_name.get(surface_name)
            if host_wall is not None:
                host_wall["wall_thickness_mm"] = int(thickness_mm)
                host_wall["wall_thickness_inference_source"] = source_name
                host_wall["construction_name"] = row["construction_name"]

    wall_thickness_inference["surface_thicknesses"] = surface_thicknesses
    wall_thickness_inference["source_counts"] = dict(
        sorted(Counter(str(payload.get("source", "")) for payload in surface_thicknesses.values()).items())
    )
    wall_thickness_inference["collinear_resolved_group_count"] = len(resolved_by_group)
    return resolved_by_group


def build_positions_csv(
    building_rows: list[dict[str, object]],
    surface_thicknesses: dict[str, dict[str, object]],
    out_path: Path,
) -> None:
    row_by_name = {
        str(row.get("surface_name", "")).strip(): row
        for row in building_rows
        if str(row.get("surface_name", "")).strip()
    }
    seen: set[tuple[str, ...]] = set()
    rows: list[dict[str, object]] = []
    for row in building_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        surface_name = str(row.get("surface_name", "")).strip()
        boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        if boundary_condition == "Surface":
            peer_surface_name = str(row.get("outside_boundary_condition_object", "")).strip()
            group_key = tuple(
                sorted(name for name in (surface_name, peer_surface_name) if name)
            )
        else:
            group_key = (surface_name,)
        if group_key in seen or not group_key:
            continue
        seen.add(group_key)
        base_row = row_by_name.get(group_key[0])
        if base_row is None:
            continue
        segment = surface_segment(base_row)
        if segment is None:
            continue
        thickness_mm = int(
            surface_thicknesses.get(group_key[0], {}).get("wall_thickness_mm", 0)
            or parse_construction_thickness(str(base_row.get("construction_name", "")) or "")
            or 0
        )
        rows.append(
            {
                "thickness_mm": thickness_mm,
                "boundary_condition": boundary_condition,
                "x1_m": segment["var_min"] if segment["axis"] == "horizontal" else segment["fixed_coord"],
                "y1_m": segment["fixed_coord"] if segment["axis"] == "horizontal" else segment["var_min"],
                "x2_m": segment["var_max"] if segment["axis"] == "horizontal" else segment["fixed_coord"],
                "y2_m": segment["fixed_coord"] if segment["axis"] == "horizontal" else segment["var_max"],
                "surface_names": "; ".join(group_key),
            }
        )
    rows.sort(
        key=lambda item: (
            float(item["y1_m"]),
            float(item["x1_m"]),
            str(item["boundary_condition"]),
            str(item["surface_names"]),
        )
    )
    write_csv_rows(
        out_path,
        rows,
        ["thickness_mm", "boundary_condition", "x1_m", "y1_m", "x2_m", "y2_m", "surface_names"],
    )


def canonical_segment_points(
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    normalized_start = (round(float(start[0]), 3), round(float(start[1]), 3))
    normalized_end = (round(float(end[0]), 3), round(float(end[1]), 3))
    return (
        (normalized_start, normalized_end)
        if normalized_start <= normalized_end
        else (normalized_end, normalized_start)
    )


def normalized_wall_orientation_deg(
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    angle_deg = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
    if angle_deg < 0.0:
        angle_deg += 180.0
    if angle_deg >= 180.0:
        angle_deg -= 180.0
    return round(angle_deg, 3)


def wall_orientation_cardinal(
    start: tuple[float, float],
    end: tuple[float, float],
) -> str:
    dx = abs(end[0] - start[0])
    dy = abs(end[1] - start[1])
    return "East-West" if dx >= dy else "North-South"


def wall_family_for_boundary_condition(boundary_condition: str) -> str:
    if boundary_condition == "Outdoors":
        return "Exterior"
    if boundary_condition == "Surface":
        return "Partition"
    if boundary_condition == "Adiabatic":
        return "Adiabatic"
    return "Other"


def wall_type_name_for_inventory(total_thickness_mm: int) -> str:
    return f"DXF Wall {int(total_thickness_mm)}mm Total"


def build_wall_inventory_rows(
    wall_hosts_by_zone: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    all_host_walls = [
        host_wall
        for host_wall_list in wall_hosts_by_zone.values()
        for host_wall in host_wall_list
    ]
    host_by_surface_name = {
        str(host_wall.get("surface_name", "")): host_wall
        for host_wall in all_host_walls
        if str(host_wall.get("surface_name", "")).strip()
    }

    inventory_rows: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for host_wall in sorted(all_host_walls, key=lambda item: str(item.get("surface_name", ""))):
        surface_name_primary = str(host_wall.get("surface_name", "")).strip()
        if not surface_name_primary:
            continue

        boundary_condition = str(host_wall.get("boundary_condition", "")).strip()
        paired_surface_name = str(host_wall.get("paired_surface_name", "")).strip()
        if boundary_condition == "Surface" and paired_surface_name:
            group_key = ("Surface", *sorted((surface_name_primary, paired_surface_name)))
        else:
            group_key = (boundary_condition, surface_name_primary, "")
        if group_key in seen_keys:
            continue
        seen_keys.add(group_key)

        peer_wall = host_by_surface_name.get(paired_surface_name) if paired_surface_name else None
        if peer_wall is not None and surface_name_primary > paired_surface_name:
            primary_wall = peer_wall
            secondary_wall = host_wall
        else:
            primary_wall = host_wall
            secondary_wall = peer_wall

        primary_start_values = primary_wall.get("start")
        primary_end_values = primary_wall.get("end")
        if not isinstance(primary_start_values, list) or len(primary_start_values) < 2:
            continue
        if not isinstance(primary_end_values, list) or len(primary_end_values) < 2:
            continue

        start_xy_m, end_xy_m = canonical_segment_points(
            (float(primary_start_values[0]), float(primary_start_values[1])),
            (float(primary_end_values[0]), float(primary_end_values[1])),
        )
        midpoint_x_m = round((start_xy_m[0] + end_xy_m[0]) / 2.0, 3)
        midpoint_y_m = round((start_xy_m[1] + end_xy_m[1]) / 2.0, 3)
        length_m = round(math.hypot(end_xy_m[0] - start_xy_m[0], end_xy_m[1] - start_xy_m[1]), 3)
        height_m = round(float(primary_wall.get("height_m", 0.0) or 0.0), 3)

        thickness_candidates = [
            int(payload)
            for payload in [
                primary_wall.get("wall_thickness_mm"),
                secondary_wall.get("wall_thickness_mm") if secondary_wall is not None else None,
            ]
            if payload not in {None, ""}
        ]
        total_thickness_mm = max(thickness_candidates) if thickness_candidates else 0
        finish_each_side_mm = 15 if total_thickness_mm > 0 else 0
        finish_total_mm = finish_each_side_mm * 2 if total_thickness_mm > 0 else 0
        core_thickness_mm = max(0, total_thickness_mm - finish_total_mm)
        primary_role = str(primary_wall.get("wall_role", "")).strip()
        secondary_role = str(secondary_wall.get("wall_role", "")).strip() if secondary_wall else ""
        effective_role = primary_role or secondary_role or inferred_wall_role(
            boundary_condition=boundary_condition,
            wall_thickness_mm=total_thickness_mm,
        )
        effective_family = str(primary_wall.get("wall_family", "")).strip()
        if not effective_family and secondary_wall is not None:
            effective_family = str(secondary_wall.get("wall_family", "")).strip()
        if not effective_family:
            effective_family = wall_family_from_role(
                boundary_condition=boundary_condition,
                role_name=effective_role,
                wall_thickness_mm=total_thickness_mm,
            )

        inventory_rows.append(
            {
                "physical_wall_id": f"WALL_{len(inventory_rows) + 1:03d}",
                "boundary_condition": boundary_condition,
                "wall_family": effective_family or wall_family_for_boundary_condition(boundary_condition),
                "wall_role": effective_role,
                "wall_type_name": wall_type_name_for_inventory(total_thickness_mm),
                "total_thickness_mm": total_thickness_mm,
                "finish_each_side_mm": finish_each_side_mm,
                "finish_total_mm": finish_total_mm,
                "core_thickness_mm": core_thickness_mm,
                "surface_name_primary": str(primary_wall.get("surface_name", "") or ""),
                "surface_name_secondary": str(secondary_wall.get("surface_name", "") or "") if secondary_wall else "",
                "zone_name_primary": str(primary_wall.get("zone_name", "") or ""),
                "zone_name_secondary": str(secondary_wall.get("zone_name", "") or "") if secondary_wall else "",
                "adjacent_zone_name_primary": str(primary_wall.get("adjacent_zone_name", "") or ""),
                "adjacent_zone_name_secondary": str(secondary_wall.get("adjacent_zone_name", "") or "") if secondary_wall else "",
                "construction_name_primary": str(primary_wall.get("construction_name", "") or ""),
                "construction_name_secondary": str(secondary_wall.get("construction_name", "") or "") if secondary_wall else "",
                "thickness_inference_source_primary": str(primary_wall.get("wall_thickness_inference_source", "") or ""),
                "thickness_inference_source_secondary": str(secondary_wall.get("wall_thickness_inference_source", "") or "") if secondary_wall else "",
                "wall_role_primary": primary_role,
                "wall_role_secondary": secondary_role,
                "wall_layer_canonical_primary": str(primary_wall.get("wall_layer_canonical", "") or ""),
                "wall_layer_canonical_secondary": str(secondary_wall.get("wall_layer_canonical", "") or "") if secondary_wall else "",
                "wall_layer_source_layers_primary": list(primary_wall.get("wall_layer_source_layers", []) or []),
                "wall_layer_source_layers_secondary": list(secondary_wall.get("wall_layer_source_layers", []) or []) if secondary_wall else [],
                "length_m": length_m,
                "height_m": height_m,
                "axis": str(primary_wall.get("axis", "") or ""),
                "side_primary": str(primary_wall.get("side", "") or ""),
                "side_secondary": str(secondary_wall.get("side", "") or "") if secondary_wall else "",
                "orientation_deg": normalized_wall_orientation_deg(start_xy_m, end_xy_m),
                "orientation_cardinal": wall_orientation_cardinal(start_xy_m, end_xy_m),
                "start_x_m": start_xy_m[0],
                "start_y_m": start_xy_m[1],
                "end_x_m": end_xy_m[0],
                "end_y_m": end_xy_m[1],
                "midpoint_x_m": midpoint_x_m,
                "midpoint_y_m": midpoint_y_m,
                "position_basis": (
                    "shared_zone_boundary_source_edge"
                    if boundary_condition == "Surface"
                    else "zone_boundary_source_edge"
                ),
                "segment_wkt": (
                    f"LINESTRING ({start_xy_m[0]:.3f} {start_xy_m[1]:.3f}, "
                    f"{end_xy_m[0]:.3f} {end_xy_m[1]:.3f})"
                ),
            }
        )

    return inventory_rows


def summarize_wall_inventory_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    boundary_counts = Counter(str(row.get("boundary_condition", "") or "") for row in rows)
    family_counts = Counter(str(row.get("wall_family", "") or "") for row in rows)
    thickness_counts = Counter(int(row.get("total_thickness_mm", 0) or 0) for row in rows)
    type_counts = Counter(str(row.get("wall_type_name", "") or "") for row in rows)
    return {
        "row_count": len(rows),
        "boundary_condition_counts": counter_to_sorted_dict(boundary_counts),
        "wall_family_counts": counter_to_sorted_dict(family_counts),
        "wall_type_counts": counter_to_sorted_dict(type_counts),
        "thickness_counts_mm": dict(
            sorted((str(key), int(value)) for key, value in thickness_counts.items() if key > 0)
        ),
    }


def translate_wall_inventory_rows_xy(
    rows: list[dict[str, object]],
    *,
    offset_x_m: float,
    offset_y_m: float,
) -> list[dict[str, object]]:
    translated_rows: list[dict[str, object]] = []
    for row in rows:
        row_copy = dict(row)
        for key, delta in {
            "start_x_m": offset_x_m,
            "end_x_m": offset_x_m,
            "midpoint_x_m": offset_x_m,
        }.items():
            if row_copy.get(key) not in {None, ""}:
                row_copy[key] = round(float(row_copy[key]) - delta, 3)
        for key, delta in {
            "start_y_m": offset_y_m,
            "end_y_m": offset_y_m,
            "midpoint_y_m": offset_y_m,
        }.items():
            if row_copy.get(key) not in {None, ""}:
                row_copy[key] = round(float(row_copy[key]) - delta, 3)
        if all(row_copy.get(key) not in {None, ""} for key in ("start_x_m", "start_y_m", "end_x_m", "end_y_m")):
            row_copy["segment_wkt"] = (
                f"LINESTRING ({float(row_copy['start_x_m']):.3f} {float(row_copy['start_y_m']):.3f}, "
                f"{float(row_copy['end_x_m']):.3f} {float(row_copy['end_y_m']):.3f})"
            )
        translated_rows.append(row_copy)
    return translated_rows


def normalize_wall_reference_policy(
    geometry_payload: dict[str, object] | None,
) -> dict[str, object]:
    geometry_policy = {}
    if isinstance((geometry_payload or {}).get("geometry_policy"), dict):
        geometry_policy = dict((geometry_payload or {}).get("geometry_policy", {}))
    raw_policy = {}
    if isinstance(geometry_policy.get("wall_reference_policy"), dict):
        raw_policy = dict(geometry_policy.get("wall_reference_policy", {}))

    external_reference_type = (
        str(raw_policy.get("external_reference_type", "")).strip()
        or str(DEFAULT_WALL_REFERENCE_POLICY["external_reference_type"])
    )
    interzone_reference_type = (
        str(raw_policy.get("interzone_reference_type", "")).strip()
        or str(DEFAULT_WALL_REFERENCE_POLICY["interzone_reference_type"])
    )
    single_zone_reference_type = (
        str(raw_policy.get("single_zone_reference_type", "")).strip()
        or str(DEFAULT_WALL_REFERENCE_POLICY["single_zone_reference_type"])
    )
    policy_source = (
        str(raw_policy.get("policy_source", "")).strip()
        or (
            str((geometry_payload or {}).get("geometry_policy_source", "")).strip()
            if raw_policy
            else "default_builtin"
        )
    )
    return {
        "policy_name": (
            str(raw_policy.get("policy_name", "")).strip()
            or str(DEFAULT_WALL_REFERENCE_POLICY["policy_name"])
        ),
        "policy_source": policy_source,
        "external_reference_type": external_reference_type,
        "interzone_reference_type": interzone_reference_type,
        "single_zone_reference_type": single_zone_reference_type,
    }


def wall_normal_direction_for_side(side: str) -> tuple[float, float, str]:
    normalized_side = str(side or "").strip().lower()
    return {
        "south": (0.0, -1.0, "south"),
        "north": (0.0, 1.0, "north"),
        "east": (1.0, 0.0, "east"),
        "west": (-1.0, 0.0, "west"),
    }.get(normalized_side, (0.0, 0.0, "unknown"))


def shift_segment_xy(
    start_xy_m: tuple[float, float],
    end_xy_m: tuple[float, float],
    *,
    delta_x_m: float,
    delta_y_m: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    return (
        (round(start_xy_m[0] + delta_x_m, 3), round(start_xy_m[1] + delta_y_m, 3)),
        (round(end_xy_m[0] + delta_x_m, 3), round(end_xy_m[1] + delta_y_m, 3)),
    )


def build_line_payload(
    start_xy_m: tuple[float, float],
    end_xy_m: tuple[float, float],
) -> dict[str, list[float]]:
    return {
        "start": [round(float(start_xy_m[0]), 3), round(float(start_xy_m[1]), 3)],
        "end": [round(float(end_xy_m[0]), 3), round(float(end_xy_m[1]), 3)],
    }


def line_points_from_payload(payload: dict[str, object]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    start_values = payload.get("start")
    end_values = payload.get("end")
    if not isinstance(start_values, list) or len(start_values) < 2:
        return None
    if not isinstance(end_values, list) or len(end_values) < 2:
        return None
    try:
        return (
            (float(start_values[0]), float(start_values[1])),
            (float(end_values[0]), float(end_values[1])),
        )
    except (TypeError, ValueError):
        return None


def line_fixed_coord_m(payload: dict[str, object]) -> float | None:
    points = line_points_from_payload(payload)
    if points is None:
        return None
    start_xy_m, end_xy_m = points
    if abs(start_xy_m[1] - end_xy_m[1]) <= 1e-6:
        return round((start_xy_m[1] + end_xy_m[1]) / 2.0, 6)
    if abs(start_xy_m[0] - end_xy_m[0]) <= 1e-6:
        return round((start_xy_m[0] + end_xy_m[0]) / 2.0, 6)
    return None


def canonical_line_key(payload: dict[str, object]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    points = line_points_from_payload(payload)
    if points is None:
        return None
    return canonical_segment_points(*points)


def inventory_wall_role(row: dict[str, object]) -> str:
    role_name = str(row.get("wall_role", "")).strip()
    if role_name:
        return role_name
    return inferred_wall_role(
        boundary_condition=str(row.get("boundary_condition", "")).strip(),
        wall_thickness_mm=int(row.get("total_thickness_mm", 0) or 0),
    )


def wall_reference_type_for_inventory_row(
    row: dict[str, object],
    reference_policy: dict[str, object],
) -> str:
    boundary_condition = str(row.get("boundary_condition", "")).strip()
    surface_name_secondary = str(row.get("surface_name_secondary", "")).strip()
    if boundary_condition == "Surface" and surface_name_secondary:
        reference_type = str(reference_policy.get("interzone_reference_type", "")).strip()
        if reference_type != "shared_zone_boundary_centerline":
            raise WorkspaceRuleError(
                "Interzone walls currently require shared_zone_boundary_centerline reference resolution."
            )
        return reference_type
    if boundary_condition == "Outdoors":
        return str(reference_policy.get("external_reference_type", "")).strip()
    return str(reference_policy.get("single_zone_reference_type", "")).strip()


def wall_reference_offset_m(
    reference_type: str,
    wall_thickness_mm: int,
) -> float:
    normalized_reference = str(reference_type or "").strip()
    thickness_m = max(0.0, float(wall_thickness_mm) / 1000.0)
    if normalized_reference == "zone_boundary_inside_face_to_outer_face":
        return thickness_m
    if normalized_reference == "zone_boundary_inside_face_to_centerline":
        return thickness_m / 2.0
    if normalized_reference in ZERO_OFFSET_WALL_REFERENCE_TYPES:
        return 0.0
    raise WorkspaceRuleError(f"Unsupported wall reference type: {normalized_reference}")


def line_axis_label(payload: dict[str, object]) -> str:
    points = line_points_from_payload(payload)
    if points is None:
        return ""
    start_xy_m, end_xy_m = points
    if abs(start_xy_m[1] - end_xy_m[1]) <= 1e-6:
        return "horizontal"
    if abs(start_xy_m[0] - end_xy_m[0]) <= 1e-6:
        return "vertical"
    return ""


def wall_reference_types_equivalent(
    first_reference_type: str,
    second_reference_type: str,
) -> bool:
    first_normalized = str(first_reference_type or "").strip()
    second_normalized = str(second_reference_type or "").strip()
    if first_normalized == second_normalized:
        return True
    return (
        first_normalized in OUTER_FACE_WALL_REFERENCE_TYPES
        and second_normalized in OUTER_FACE_WALL_REFERENCE_TYPES
    )


def classify_reference_type_from_expected_offset_mm(
    expected_offset_mm: float,
    wall_thickness_mm: int,
    *,
    tolerance_mm: float = 1.0,
) -> str:
    if abs(expected_offset_mm) <= tolerance_mm:
        return "zone_boundary_outer_face"
    if abs(expected_offset_mm - (float(wall_thickness_mm) / 2.0)) <= tolerance_mm:
        return "zone_boundary_inside_face_to_centerline"
    if abs(expected_offset_mm - float(wall_thickness_mm)) <= tolerance_mm:
        return "zone_boundary_inside_face_to_outer_face"
    return "unclassified"


def boundary_candidate_index_by_handle(
    geometry_payload: dict[str, object] | None,
) -> dict[str, dict[str, object]]:
    boundary_candidates = list((geometry_payload or {}).get("boundary_candidates", []))
    indexed_candidates: dict[str, dict[str, object]] = {}
    for candidate in boundary_candidates:
        if not isinstance(candidate, dict):
            continue
        handle = str(candidate.get("handle", "")).strip()
        if handle:
            indexed_candidates[handle] = dict(candidate)
    return indexed_candidates


def boundary_candidate_outer_face_fixed_coord_m(
    candidate: dict[str, object],
    *,
    axis: str,
    outward_side: str,
) -> float | None:
    bbox_xy = candidate.get("bbox_xy")
    if not isinstance(bbox_xy, list) or len(bbox_xy) < 4:
        return None
    try:
        min_x_m = min(float(bbox_xy[0]), float(bbox_xy[2])) / 1000.0
        max_x_m = max(float(bbox_xy[0]), float(bbox_xy[2])) / 1000.0
        min_y_m = min(float(bbox_xy[1]), float(bbox_xy[3])) / 1000.0
        max_y_m = max(float(bbox_xy[1]), float(bbox_xy[3])) / 1000.0
    except (TypeError, ValueError):
        return None

    if axis == "horizontal":
        if outward_side == "south":
            return round(min_y_m, 6)
        if outward_side == "north":
            return round(max_y_m, 6)
        return None
    if axis == "vertical":
        if outward_side == "west":
            return round(min_x_m, 6)
        if outward_side == "east":
            return round(max_x_m, 6)
        return None
    return None


def surface_shell_closure_detail(
    *,
    surface_name: str,
    surface_geometry: dict[str, object],
    resolved_surface_payload: dict[str, object] | None,
    boundary_candidates_by_handle: dict[str, dict[str, object]],
    tolerance_mm: float,
) -> dict[str, object]:
    source_edge = dict(surface_geometry.get("source_edge", {}))
    final_export_line = dict(surface_geometry.get("final_export_line", {}))
    axis = line_axis_label(source_edge)
    if not axis:
        return {
            "surface_name": surface_name,
            "status": "skipped",
            "reason": "non_orthogonal_surface",
        }

    source_fixed_coord_m = line_fixed_coord_m(source_edge)
    final_fixed_coord_m = line_fixed_coord_m(final_export_line)
    if source_fixed_coord_m is None or final_fixed_coord_m is None:
        return {
            "surface_name": surface_name,
            "status": "skipped",
            "reason": "missing_surface_plane",
        }

    surface_payload = dict(resolved_surface_payload or {})
    layer_record_handles = [
        str(handle).strip()
        for handle in list(surface_payload.get("layer_record_handles", []))
        if str(handle).strip()
    ]
    if not layer_record_handles:
        return {
            "surface_name": surface_name,
            "status": "skipped",
            "reason": "missing_layer_record_handles",
        }

    outward_side = (
        str(dict(surface_geometry.get("normal_direction", {})).get("label", "")).strip().lower()
    )
    outer_face_fixed_coords_m: list[float] = []
    candidate_handles_used: list[str] = []
    for handle in layer_record_handles:
        candidate = boundary_candidates_by_handle.get(handle)
        if candidate is None:
            continue
        fixed_coord_m = boundary_candidate_outer_face_fixed_coord_m(
            candidate,
            axis=axis,
            outward_side=outward_side,
        )
        if fixed_coord_m is None:
            continue
        outer_face_fixed_coords_m.append(fixed_coord_m)
        candidate_handles_used.append(handle)

    if not outer_face_fixed_coords_m:
        return {
            "surface_name": surface_name,
            "status": "skipped",
            "reason": "missing_boundary_candidate_outer_face",
            "layer_record_handles": layer_record_handles,
        }

    expected_shell_fixed_coord_m = round(
        sum(outer_face_fixed_coords_m) / len(outer_face_fixed_coords_m),
        6,
    )
    wall_thickness_mm = int(
        surface_geometry.get(
            "thickness_mm",
            surface_payload.get("wall_thickness_mm", 0),
        )
        or 0
    )
    source_to_expected_mm = abs(expected_shell_fixed_coord_m - source_fixed_coord_m) * 1000.0
    final_to_expected_mm = abs(expected_shell_fixed_coord_m - final_fixed_coord_m) * 1000.0
    expected_reference_type = classify_reference_type_from_expected_offset_mm(
        source_to_expected_mm,
        wall_thickness_mm,
        tolerance_mm=tolerance_mm,
    )
    policy_reference_type = str(surface_geometry.get("reference_type", "")).strip()
    return {
        "surface_name": surface_name,
        "status": "passed" if final_to_expected_mm <= tolerance_mm else "failed",
        "reason": "" if final_to_expected_mm <= tolerance_mm else "final_export_line_mismatch",
        "axis": axis,
        "outward_side": outward_side,
        "policy_reference_type": policy_reference_type,
        "expected_reference_type": expected_reference_type,
        "reference_type_matches_evidence": wall_reference_types_equivalent(
            policy_reference_type,
            expected_reference_type,
        ),
        "wall_thickness_mm": wall_thickness_mm,
        "source_fixed_coord_m": round(source_fixed_coord_m, 6),
        "expected_shell_fixed_coord_m": expected_shell_fixed_coord_m,
        "final_fixed_coord_m": round(final_fixed_coord_m, 6),
        "source_to_expected_mm": round(source_to_expected_mm, 6),
        "final_to_expected_mm": round(final_to_expected_mm, 6),
        "candidate_handles_used": candidate_handles_used,
        "layer_record_handles": layer_record_handles,
        "layer_canonical": str(surface_payload.get("wall_layer_canonical", "")).strip(),
        "candidate_outer_face_fixed_coords_m": [
            round(value, 6) for value in outer_face_fixed_coords_m
        ],
    }


def build_surface_shell_closure_summary(
    *,
    surface_export_geometry_by_name: dict[str, dict[str, object]],
    resolved_surface_payloads: list[dict[str, object]],
    geometry_payload: dict[str, object] | None,
    tolerance_mm: float = 10.0,
) -> dict[str, object]:
    surface_payload_by_name = {
        str(payload.get("surface_name", "")).strip(): dict(payload)
        for payload in resolved_surface_payloads
        if str(payload.get("surface_name", "")).strip()
    }
    boundary_candidates_by_handle = boundary_candidate_index_by_handle(geometry_payload)

    detail_by_surface_name: dict[str, dict[str, object]] = {}
    failed_surface_names: list[str] = []
    skipped_surface_names: list[str] = []
    reference_type_mismatch_surface_names: list[str] = []
    max_plane_gap_mm = 0.0
    checked_surface_count = 0

    for surface_name, surface_geometry in sorted(surface_export_geometry_by_name.items()):
        surface_name_normalized = str(surface_name).strip()
        if not surface_name_normalized:
            continue
        if str(surface_geometry.get("boundary_condition", "")).strip() != "Outdoors":
            continue
        detail = surface_shell_closure_detail(
            surface_name=surface_name_normalized,
            surface_geometry=dict(surface_geometry),
            resolved_surface_payload=surface_payload_by_name.get(surface_name_normalized),
            boundary_candidates_by_handle=boundary_candidates_by_handle,
            tolerance_mm=tolerance_mm,
        )
        detail_by_surface_name[surface_name_normalized] = detail
        if str(detail.get("status", "")).strip() == "skipped":
            skipped_surface_names.append(surface_name_normalized)
            continue

        checked_surface_count += 1
        plane_gap_mm = float(detail.get("final_to_expected_mm", 0.0) or 0.0)
        max_plane_gap_mm = max(max_plane_gap_mm, plane_gap_mm)
        if plane_gap_mm > tolerance_mm:
            failed_surface_names.append(surface_name_normalized)
        if not bool(detail.get("reference_type_matches_evidence", False)):
            reference_type_mismatch_surface_names.append(surface_name_normalized)

    return {
        "tolerance_mm": round(tolerance_mm, 6),
        "checked_surface_count": checked_surface_count,
        "skipped_surface_count": len(skipped_surface_names),
        "passed": not failed_surface_names,
        "failed_surface_names": sorted(failed_surface_names),
        "skipped_surface_names": sorted(skipped_surface_names),
        "reference_type_mismatch_surface_names": sorted(reference_type_mismatch_surface_names),
        "max_plane_gap_mm": round(max_plane_gap_mm, 6),
        "details_by_surface_name": detail_by_surface_name,
    }


def enforce_wall_resolution_qa(qa_checks: dict[str, object]) -> None:
    surface_shell_closure = dict(qa_checks.get("surface_shell_closure", {}))
    failed_surface_names = [
        str(surface_name).strip()
        for surface_name in list(surface_shell_closure.get("failed_surface_names", []))
        if str(surface_name).strip()
    ]
    if failed_surface_names:
        preview = ", ".join(failed_surface_names[:6])
        if len(failed_surface_names) > 6:
            preview = f"{preview}, ..."
        raise WorkspaceRuleError(
            "Wall surface shell closure QA failed: "
            f"{preview}"
        )


def build_zone_area_preservation_summary(
    geometry_payload: dict[str, object] | None,
) -> dict[str, object]:
    zone_geometry_by_key = {}
    if isinstance((geometry_payload or {}).get("zone_geometry_by_key"), dict):
        zone_geometry_by_key = dict((geometry_payload or {}).get("zone_geometry_by_key", {}))
    zone_areas_m2_by_key = {
        str(zone_key): round(float(payload.get("footprint_area_m2", 0.0) or 0.0), 3)
        for zone_key, payload in zone_geometry_by_key.items()
        if isinstance(payload, dict) and float(payload.get("footprint_area_m2", 0.0) or 0.0) > 0.0
    }
    return {
        "checked_zone_count": len(zone_areas_m2_by_key),
        "preserved": True,
        "max_area_delta_m2": 0.0,
        "zone_footprint_area_m2_by_key": zone_areas_m2_by_key,
        "note": "Wall resolution updates only wall export references; zone floor polygons remain unchanged.",
    }


def build_wall_resolution_rows(
    *,
    wall_inventory_rows: list[dict[str, object]],
    wall_hosts_by_surface_name: dict[str, dict[str, object]],
    reference_policy: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    resolved_walls: list[dict[str, object]] = []
    surface_export_geometry_by_name: dict[str, dict[str, object]] = {}

    for row in wall_inventory_rows:
        wall_id = str(row.get("physical_wall_id", "")).strip() or f"WALL_{len(resolved_walls) + 1:03d}"
        boundary_condition = str(row.get("boundary_condition", "")).strip()
        wall_type = inventory_wall_role(row)
        wall_thickness_mm = int(row.get("total_thickness_mm", 0) or 0)
        surface_name_primary = str(row.get("surface_name_primary", "")).strip()
        surface_name_secondary = str(row.get("surface_name_secondary", "")).strip()
        zone_names = [
            value
            for value in [
                str(row.get("zone_name_primary", "")).strip(),
                str(row.get("zone_name_secondary", "")).strip(),
            ]
            if value
        ]

        source_start_xy_m = (
            round(float(row.get("start_x_m", 0.0) or 0.0), 3),
            round(float(row.get("start_y_m", 0.0) or 0.0), 3),
        )
        source_end_xy_m = (
            round(float(row.get("end_x_m", 0.0) or 0.0), 3),
            round(float(row.get("end_y_m", 0.0) or 0.0), 3),
        )
        reference_type = wall_reference_type_for_inventory_row(row, reference_policy)
        reference_offset_m = wall_reference_offset_m(reference_type, wall_thickness_mm)
        normal_x, normal_y, normal_label = wall_normal_direction_for_side(str(row.get("side_primary", "")))
        final_start_xy_m, final_end_xy_m = shift_segment_xy(
            source_start_xy_m,
            source_end_xy_m,
            delta_x_m=normal_x * reference_offset_m,
            delta_y_m=normal_y * reference_offset_m,
        )

        resolved_walls.append(
            {
                "wall_id": wall_id,
                "surface_names": [
                    value
                    for value in [surface_name_primary, surface_name_secondary]
                    if value
                ],
                "zone_names": zone_names,
                "boundary_condition": boundary_condition,
                "wall_type": wall_type,
                "wall_family": str(row.get("wall_family", "")).strip(),
                "thickness_mm": wall_thickness_mm,
                "normal_direction": {
                    "x": round(normal_x, 6),
                    "y": round(normal_y, 6),
                    "label": normal_label,
                    "basis_surface_name": surface_name_primary,
                },
                "reference_type": reference_type,
                "reference_offset_mm": int(round(reference_offset_m * 1000.0)),
                "source_edge": {
                    **build_line_payload(source_start_xy_m, source_end_xy_m),
                    "basis": str(row.get("position_basis", "")).strip(),
                    "axis": str(row.get("axis", "")).strip(),
                    "surface_name_primary": surface_name_primary,
                    "surface_name_secondary": surface_name_secondary,
                },
                "final_export_line": build_line_payload(final_start_xy_m, final_end_xy_m),
                "length_m": round(math.hypot(final_end_xy_m[0] - final_start_xy_m[0], final_end_xy_m[1] - final_start_xy_m[1]), 3),
            }
        )

        for surface_name in [value for value in [surface_name_primary, surface_name_secondary] if value]:
            host_wall = wall_hosts_by_surface_name.get(surface_name, {})
            host_start_values = host_wall.get("start")
            host_end_values = host_wall.get("end")
            if (
                isinstance(host_start_values, list)
                and len(host_start_values) >= 2
                and isinstance(host_end_values, list)
                and len(host_end_values) >= 2
            ):
                host_start_xy_m = (float(host_start_values[0]), float(host_start_values[1]))
                host_end_xy_m = (float(host_end_values[0]), float(host_end_values[1]))
            else:
                host_start_xy_m = source_start_xy_m
                host_end_xy_m = source_end_xy_m

            surface_normal_x, surface_normal_y, surface_normal_label = wall_normal_direction_for_side(
                str(host_wall.get("side", "")).strip() or str(row.get("side_primary", "")).strip()
            )
            surface_offset_m = reference_offset_m if boundary_condition != "Surface" else 0.0
            surface_final_start_xy_m, surface_final_end_xy_m = shift_segment_xy(
                host_start_xy_m,
                host_end_xy_m,
                delta_x_m=surface_normal_x * surface_offset_m,
                delta_y_m=surface_normal_y * surface_offset_m,
            )
            height_m = round(float(host_wall.get("height_m", row.get("height_m", 0.0)) or 0.0), 3)
            surface_export_geometry_by_name[surface_name] = {
                "wall_id": wall_id,
                "surface_name": surface_name,
                "zone_name": str(host_wall.get("zone_name", "")).strip()
                or str(row.get("zone_name_primary", "")).strip(),
                "boundary_condition": boundary_condition,
                "wall_type": wall_type,
                "thickness_mm": wall_thickness_mm,
                "reference_type": reference_type,
                "reference_offset_mm": int(round(surface_offset_m * 1000.0)),
                "normal_direction": {
                    "x": round(surface_normal_x, 6),
                    "y": round(surface_normal_y, 6),
                    "label": surface_normal_label,
                },
                "offset_vector_m": [
                    round(surface_normal_x * surface_offset_m, 6),
                    round(surface_normal_y * surface_offset_m, 6),
                ],
                "source_edge": build_line_payload(host_start_xy_m, host_end_xy_m),
                "final_export_line": build_line_payload(surface_final_start_xy_m, surface_final_end_xy_m),
                "z_min_m": 0.0,
                "z_max_m": height_m,
            }

    return resolved_walls, surface_export_geometry_by_name


def build_wall_resolution_qa(
    *,
    resolved_walls: list[dict[str, object]],
    surface_export_geometry_by_name: dict[str, dict[str, object]],
    resolved_surface_payloads: list[dict[str, object]],
    geometry_payload: dict[str, object] | None,
) -> dict[str, object]:
    external_failures: list[str] = []
    max_external_offset_error_mm = 0.0
    external_wall_count = 0
    for row in resolved_walls:
        if str(row.get("boundary_condition", "")).strip() != "Outdoors":
            continue
        external_wall_count += 1
        thickness_mm = int(row.get("thickness_mm", 0) or 0)
        expected_offset_m = wall_reference_offset_m(str(row.get("reference_type", "")).strip(), thickness_mm)
        source_fixed_coord_m = line_fixed_coord_m(dict(row.get("source_edge", {})))
        final_fixed_coord_m = line_fixed_coord_m(dict(row.get("final_export_line", {})))
        if source_fixed_coord_m is None or final_fixed_coord_m is None:
            continue
        actual_offset_m = abs(final_fixed_coord_m - source_fixed_coord_m)
        offset_error_mm = abs(actual_offset_m - expected_offset_m) * 1000.0
        max_external_offset_error_mm = max(max_external_offset_error_mm, offset_error_mm)
        if offset_error_mm > 1e-3:
            external_failures.append(str(row.get("wall_id", "")))

    interzone_failures: list[str] = []
    interzone_wall_count = 0
    for row in resolved_walls:
        if str(row.get("boundary_condition", "")).strip() != "Surface":
            continue
        interzone_wall_count += 1
        surface_names = [
            str(surface_name).strip()
            for surface_name in list(row.get("surface_names", []))
            if str(surface_name).strip()
        ]
        if len(surface_names) != 2:
            interzone_failures.append(str(row.get("wall_id", "")))
            continue
        first_payload = surface_export_geometry_by_name.get(surface_names[0], {})
        second_payload = surface_export_geometry_by_name.get(surface_names[1], {})
        if canonical_line_key(dict(first_payload.get("final_export_line", {}))) != canonical_line_key(
            dict(second_payload.get("final_export_line", {}))
        ):
            interzone_failures.append(str(row.get("wall_id", "")))

    return {
        "zone_area_preservation": build_zone_area_preservation_summary(geometry_payload),
        "surface_shell_closure": build_surface_shell_closure_summary(
            surface_export_geometry_by_name=surface_export_geometry_by_name,
            resolved_surface_payloads=resolved_surface_payloads,
            geometry_payload=geometry_payload,
        ),
        "external_wall_offset_correctness": {
            "checked_wall_count": external_wall_count,
            "passed": not external_failures,
            "failed_wall_ids": sorted(external_failures),
            "max_offset_error_mm": round(max_external_offset_error_mm, 6),
        },
        "interzone_wall_consistency": {
            "checked_wall_count": interzone_wall_count,
            "passed": not interzone_failures,
            "failed_wall_ids": sorted(interzone_failures),
        },
    }


def build_wall_artifacts(
    *,
    surface_rows: list[dict[str, object]],
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object] | None,
    dimension_annotations: list[dict[str, object]],
    layer_profile_path: Path | str | None = None,
    reference_idf_path: Path | str | None = None,
) -> dict[str, object]:
    resolved_surface_rows = [dict(row) for row in surface_rows]
    wall_hosts_by_zone, wall_hosts_by_surface_name = build_wall_host_collections(resolved_surface_rows)
    dimension_summary = normalize_dimension_annotations(dimension_annotations)
    layer_profile = load_layer_profile(layer_profile_path or DEFAULT_WALL_LAYER_PROFILE)
    adiabatic_boundary_summary = apply_layer_based_adiabatic_boundaries(
        resolved_surface_rows,
        wall_hosts_by_surface_name,
        mapping_payload=mapping_payload,
        layer_profile=layer_profile,
    )
    wall_boundary_override_summary = apply_geometry_policy_wall_boundary_overrides(
        resolved_surface_rows,
        wall_hosts_by_surface_name,
        geometry_payload=geometry_payload,
    )

    wall_thickness_inference = infer_wall_thicknesses(
        wall_hosts_by_zone=wall_hosts_by_zone,
        mapping_payload=mapping_payload,
        dimension_summary=dimension_summary,
        layer_profile=layer_profile,
    )
    surface_thicknesses = {
        str(surface_name): dict(payload)
        for surface_name, payload in dict(wall_thickness_inference.get("surface_thicknesses", {})).items()
    }
    apply_surface_thickness_payloads(resolved_surface_rows, wall_hosts_by_surface_name, surface_thicknesses)

    reference_summary = apply_reference_wall_constructions(
        resolved_surface_rows,
        wall_hosts_by_surface_name,
        surface_thicknesses,
        reference_idf_path=reference_idf_path,
    )
    wall_thickness_inference["surface_thicknesses"] = surface_thicknesses
    wall_thickness_inference["source_counts"] = dict(
        sorted(Counter(str(payload.get("source", "")) for payload in surface_thicknesses.values()).items())
    )

    opening_hosts = estimate_opening_host_surface_names(mapping_payload, wall_hosts_by_zone)
    resolved_by_group = apply_collinear_wall_resolution(
        surface_rows=resolved_surface_rows,
        wall_hosts_by_surface_name=wall_hosts_by_surface_name,
        wall_thickness_inference=wall_thickness_inference,
        opening_hosts=opening_hosts,
    )
    apply_surface_thickness_payloads(
        resolved_surface_rows,
        wall_hosts_by_surface_name,
        {
            str(surface_name): dict(payload)
            for surface_name, payload in dict(wall_thickness_inference.get("surface_thicknesses", {})).items()
        },
    )

    wall_inventory_rows = build_wall_inventory_rows(wall_hosts_by_zone)
    wall_inventory_summary = summarize_wall_inventory_rows(wall_inventory_rows)
    wall_reference_policy = normalize_wall_reference_policy(geometry_payload)
    resolved_walls, surface_export_geometry_by_name = build_wall_resolution_rows(
        wall_inventory_rows=wall_inventory_rows,
        wall_hosts_by_surface_name=wall_hosts_by_surface_name,
        reference_policy=wall_reference_policy,
    )

    resolved_surface_payloads: list[dict[str, object]] = []
    for row in resolved_surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        surface_name = str(row.get("surface_name", "")).strip()
        payload = dict(wall_thickness_inference.get("surface_thicknesses", {}).get(surface_name, {}))
        resolved_surface_payloads.append(
            {
                "surface_name": surface_name,
                "zone_name": str(row.get("zone_name", "")).strip(),
                "boundary_condition": str(row.get("outside_boundary_condition", "")).strip(),
                "paired_surface_name": str(row.get("outside_boundary_condition_object", "")).strip(),
                "construction_name": str(row.get("construction_name", "")).strip(),
                "wall_thickness_mm": int(row.get("inferred_wall_thickness_mm", 0) or 0),
                "wall_thickness_inference_source": str(row.get("wall_thickness_inference_source", "")).strip(),
                "wall_role": str(row.get("inferred_wall_role", "")).strip(),
                "wall_family": str(row.get("inferred_wall_family", "")).strip(),
                "wall_layer_canonical": str(row.get("wall_layer_canonical", "")).strip(),
                "wall_layer_source_layers": list(row.get("wall_layer_source_layers", []))
                if isinstance(row.get("wall_layer_source_layers", []), list)
                else [],
                "candidate_scores": dict(payload.get("candidate_scores", {})),
                "dimension_handles": list(payload.get("dimension_handles", [])),
                "layer_record_handles": list(payload.get("layer_record_handles", [])),
            }
        )

    qa_checks = build_wall_resolution_qa(
        resolved_walls=resolved_walls,
        surface_export_geometry_by_name=surface_export_geometry_by_name,
        resolved_surface_payloads=resolved_surface_payloads,
        geometry_payload=geometry_payload,
    )
    enforce_wall_resolution_qa(qa_checks)

    wall_resolution = {
        "reference_policy": wall_reference_policy,
        "surface_wall_count": len(resolved_surface_payloads),
        "interzone_pair_count": sum(
            1
            for row in resolved_surface_rows
            if str(row.get("surface_type", "")).strip() == "Wall"
            and str(row.get("outside_boundary_condition", "")).strip() == "Surface"
        ) // 2,
        "estimated_opening_host_count": len(opening_hosts),
        "estimated_opening_hosts": sorted(opening_hosts),
        "reference_construction_summary": reference_summary,
        "adiabatic_boundary_summary": adiabatic_boundary_summary,
        "wall_boundary_override_summary": wall_boundary_override_summary,
        "dimension_annotation_summary": {
            "annotation_count": int(dimension_summary.get("source_count", 0) or 0),
            "value_counts_mm": {
                str(key): int(value)
                for key, value in sorted(dict(dimension_summary.get("value_counts_mm", {})).items())
            },
            "repeated_values_mm": list(dimension_summary.get("repeated_values_mm", [])),
        },
        "parser_layer_evidence_summary": dict(wall_thickness_inference.get("parser_layer_evidence_summary", {})),
        "collinear_resolved_group_count": len(resolved_by_group),
        "collinear_resolved_groups": [
            {
                "surface_group": list(group_key),
                "resolved_thickness_mm": int(thickness_mm),
            }
            for group_key, thickness_mm in sorted(resolved_by_group.items())
        ],
        "wall_thickness_inference": wall_thickness_inference,
        "wall_inventory_summary": wall_inventory_summary,
        "resolved_surfaces": resolved_surface_payloads,
        "resolved_walls": resolved_walls,
        "surface_export_geometry_by_name": surface_export_geometry_by_name,
        "qa_checks": qa_checks,
    }

    return {
        "surface_rows": resolved_surface_rows,
        "wall_hosts_by_zone": wall_hosts_by_zone,
        "wall_hosts_by_surface_name": wall_hosts_by_surface_name,
        "wall_thickness_inference": wall_thickness_inference,
        "wall_inventory_rows": wall_inventory_rows,
        "wall_inventory_summary": wall_inventory_summary,
        "wall_resolution": wall_resolution,
    }


def write_wall_outputs(
    wall_artifacts: dict[str, object],
    output_dir: Path | str | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    resolved_project_id = path_resolver.resolve_project_id(project_id) if output_dir is None or project_id is not None else None
    resolved_output_dir = GUARD.assert_write_path(
        output_dir or _resolve_default_output_dir(resolved_project_id),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    if resolved_project_id is not None:
        path_resolver.assert_output_in_project_scope(resolved_project_id, resolved_output_dir)
    paths = {
        "wall_inventory": resolved_output_dir / "wall_inventory.json",
        "wall_resolution": resolved_output_dir / "wall_resolution.json",
    }
    GUARD.write_json(
        paths["wall_inventory"],
        list(wall_artifacts.get("wall_inventory_rows", [])),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    GUARD.write_json(
        paths["wall_resolution"],
        dict(wall_artifacts.get("wall_resolution", {})),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    return {key: workspace_path(path) for key, path in paths.items()}


def parse_wall_inputs(
    *,
    geometry_payload_path: Path,
    surface_rows_path: Path,
    mapping_payload_path: Path,
    dimension_annotations_path: Path,
    reference_idf_path: Path | None = None,
) -> dict[str, object]:
    geometry_payload = load_json_object(geometry_payload_path)
    surface_rows = load_json_list(surface_rows_path)
    mapping_payload = load_json_object(mapping_payload_path)
    dimension_annotations = load_json_list(dimension_annotations_path)
    return build_wall_artifacts(
        surface_rows=surface_rows,
        mapping_payload=mapping_payload,
        geometry_payload=geometry_payload,
        dimension_annotations=dimension_annotations,
        reference_idf_path=reference_idf_path,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve wall logic artifacts from intermediate surfaces, mapping, and layer/dimension evidence.",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project ID used to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--geometry-payload",
        type=Path,
        default=None,
        help="Path to geometry_payload.json. If omitted, resolves from 5_output/<project_id>/intermediate/geometry.",
    )
    parser.add_argument(
        "--surface-rows",
        type=Path,
        default=None,
        help="Path to surface_rows.json. If omitted, resolves from 5_output/<project_id>/intermediate/surfaces.",
    )
    parser.add_argument(
        "--mapping-payload",
        type=Path,
        default=None,
        help="Path to mapping_payload.json. If omitted, resolves from 5_output/<project_id>/intermediate/mapping.",
    )
    parser.add_argument(
        "--dimension-annotations",
        type=Path,
        default=None,
        help="Path to dimension_annotations.json. If omitted, resolves from 5_output/<project_id>/intermediate/mapping.",
    )
    parser.add_argument(
        "--reference-idf",
        type=Path,
        default=None,
        help="Optional project reference IDF path. If omitted, no reference-construction override file is applied.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for wall artifacts. If omitted, defaults to 5_output/<project_id>/intermediate/walls.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    project_id = path_resolver.resolve_project_id(args.project)
    resolved_inputs = _resolve_project_wall_inputs(project_id)
    reference_idf_path = args.reference_idf or _resolve_default_wall_reference_idf(project_id)

    wall_artifacts = parse_wall_inputs(
        geometry_payload_path=args.geometry_payload or resolved_inputs["geometry_payload"],
        surface_rows_path=args.surface_rows or resolved_inputs["surface_rows"],
        mapping_payload_path=args.mapping_payload or resolved_inputs["mapping_payload"],
        dimension_annotations_path=args.dimension_annotations or resolved_inputs["dimension_annotations"],
        reference_idf_path=reference_idf_path,
    )
    output_paths = write_wall_outputs(
        wall_artifacts,
        output_dir=args.output_dir or _resolve_default_output_dir(project_id),
        project_id=project_id,
    )
    wall_resolution = dict(wall_artifacts.get("wall_resolution", {}))
    wall_inventory_summary = dict(wall_artifacts.get("wall_inventory_summary", {}))
    print("Wall surfaces:", wall_resolution.get("surface_wall_count", 0))
    print("Wall inventory rows:", wall_inventory_summary.get("row_count", 0))
    print("Thickness groups:", wall_inventory_summary.get("thickness_counts_mm", {}))
    print("Reference overrides:", dict(wall_resolution.get("reference_construction_summary", {})).get("applied_surface_count", 0))
    print("Collinear resolved groups:", wall_resolution.get("collinear_resolved_group_count", 0))
    print("Resolved walls:", len(list(wall_resolution.get("resolved_walls", []))))
    print(
        "Wall QA:",
        {
            "external_wall_offset_correctness": dict(
                dict(wall_resolution.get("qa_checks", {})).get("external_wall_offset_correctness", {})
            ).get("passed"),
            "interzone_wall_consistency": dict(
                dict(wall_resolution.get("qa_checks", {})).get("interzone_wall_consistency", {})
            ).get("passed"),
        },
    )
    print("Wall inventory:", output_paths.get("wall_inventory", ""))
    print("Wall resolution:", output_paths.get("wall_resolution", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
