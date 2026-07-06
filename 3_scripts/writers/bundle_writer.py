#!/usr/bin/env python3
"""
Assemble final CSV bundle tables and bundle manifest from intermediate artifacts.

This writer works only from intermediate payloads and reference/library files. It
does not parse raw DXF, does not infer geometry, does
not resolve wall thickness, does not choose host walls, and does not rebuild IDF
directly.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import unicodedata
import warnings
from collections import Counter
from pathlib import Path
from typing import Any
import re

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transformers.wall_logic import (  # noqa: E402
    build_positions_csv,
    counter_to_sorted_dict,
    load_input_wall_construction_library,
    translate_wall_inventory_rows_xy,
)
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils.envelope_library import SIMPLE_GLAZING_FIELDS, apply_envelope_to_bundle_tables  # noqa: E402
from utils import path_resolver  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_MAPPING_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "mapping_payload.json"
DEFAULT_GEOMETRY_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "geometry" / "geometry_payload.json"
DEFAULT_SURFACE_ROWS = Path("5_output") / "<project_id>" / "intermediate" / "surfaces" / "surface_rows.json"
DEFAULT_WALL_INVENTORY = Path("5_output") / "<project_id>" / "intermediate" / "walls" / "wall_inventory.json"
DEFAULT_WALL_RESOLUTION = Path("5_output") / "<project_id>" / "intermediate" / "walls" / "wall_resolution.json"
DEFAULT_FENESTRATION_ROWS = Path("5_output") / "<project_id>" / "intermediate" / "fenestration" / "fenestration_rows.json"
DEFAULT_OPENING_HOST_MAPPING = Path("5_output") / "<project_id>" / "intermediate" / "fenestration" / "opening_host_mapping.json"
DEFAULT_OUTPUT_DIR = Path("5_output") / "<project_id>" / "csv" / "<project_id>_idf_input_bundle"
SHARED_SAMPLE_IDF_TEMPLATE = ROOT / "5_output" / "_shared" / "idf" / "Test1_for_DB_import_DBlean_crosscheck.idf"
LEGACY_SAMPLE_IDF_TEMPLATE = ROOT / "5_output" / "idf" / "Test1_for_DB_import_DBlean_crosscheck.idf"
CONSTRUCTION_LAYER_FIELDS = [f"layer_{index}" for index in range(1, 7)]
ADIABATIC_HALF_CONSTRUCTION_SUFFIX = "_AdiabaticHalf"
BUILDING_SURFACE_VERTEX_FIELDS = [
    field_name
    for index in range(1, 13)
    for field_name in (f"v{index}_x", f"v{index}_y", f"v{index}_z")
]
DEFAULT_IDF_COORDINATE_SYSTEM = "World"

IDF_BUNDLE_TABLE_FIELDS: dict[str, list[str]] = {
    "Version.csv": ["version_identifier"],
    "Site_Location.csv": ["location_name", "latitude", "longitude", "time_zone", "elevation_m"],
    "Building.csv": [
        "building_name",
        "north_axis_deg",
        "terrain",
        "loads_convergence_tolerance",
        "temperature_convergence_tolerance",
        "solar_distribution",
        "maximum_warmup_days",
        "minimum_warmup_days",
    ],
    "GlobalGeometryRules.csv": [
        "starting_vertex_position",
        "vertex_entry_direction",
        "coordinate_system",
    ],
    "Zone.csv": [
        "zone_name",
        "relative_north_deg",
        "x_origin_m",
        "y_origin_m",
        "z_origin_m",
        "zone_type",
        "zone_multiplier",
        "ceiling_height_m",
        "volume_m3",
        "floor_area_m2",
        "inside_convection_algorithm",
        "outside_convection_algorithm",
        "part_of_total_floor_area",
    ],
    "BuildingSurface_Detailed.csv": [
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
        *BUILDING_SURFACE_VERTEX_FIELDS,
    ],
    "FenestrationSurface_Detailed.csv": [
        "fenestration_name",
        "surface_type",
        "construction_name",
        "building_surface_name",
        "outside_boundary_condition_object",
        "view_factor_to_ground",
        "frame_and_divider_name",
        "multiplier",
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
    ],
    "Construction.csv": ["construction_name", *CONSTRUCTION_LAYER_FIELDS],
    "Material.csv": [
        "material_name",
        "roughness",
        "thickness_m",
        "conductivity_w_per_mk",
        "density_kg_per_m3",
        "specific_heat_j_per_kgk",
        "thermal_emittance",
        "solar_absorptance",
        "visible_absorptance",
    ],
    "WindowMaterial_Gas.csv": ["gas_layer_name", "gas_type", "thickness_m"],
    "WindowMaterial_SimpleGlazingSystem.csv": SIMPLE_GLAZING_FIELDS,
    "WindowMaterial_Glazing.csv": [
        "glazing_name",
        "optical_data_type",
        "spectral_data_set_name",
        "thickness_m",
        "solar_transmittance",
        "solar_reflectance_front",
        "solar_reflectance_back",
        "visible_transmittance",
        "visible_reflectance_front",
        "visible_reflectance_back",
        "ir_transmittance",
        "ir_emissivity_front",
        "ir_emissivity_back",
        "conductivity_w_per_mk",
        "dirt_correction_factor",
    ],
    "WindowProperty_FrameAndDivider.csv": [
        "frame_divider_name",
        "frame_width_m",
        "frame_outside_projection_m",
        "frame_inside_projection_m",
        "frame_conductance_w_per_m2k",
        "frame_edge_to_center_glass_conductance_ratio",
        "frame_solar_absorptance",
        "frame_visible_absorptance",
        "frame_thermal_emissivity",
        "divider_type",
        "divider_width_m",
        "number_horizontal_dividers",
        "number_vertical_dividers",
        "divider_outside_projection_m",
        "divider_inside_projection_m",
        "divider_conductance_w_per_m2k",
        "divider_edge_to_center_glass_conductance_ratio",
        "divider_solar_absorptance",
        "divider_visible_absorptance",
        "divider_thermal_emissivity",
        "outside_reveal_solar_absorptance",
        "inside_sill_depth_m",
        "inside_sill_solar_absorptance",
        "inside_reveal_depth_m",
        "inside_reveal_solar_absorptance",
    ],
}

WALL_INVENTORY_FIELDS = [
    "physical_wall_id",
    "boundary_condition",
    "wall_family",
    "wall_type_name",
    "total_thickness_mm",
    "finish_each_side_mm",
    "finish_total_mm",
    "core_thickness_mm",
    "surface_name_primary",
    "surface_name_secondary",
    "zone_name_primary",
    "zone_name_secondary",
    "adjacent_zone_name_primary",
    "adjacent_zone_name_secondary",
    "construction_name_primary",
    "construction_name_secondary",
    "thickness_inference_source_primary",
    "thickness_inference_source_secondary",
    "length_m",
    "height_m",
    "axis",
    "side_primary",
    "side_secondary",
    "orientation_deg",
    "orientation_cardinal",
    "start_x_m",
    "start_y_m",
    "end_x_m",
    "end_y_m",
    "midpoint_x_m",
    "midpoint_y_m",
    "position_basis",
    "segment_wkt",
]

SAMPLE_MATERIAL_ROWS = [
    {"material_name": "External Rendering_.025", "roughness": "Rough", "thickness_m": ".025", "conductivity_w_per_mk": "0.5", "density_kg_per_m3": "1300", "specific_heat_j_per_kgk": "1000", "thermal_emittance": "0.9", "solar_absorptance": "0.7", "visible_absorptance": "0.7"},
    {"material_name": "MW Stone Wool (rolls)_.1025", "roughness": "Rough", "thickness_m": ".1025", "conductivity_w_per_mk": "0.04", "density_kg_per_m3": "30", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Timber Flooring_.005", "roughness": "Rough", "thickness_m": ".005", "conductivity_w_per_mk": "0.14", "density_kg_per_m3": "650", "specific_heat_j_per_kgk": "1200", "thermal_emittance": "0.9", "solar_absorptance": "0.78", "visible_absorptance": "0.78"},
    {"material_name": "Cast Concrete (Dense)_.1", "roughness": "Rough", "thickness_m": ".1", "conductivity_w_per_mk": "1.4", "density_kg_per_m3": "2100", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Cement/plaster/mortar - gypsum plaster_.015", "roughness": "Rough", "thickness_m": ".015", "conductivity_w_per_mk": "0.51", "density_kg_per_m3": "1120", "specific_heat_j_per_kgk": "960", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Brick_.11", "roughness": "Rough", "thickness_m": ".11", "conductivity_w_per_mk": "0.72", "density_kg_per_m3": "1920", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Painted Oak_.035", "roughness": "Rough", "thickness_m": ".035", "conductivity_w_per_mk": "0.19", "density_kg_per_m3": "700", "specific_heat_j_per_kgk": "2390", "thermal_emittance": "0.9", "solar_absorptance": "0.5", "visible_absorptance": "0.5"},
    {"material_name": "Cement/plaster/mortar - cement plaster_.015", "roughness": "Rough", "thickness_m": ".015", "conductivity_w_per_mk": "0.72", "density_kg_per_m3": "1760", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Brick_.22", "roughness": "Rough", "thickness_m": ".22", "conductivity_w_per_mk": "0.72", "density_kg_per_m3": "1920", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Brick_.15", "roughness": "Rough", "thickness_m": ".15", "conductivity_w_per_mk": "0.72", "density_kg_per_m3": "1920", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Aerated Concrete Slab_.125", "roughness": "Rough", "thickness_m": ".125", "conductivity_w_per_mk": "0.16", "density_kg_per_m3": "500", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
    {"material_name": "Aerated Concrete Slab_.055", "roughness": "Rough", "thickness_m": ".055", "conductivity_w_per_mk": "0.16", "density_kg_per_m3": "500", "specific_heat_j_per_kgk": "840", "thermal_emittance": "0.9", "solar_absorptance": "0.6", "visible_absorptance": "0.6"},
]

SAMPLE_GLAZING_ROWS = [
    {"glazing_name": "3", "optical_data_type": "SpectralAverage", "spectral_data_set_name": "", "thickness_m": ".006", "solar_transmittance": ".775", "solar_reflectance_front": ".071", "solar_reflectance_back": ".071", "visible_transmittance": ".881", "visible_reflectance_front": ".080", "visible_reflectance_back": ".080", "ir_transmittance": ".0", "ir_emissivity_front": ".84", "ir_emissivity_back": ".84", "conductivity_w_per_mk": ".9", "dirt_correction_factor": "1"},
    {"glazing_name": "44", "optical_data_type": "SpectralAverage", "spectral_data_set_name": "", "thickness_m": ".006", "solar_transmittance": ".360", "solar_reflectance_front": ".093", "solar_reflectance_back": ".200", "visible_transmittance": ".500", "visible_reflectance_front": ".035", "visible_reflectance_back": ".054", "ir_transmittance": ".0", "ir_emissivity_front": ".84", "ir_emissivity_back": ".10", "conductivity_w_per_mk": ".9", "dirt_correction_factor": "1"},
    {"glazing_name": "100", "optical_data_type": "SpectralAverage", "spectral_data_set_name": "", "thickness_m": ".003", "solar_transmittance": ".99", "solar_reflectance_front": ".005", "solar_reflectance_back": ".005", "visible_transmittance": ".99", "visible_reflectance_front": ".005", "visible_reflectance_back": ".005", "ir_transmittance": ".99", "ir_emissivity_front": ".005", "ir_emissivity_back": ".005", "conductivity_w_per_mk": "5", "dirt_correction_factor": "1"},
]

SAMPLE_GAS_ROWS = [{"gas_layer_name": "1004", "gas_type": "Argon", "thickness_m": ".013"}]

SAMPLE_FRAME_ROWS = [
    {
        "frame_divider_name": "1",
        "frame_width_m": "0.04",
        "frame_outside_projection_m": "0.0",
        "frame_inside_projection_m": "0.0",
        "frame_conductance_w_per_m2k": "9.5",
        "frame_edge_to_center_glass_conductance_ratio": "1.0",
        "frame_solar_absorptance": "0.5",
        "frame_visible_absorptance": "0.5",
        "frame_thermal_emissivity": "0.9",
        "divider_type": "DividedLite",
        "divider_width_m": "0.020",
        "number_horizontal_dividers": "1",
        "number_vertical_dividers": "1",
        "divider_outside_projection_m": "0.0",
        "divider_inside_projection_m": "0.0",
        "divider_conductance_w_per_m2k": "9.5",
        "divider_edge_to_center_glass_conductance_ratio": "1.0",
        "divider_solar_absorptance": "0.5",
        "divider_visible_absorptance": "0.5",
        "divider_thermal_emissivity": "0.9",
        "outside_reveal_solar_absorptance": "0.5",
        "inside_sill_depth_m": "0.0",
        "inside_sill_solar_absorptance": "0.5",
        "inside_reveal_depth_m": "0.0",
        "inside_reveal_solar_absorptance": "0.5",
    },
]

SAMPLE_CONSTRUCTION_ROWS = [
    {"construction_name": "Project external floor", "layer_1": "External Rendering_.025", "layer_2": "MW Stone Wool (rolls)_.1025", "layer_3": "Timber Flooring_.005"},
    {"construction_name": "Project internal floor_Reversed", "layer_1": "Cast Concrete (Dense)_.1", "layer_2": "", "layer_3": ""},
    {"construction_name": "Project internal door", "layer_1": "Painted Oak_.035", "layer_2": "", "layer_3": ""},
    {"construction_name": "Project internal door_Rev", "layer_1": "Painted Oak_.035", "layer_2": "", "layer_3": ""},
    {"construction_name": "Project external door", "layer_1": "Painted Oak_.035", "layer_2": "", "layer_3": ""},
    {"construction_name": "Dbl LoE (e2=.1) Tint 6mm/13mm Arg - 1001", "layer_1": "44", "layer_2": "1004", "layer_3": "3"},
    {"construction_name": "Perfectly Clear - 1002", "layer_1": "100", "layer_2": "", "layer_3": ""},
]


def workspace_path(path: Path | str) -> str:
    resolved = Path(path)
    try:
        return str(resolved.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


def _resolve_required_bundle_inputs(project_id: str) -> dict[str, Path]:
    resolved = {
        "mapping_payload": path_resolver.resolve_output_file_for_read(project_id, "intermediate/mapping", "mapping_payload.json"),
        "geometry_payload": path_resolver.resolve_output_file_for_read(project_id, "intermediate/geometry", "geometry_payload.json"),
        "surface_rows": path_resolver.resolve_output_file_for_read(project_id, "intermediate/surfaces", "surface_rows.json"),
        "wall_inventory": path_resolver.resolve_output_file_for_read(project_id, "intermediate/walls", "wall_inventory.json"),
        "wall_resolution": path_resolver.resolve_output_file_for_read(project_id, "intermediate/walls", "wall_resolution.json"),
        "fenestration_rows": path_resolver.resolve_output_file_for_read(project_id, "intermediate/fenestration", "fenestration_rows.json"),
    }
    missing = [name for name, value in resolved.items() if value is None]
    if missing:
        raise WorkspaceRuleError(
            f"Missing required bundle inputs for project '{project_id}': {', '.join(sorted(missing))}"
        )
    return {name: value for name, value in resolved.items() if value is not None}


def _resolve_default_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "csv", f"{project_id}_idf_input_bundle")


def _resolve_opening_host_mapping(project_id: str) -> Path | None:
    return path_resolver.resolve_output_file_for_read(project_id, "intermediate/fenestration", "opening_host_mapping.json")


def _resolve_sample_idf_template() -> Path | None:
    if SHARED_SAMPLE_IDF_TEMPLATE.exists():
        return SHARED_SAMPLE_IDF_TEMPLATE
    if LEGACY_SAMPLE_IDF_TEMPLATE.exists():
        warnings.warn(
            "deprecated output layout: using legacy shared sample IDF template from 5_output/idf",
            DeprecationWarning,
            stacklevel=2,
        )
        return LEGACY_SAMPLE_IDF_TEMPLATE
    return None


def _infer_project_id_from_output_dir(output_dir: Path) -> str:
    output_root = ROOT / "5_output"
    try:
        relative = output_dir.resolve().relative_to(output_root.resolve())
    except ValueError as exc:
        raise WorkspaceRuleError(f"Bundle output dir must stay inside 5_output: {output_dir}") from exc
    if len(relative.parts) >= 3 and relative.parts[1] == "csv" and relative.parts[0] not in path_resolver.GLOBAL_OUTPUT_CATEGORIES:
        return relative.parts[0]
    raise WorkspaceRuleError(
        f"Bundle output dir must live under 5_output/<project_id>/csv/: {workspace_path(output_dir)}"
    )


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


def render_csv_text(fieldnames: list[str], rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fieldnames})
    return buffer.getvalue()


def ascii_token(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("+", "_")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    ascii_text = re.sub(r"_+", "_", ascii_text).strip("_")
    return ascii_text.upper() or "UNNAMED"


def canonical_zone_key(text: str) -> str:
    value = str(text or "").split(":")[-1].strip()
    token = ascii_token(value)
    token = {
        "PKXPB": "PK_PB",
        "PKPB": "PK_PB",
        "PNPK": "PN_PK",
        "LOGIA": "LOGIA",
    }.get(token, token)
    logia_match = re.fullmatch(r"LOGIA_?0?(\d{1,2})", token)
    if logia_match:
        return f"LOGIA_{int(logia_match.group(1)):02d}"
    compact_match = re.fullmatch(r"(PN|WC)(\d{1,2})", token)
    if compact_match:
        return f"{compact_match.group(1)}_{int(compact_match.group(2)):02d}"
    spaced_match = re.fullmatch(r"(PN|WC)_0?(\d{1,2})", token)
    if spaced_match:
        return f"{spaced_match.group(1)}_{int(spaced_match.group(2)):02d}"
    return token


def fields_for_idf_object(object_type: str) -> list[str]:
    lookup = {
        "Zone": IDF_BUNDLE_TABLE_FIELDS["Zone.csv"],
        "BuildingSurface:Detailed": IDF_BUNDLE_TABLE_FIELDS["BuildingSurface_Detailed.csv"],
        "FenestrationSurface:Detailed": IDF_BUNDLE_TABLE_FIELDS["FenestrationSurface_Detailed.csv"],
    }
    return lookup[object_type]


def parse_idf_objects(path: Path, object_types: list[str]) -> dict[str, list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    requested = set(object_types)
    rows_by_type: dict[str, list[dict[str, str]]] = {object_type: [] for object_type in object_types}
    current_tokens: list[str] = []
    current_value_chars: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("!", 1)[0]
        for character in line:
            if character in {",", ";"}:
                current_tokens.append("".join(current_value_chars).strip())
                current_value_chars = []
                if character == ";":
                    if current_tokens:
                        object_type = current_tokens[0]
                        if object_type in requested:
                            fields = fields_for_idf_object(object_type)
                            values = current_tokens[1:]
                            padded_values = values + [""] * max(0, len(fields) - len(values))
                            rows_by_type[object_type].append(
                                {field: padded_values[index] for index, field in enumerate(fields)}
                            )
                    current_tokens = []
            else:
                current_value_chars.append(character)
        current_value_chars.append(" ")
    return rows_by_type


def load_sample_geometry_template(
    zone_name_map: dict[str, str],
) -> dict[str, list[dict[str, object]]] | None:
    sample_idf_template = _resolve_sample_idf_template()
    if sample_idf_template is None:
        return None
    parsed = parse_idf_objects(
        sample_idf_template,
        ["Zone", "BuildingSurface:Detailed", "FenestrationSurface:Detailed"],
    )
    extracted_zone_names_by_key = {
        canonical_zone_key(source_zone_name): csv_zone_name
        for source_zone_name, csv_zone_name in zone_name_map.items()
    }
    sample_to_current_zone_names: dict[str, str] = {}
    zone_rows: list[dict[str, object]] = []
    for row in parsed["Zone"]:
        sample_zone_name = row.get("zone_name", "")
        csv_zone_name = extracted_zone_names_by_key.get(canonical_zone_key(sample_zone_name), "")
        if not csv_zone_name:
            continue
        sample_to_current_zone_names[sample_zone_name] = csv_zone_name
        row_copy = row.copy()
        row_copy["zone_name"] = csv_zone_name
        zone_rows.append(row_copy)
    if not zone_rows:
        return None
    building_surface_rows: list[dict[str, object]] = []
    kept_surface_names: set[str] = set()
    for row in parsed["BuildingSurface:Detailed"]:
        sample_zone_name = row.get("zone_name", "")
        csv_zone_name = sample_to_current_zone_names.get(sample_zone_name, "")
        if not csv_zone_name:
            continue
        row_copy = row.copy()
        row_copy["zone_name"] = csv_zone_name
        building_surface_rows.append(row_copy)
        kept_surface_names.add(str(row_copy.get("surface_name", "")))
    fenestration_rows: list[dict[str, object]] = []
    for row in parsed["FenestrationSurface:Detailed"]:
        if row.get("building_surface_name", "") not in kept_surface_names:
            continue
        fenestration_rows.append(row.copy())
    return {
        "zone_rows": zone_rows,
        "building_surface_rows": building_surface_rows,
        "fenestration_rows": fenestration_rows,
    }


def recommended_fenestration_construction(surface_type: str) -> str:
    if surface_type == "Door":
        return "Project external door"
    if surface_type == "GlassDoor":
        return "Perfectly Clear - 1002"
    return "Dbl LoE (e2=.1) Tint 6mm/13mm Arg - 1001"


def dxf_wall_core_material_name(core_thickness_mm: int) -> str:
    return f"DXF Wall Core {int(core_thickness_mm)}mm"


def dxf_wall_core_material_row(core_thickness_mm: int) -> dict[str, str]:
    return {
        "material_name": dxf_wall_core_material_name(core_thickness_mm),
        "roughness": "Rough",
        "thickness_m": f"{(int(core_thickness_mm) / 1000.0):.3f}",
        "conductivity_w_per_mk": "0.72",
        "density_kg_per_m3": "1920",
        "specific_heat_j_per_kgk": "840",
        "thermal_emittance": "0.9",
        "solar_absorptance": "0.6",
        "visible_absorptance": "0.6",
    }


def parse_material_thickness_m(material_row: dict[str, object]) -> float | None:
    value = str(material_row.get("thickness_m", "")).strip()
    if not value:
        return None
    try:
        thickness_m = float(value)
    except ValueError:
        return None
    return thickness_m if thickness_m > 0.0 else None


def construction_layer_names(row: dict[str, object]) -> list[str]:
    return [
        str(row.get(field_name, "")).strip()
        for field_name in CONSTRUCTION_LAYER_FIELDS
        if str(row.get(field_name, "")).strip()
    ]


def construction_is_symmetric(layer_names: list[str]) -> bool:
    return bool(layer_names) and layer_names == list(reversed(layer_names))


def adiabatic_partial_material_name(material_name: str, thickness_m: float) -> str:
    thickness_mm = int(round(float(thickness_m) * 1000.0))
    return f"{ascii_token(material_name)}_ADIABATIC_{thickness_mm}MM"


def material_row_with_thickness(
    material_row: dict[str, object],
    *,
    material_name: str,
    thickness_m: float,
) -> dict[str, str]:
    copied = {field_name: str(material_row.get(field_name, "")).strip() for field_name in IDF_BUNDLE_TABLE_FIELDS["Material.csv"]}
    copied["material_name"] = material_name
    copied["thickness_m"] = f"{float(thickness_m):.3f}"
    return copied


def build_symmetric_adiabatic_half_construction(
    *,
    construction_name: str,
    layer_names: list[str],
    material_lookup: dict[str, dict[str, object]],
    material_names: set[str],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    if not construction_is_symmetric(layer_names):
        raise WorkspaceRuleError(
            "Adiabatic construction requires confirmation before export: "
            f"{construction_name} is not symmetric, so the script cannot infer which side is inside."
        )

    layer_thicknesses_m: list[float] = []
    for material_name in layer_names:
        material_row = material_lookup.get(material_name)
        if material_row is None:
            raise WorkspaceRuleError(
                f"Adiabatic construction {construction_name} references unknown material: {material_name}"
            )
        thickness_m = parse_material_thickness_m(material_row)
        if thickness_m is None:
            raise WorkspaceRuleError(
                f"Adiabatic construction {construction_name} has material without positive thickness: {material_name}"
            )
        layer_thicknesses_m.append(thickness_m)

    half_target_m = sum(layer_thicknesses_m) / 2.0
    remaining_m = half_target_m
    half_layers: list[str] = []
    generated_material_rows: list[dict[str, str]] = []
    tolerance_m = 1e-9
    for material_name, thickness_m in zip(layer_names, layer_thicknesses_m, strict=True):
        if remaining_m <= tolerance_m:
            break
        if thickness_m <= remaining_m + tolerance_m:
            half_layers.append(material_name)
            remaining_m -= thickness_m
            continue

        partial_name = adiabatic_partial_material_name(material_name, remaining_m)
        half_layers.append(partial_name)
        if partial_name not in material_names:
            generated_material_rows.append(
                material_row_with_thickness(
                    material_lookup[material_name],
                    material_name=partial_name,
                    thickness_m=remaining_m,
                )
            )
            material_names.add(partial_name)
        remaining_m = 0.0

    half_construction_name = f"{construction_name}{ADIABATIC_HALF_CONSTRUCTION_SUFFIX}"
    construction_row = {"construction_name": half_construction_name}
    for field_name in CONSTRUCTION_LAYER_FIELDS:
        construction_row[field_name] = ""
    for index, material_name in enumerate(half_layers, start=1):
        construction_row[f"layer_{index}"] = material_name
    return construction_row, generated_material_rows


def apply_adiabatic_half_constructions(
    surface_rows: list[dict[str, object]],
    construction_rows: list[dict[str, str]],
    material_rows: list[dict[str, str]],
) -> dict[str, object]:
    construction_lookup = {
        str(row.get("construction_name", "")).strip(): row
        for row in construction_rows
        if str(row.get("construction_name", "")).strip()
    }
    material_lookup: dict[str, dict[str, object]] = {
        str(row.get("material_name", "")).strip(): row
        for row in material_rows
        if str(row.get("material_name", "")).strip()
    }
    construction_names = set(construction_lookup)
    material_names = set(material_lookup)
    generated_constructions: dict[str, dict[str, str]] = {}
    generated_material_count = 0
    updated_surfaces: list[dict[str, object]] = []

    for row in surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        if str(row.get("outside_boundary_condition", "")).strip() != "Adiabatic":
            continue
        construction_name = str(row.get("construction_name", "")).strip()
        if not construction_name or construction_name.endswith(ADIABATIC_HALF_CONSTRUCTION_SUFFIX):
            continue
        construction_row = construction_lookup.get(construction_name)
        if construction_row is None:
            raise WorkspaceRuleError(
                f"Adiabatic wall {row.get('surface_name', '')} references missing construction: {construction_name}"
            )
        half_construction_name = f"{construction_name}{ADIABATIC_HALF_CONSTRUCTION_SUFFIX}"
        if half_construction_name not in construction_names:
            half_construction_row, new_material_rows = build_symmetric_adiabatic_half_construction(
                construction_name=construction_name,
                layer_names=construction_layer_names(construction_row),
                material_lookup=material_lookup,
                material_names=material_names,
            )
            construction_rows.append(half_construction_row)
            construction_lookup[half_construction_name] = half_construction_row
            construction_names.add(half_construction_name)
            generated_constructions[half_construction_name] = half_construction_row
            material_rows.extend(new_material_rows)
            for material_row in new_material_rows:
                material_lookup[str(material_row.get("material_name", "")).strip()] = material_row
            generated_material_count += len(new_material_rows)
        row["construction_name"] = half_construction_name
        updated_surfaces.append(
            {
                "surface_name": str(row.get("surface_name", "")).strip(),
                "source_construction_name": construction_name,
                "adiabatic_half_construction_name": half_construction_name,
            }
        )

    return {
        "applied_surface_count": len(updated_surfaces),
        "generated_construction_count": len(generated_constructions),
        "generated_material_count": generated_material_count,
        "updated_surfaces": updated_surfaces,
    }


def preserve_designbuilder_adiabatic_full_constructions(
    surface_rows: list[dict[str, object]],
) -> dict[str, object]:
    checked_surfaces: list[dict[str, str]] = []
    normalized_surfaces: list[dict[str, str]] = []

    for row in surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        if str(row.get("outside_boundary_condition", "")).strip() != "Adiabatic":
            continue

        surface_name = str(row.get("surface_name", "")).strip()
        construction_name = str(row.get("construction_name", "")).strip()
        checked_surfaces.append(
            {
                "surface_name": surface_name,
                "construction_name": construction_name,
            }
        )
        if construction_name.endswith(ADIABATIC_HALF_CONSTRUCTION_SUFFIX):
            full_construction_name = construction_name[: -len(ADIABATIC_HALF_CONSTRUCTION_SUFFIX)]
            row["construction_name"] = full_construction_name
            normalized_surfaces.append(
                {
                    "surface_name": surface_name,
                    "previous_construction_name": construction_name,
                    "construction_name": full_construction_name,
                }
            )

    return {
        "strategy": "designbuilder_explicit_adiabatic_full_construction",
        "checked_surface_count": len(checked_surfaces),
        "normalized_surface_count": len(normalized_surfaces),
        "checked_surfaces": checked_surfaces,
        "normalized_surfaces": normalized_surfaces,
    }


def build_dynamic_wall_library(
    surface_rows: list[dict[str, object]],
    *,
    base_construction_rows: list[dict[str, str]] | None = None,
    base_material_rows: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    wall_library = load_input_wall_construction_library()
    base_material_rows = list(base_material_rows or SAMPLE_MATERIAL_ROWS)
    base_construction_rows = list(base_construction_rows or SAMPLE_CONSTRUCTION_ROWS)
    material_names = {str(row["material_name"]) for row in base_material_rows}
    construction_names = {str(row["construction_name"]) for row in base_construction_rows}
    material_rows: list[dict[str, str]] = []
    construction_rows: list[dict[str, str]] = []
    for row in surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        construction_name = str(row.get("construction_name", "")).strip()
        if not construction_name:
            continue
        if bool(wall_library.get("available")):
            input_construction_row = dict(wall_library.get("construction_rows_by_name", {}).get(construction_name, {}))
            input_construction_spec = dict(wall_library.get("construction_specs_by_name", {}).get(construction_name, {}))
            if input_construction_row:
                if construction_name not in construction_names:
                    construction_rows.append(input_construction_row)
                    construction_names.add(construction_name)
                reverse_name = str(input_construction_spec.get("reverse_name", "")).strip()
                reverse_construction_row = dict(
                    wall_library.get("construction_rows_by_name", {}).get(reverse_name, {})
                )
                if reverse_construction_row and reverse_name not in construction_names:
                    construction_rows.append(reverse_construction_row)
                    construction_names.add(reverse_name)
                for material_name in list(input_construction_spec.get("material_names", [])):
                    material_row = dict(wall_library.get("materials_by_name", {}).get(material_name, {}))
                    if material_row and material_name not in material_names:
                        material_rows.append(material_row)
                        material_names.add(material_name)
                continue
        if not construction_name.startswith("DXF "):
            continue
        wall_thickness_mm = int(row.get("inferred_wall_thickness_mm", 0) or 0)
        if wall_thickness_mm <= 0:
            continue
        boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        finish_material = (
            "Cement/plaster/mortar - cement plaster_.015"
            if boundary_condition == "Outdoors"
            else "Cement/plaster/mortar - gypsum plaster_.015"
        )
        core_thickness_mm = max(1, wall_thickness_mm - 30)
        core_material_name = dxf_wall_core_material_name(core_thickness_mm)
        if core_material_name not in material_names:
            material_rows.append(dxf_wall_core_material_row(core_thickness_mm))
            material_names.add(core_material_name)
        if construction_name in construction_names:
            continue
        construction_rows.append(
            {
                "construction_name": construction_name,
                "layer_1": finish_material,
                "layer_2": core_material_name,
                "layer_3": finish_material,
            }
        )
        construction_names.add(construction_name)
    return construction_rows, material_rows


def summarize_building_surface_rows(building_surface_rows: list[dict[str, object]]) -> dict[str, int]:
    floor_row_count = 0
    roof_row_count = 0
    wall_face_row_count = 0
    interzone_wall_face_row_count = 0
    exterior_wall_face_row_count = 0
    adiabatic_wall_face_row_count = 0
    other_surface_row_count = 0
    other_wall_face_row_count = 0
    interzone_pair_keys: set[tuple[str, str]] = set()
    for row in building_surface_rows:
        surface_type = str(row.get("surface_type", "")).strip()
        if surface_type == "Floor":
            floor_row_count += 1
            continue
        if surface_type == "Roof":
            roof_row_count += 1
            continue
        if surface_type != "Wall":
            other_surface_row_count += 1
            continue
        wall_face_row_count += 1
        boundary_condition = str(row.get("outside_boundary_condition", "")).strip()
        if boundary_condition == "Surface":
            interzone_wall_face_row_count += 1
            surface_name = str(row.get("surface_name", "")).strip()
            paired_surface_name = str(row.get("outside_boundary_condition_object", "")).strip()
            if surface_name and paired_surface_name:
                interzone_pair_keys.add(tuple(sorted((surface_name, paired_surface_name))))
            else:
                interzone_pair_keys.add((surface_name, paired_surface_name))
        elif boundary_condition == "Outdoors":
            exterior_wall_face_row_count += 1
        elif boundary_condition == "Adiabatic":
            adiabatic_wall_face_row_count += 1
        else:
            other_wall_face_row_count += 1
    interzone_unique_surface_count = len(interzone_pair_keys)
    unique_physical_wall_surface_count = (
        interzone_unique_surface_count
        + exterior_wall_face_row_count
        + adiabatic_wall_face_row_count
        + other_wall_face_row_count
    )
    return {
        "building_surface_row_count": len(building_surface_rows),
        "floor_row_count": floor_row_count,
        "roof_row_count": roof_row_count,
        "wall_face_row_count": wall_face_row_count,
        "interzone_wall_face_row_count": interzone_wall_face_row_count,
        "interzone_unique_surface_count": interzone_unique_surface_count,
        "duplicate_interzone_wall_faces_removed": interzone_wall_face_row_count - interzone_unique_surface_count,
        "exterior_wall_face_row_count": exterior_wall_face_row_count,
        "adiabatic_wall_face_row_count": adiabatic_wall_face_row_count,
        "other_wall_face_row_count": other_wall_face_row_count,
        "unique_physical_wall_surface_count": unique_physical_wall_surface_count,
        "unique_physical_building_surface_count": floor_row_count + roof_row_count + unique_physical_wall_surface_count + other_surface_row_count,
        "other_surface_row_count": other_surface_row_count,
    }


def translate_vertex_rows_xy(
    rows: list[dict[str, object]],
    *,
    offset_x_m: float,
    offset_y_m: float,
) -> list[dict[str, object]]:
    translated_rows: list[dict[str, object]] = []
    for row in rows:
        row_copy = dict(row)
        try:
            number_of_vertices = int(str(row_copy.get("number_of_vertices", "0") or "0"))
        except ValueError:
            number_of_vertices = 0
        for index in range(1, number_of_vertices + 1):
            x_key = f"v{index}_x"
            y_key = f"v{index}_y"
            if row_copy.get(x_key) not in {None, ""}:
                row_copy[x_key] = f"{float(row_copy[x_key]) - offset_x_m:.3f}"
            if row_copy.get(y_key) not in {None, ""}:
                row_copy[y_key] = f"{float(row_copy[y_key]) - offset_y_m:.3f}"
        translated_rows.append(row_copy)
    return translated_rows


def reorder_vertical_face_vertices_for_idf(row: dict[str, object], *, tolerance_m: float = 1e-9) -> dict[str, object]:
    row_copy = dict(row)
    try:
        number_of_vertices = int(str(row_copy.get("number_of_vertices", "0") or "0"))
    except ValueError:
        return row_copy
    if number_of_vertices != 4:
        return row_copy

    surface_type = str(row_copy.get("surface_type", "")).strip()
    if surface_type not in {"Wall", "Window", "Door", "GlassDoor"}:
        return row_copy

    vertices: list[tuple[float, float, float]] = []
    for index in range(1, 5):
        try:
            vertices.append(
                (
                    float(row_copy.get(f"v{index}_x", 0.0) or 0.0),
                    float(row_copy.get(f"v{index}_y", 0.0) or 0.0),
                    float(row_copy.get(f"v{index}_z", 0.0) or 0.0),
                )
            )
        except (TypeError, ValueError):
            return row_copy

    current_vertical_edge_first = (
        abs(vertices[0][0] - vertices[1][0]) <= tolerance_m
        and abs(vertices[0][1] - vertices[1][1]) <= tolerance_m
        and abs(vertices[2][0] - vertices[3][0]) <= tolerance_m
        and abs(vertices[2][1] - vertices[3][1]) <= tolerance_m
        and abs(vertices[0][2] - vertices[1][2]) > tolerance_m
        and abs(vertices[2][2] - vertices[3][2]) > tolerance_m
    )
    if not current_vertical_edge_first:
        return row_copy

    reordered_vertices = [vertices[0], vertices[3], vertices[2], vertices[1]]
    for index, (x_value, y_value, z_value) in enumerate(reordered_vertices, start=1):
        row_copy[f"v{index}_x"] = f"{x_value:.3f}"
        row_copy[f"v{index}_y"] = f"{y_value:.3f}"
        row_copy[f"v{index}_z"] = f"{z_value:.3f}"
    return row_copy


def reorder_vertex_rows_for_idf(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [reorder_vertical_face_vertices_for_idf(row) for row in rows]


def vertex_row_plane_metadata(row: dict[str, object], *, tolerance_m: float = 1e-6) -> dict[str, object] | None:
    try:
        number_of_vertices = int(str(row.get("number_of_vertices", "0") or "0"))
    except ValueError:
        return None
    if number_of_vertices < 2:
        return None

    xs: list[float] = []
    ys: list[float] = []
    for index in range(1, number_of_vertices + 1):
        try:
            xs.append(float(row.get(f"v{index}_x", 0.0) or 0.0))
            ys.append(float(row.get(f"v{index}_y", 0.0) or 0.0))
        except (TypeError, ValueError):
            return None

    x_min = min(xs)
    x_max = max(xs)
    y_min = min(ys)
    y_max = max(ys)
    if (y_max - y_min) <= tolerance_m and (x_max - x_min) > tolerance_m:
        return {"axis": "horizontal", "fixed_coord": round((y_min + y_max) / 2.0, 6)}
    if (x_max - x_min) <= tolerance_m and (y_max - y_min) > tolerance_m:
        return {"axis": "vertical", "fixed_coord": round((x_min + x_max) / 2.0, 6)}
    return None


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


def shift_vertex_row_xy(
    row: dict[str, object],
    *,
    delta_x_m: float,
    delta_y_m: float,
) -> dict[str, object]:
    row_copy = dict(row)
    try:
        number_of_vertices = int(str(row_copy.get("number_of_vertices", "0") or "0"))
    except ValueError:
        return row_copy
    if abs(delta_x_m) <= 1e-9 and abs(delta_y_m) <= 1e-9:
        return row_copy
    for index in range(1, number_of_vertices + 1):
        x_key = f"v{index}_x"
        y_key = f"v{index}_y"
        if row_copy.get(x_key) not in {None, ""}:
            row_copy[x_key] = f"{float(row_copy[x_key]) + delta_x_m:.3f}"
        if row_copy.get(y_key) not in {None, ""}:
            row_copy[y_key] = f"{float(row_copy[y_key]) + delta_y_m:.3f}"
    return row_copy


def shift_wall_inventory_row_by_surface_offsets(
    row: dict[str, object],
    surface_offsets: dict[str, dict[str, object]],
) -> dict[str, object]:
    row_copy = dict(row)
    offset_payload = None
    for surface_key in ("surface_name_primary", "surface_name_secondary"):
        surface_name = str(row_copy.get(surface_key, "")).strip()
        if surface_name and surface_name in surface_offsets:
            offset_payload = dict(surface_offsets[surface_name])
            break
    if not offset_payload:
        return row_copy

    delta_x_m = float(offset_payload.get("delta_x_m", 0.0) or 0.0)
    delta_y_m = float(offset_payload.get("delta_y_m", 0.0) or 0.0)
    if abs(delta_x_m) <= 1e-9 and abs(delta_y_m) <= 1e-9:
        row_copy["position_basis"] = f"wall_resolution:{str(offset_payload.get('reference_type', '')).strip()}"
        return row_copy

    for key in ("start_x_m", "end_x_m", "midpoint_x_m"):
        if row_copy.get(key) not in {None, ""}:
            row_copy[key] = round(float(row_copy[key]) + delta_x_m, 3)
    for key in ("start_y_m", "end_y_m", "midpoint_y_m"):
        if row_copy.get(key) not in {None, ""}:
            row_copy[key] = round(float(row_copy[key]) + delta_y_m, 3)

    if all(row_copy.get(key) not in {None, ""} for key in ("start_x_m", "start_y_m", "end_x_m", "end_y_m")):
        row_copy["segment_wkt"] = (
            f"LINESTRING ({float(row_copy['start_x_m']):.3f} {float(row_copy['start_y_m']):.3f}, "
            f"{float(row_copy['end_x_m']):.3f} {float(row_copy['end_y_m']):.3f})"
        )
    row_copy["position_basis"] = f"wall_resolution:{str(offset_payload.get('reference_type', '')).strip()}"
    return row_copy


def surface_offsets_from_wall_resolution(
    wall_resolution: dict[str, object],
) -> dict[str, dict[str, object]]:
    raw_surface_geometry = wall_resolution.get("surface_export_geometry_by_name")
    if not isinstance(raw_surface_geometry, dict) or not raw_surface_geometry:
        raise WorkspaceRuleError(
            "wall_resolution.json is missing surface_export_geometry_by_name; final wall export geometry is undefined."
        )

    surface_offsets: dict[str, dict[str, object]] = {}
    for surface_name, raw_payload in raw_surface_geometry.items():
        if not isinstance(raw_payload, dict):
            continue
        source_edge = dict(raw_payload.get("source_edge", {}))
        final_export_line = dict(raw_payload.get("final_export_line", {}))
        source_points = line_points_from_payload(source_edge)
        final_points = line_points_from_payload(final_export_line)
        if source_points is None or final_points is None:
            continue
        surface_offsets[str(surface_name).strip()] = {
            "wall_id": str(raw_payload.get("wall_id", "")).strip(),
            "reference_type": str(raw_payload.get("reference_type", "")).strip(),
            "delta_x_m": round(final_points[0][0] - source_points[0][0], 6),
            "delta_y_m": round(final_points[0][1] - source_points[0][1], 6),
            "source_edge": source_edge,
            "final_export_line": final_export_line,
        }
    if not surface_offsets:
        raise WorkspaceRuleError(
            "wall_resolution.json did not yield any usable surface export geometry."
        )
    return surface_offsets


def floor_area_by_zone(rows: list[dict[str, object]]) -> dict[str, float]:
    area_by_zone: dict[str, float] = {}
    for row in rows:
        if str(row.get("surface_type", "")).strip() != "Floor":
            continue
        zone_name = str(row.get("zone_name", "")).strip()
        if not zone_name:
            continue
        try:
            number_of_vertices = int(str(row.get("number_of_vertices", "0") or "0"))
        except ValueError:
            continue
        xs: list[float] = []
        ys: list[float] = []
        for index in range(1, number_of_vertices + 1):
            try:
                xs.append(float(row.get(f"v{index}_x", 0.0) or 0.0))
                ys.append(float(row.get(f"v{index}_y", 0.0) or 0.0))
            except (TypeError, ValueError):
                xs = []
                ys = []
                break
        if not xs or not ys:
            continue
        polygon_area_m2 = 0.0
        for index, x_value in enumerate(xs):
            next_index = (index + 1) % len(xs)
            polygon_area_m2 += (x_value * ys[next_index]) - (xs[next_index] * ys[index])
        area_by_zone[zone_name] = round(
            area_by_zone.get(zone_name, 0.0) + abs(polygon_area_m2 / 2.0),
            6,
        )
    return area_by_zone


def build_zone_area_preservation_summary(
    source_rows: list[dict[str, object]],
    resolved_rows: list[dict[str, object]],
) -> dict[str, object]:
    source_area_by_zone = floor_area_by_zone(source_rows)
    resolved_area_by_zone = floor_area_by_zone(resolved_rows)
    zone_names = sorted(set(source_area_by_zone) | set(resolved_area_by_zone))
    deltas = {
        zone_name: round(
            abs(resolved_area_by_zone.get(zone_name, 0.0) - source_area_by_zone.get(zone_name, 0.0)),
            6,
        )
        for zone_name in zone_names
    }
    return {
        "checked_zone_count": len(zone_names),
        "passed": all(delta <= 1e-6 for delta in deltas.values()),
        "max_area_delta_m2": round(max(deltas.values(), default=0.0), 6),
        "zone_area_delta_m2_by_name": deltas,
    }


def build_opening_host_alignment_summary(
    surface_rows: list[dict[str, object]],
    fenestration_rows: list[dict[str, object]],
    *,
    tolerance_m: float = 1e-6,
) -> dict[str, object]:
    host_plane_by_name = {
        str(row.get("surface_name", "")).strip(): vertex_row_plane_metadata(row)
        for row in surface_rows
        if str(row.get("surface_name", "")).strip()
    }
    failed_fenestration_names: list[str] = []
    max_plane_delta_m = 0.0
    checked_count = 0
    for row in fenestration_rows:
        host_name = str(row.get("building_surface_name", "")).strip()
        fenestration_name = str(row.get("fenestration_name", "")).strip()
        host_plane = host_plane_by_name.get(host_name)
        fenestration_plane = vertex_row_plane_metadata(row)
        if host_plane is None or fenestration_plane is None:
            failed_fenestration_names.append(fenestration_name or host_name)
            continue
        checked_count += 1
        if str(host_plane.get("axis", "")).strip() != str(fenestration_plane.get("axis", "")).strip():
            failed_fenestration_names.append(fenestration_name or host_name)
            continue
        plane_delta_m = abs(
            float(host_plane.get("fixed_coord", 0.0) or 0.0)
            - float(fenestration_plane.get("fixed_coord", 0.0) or 0.0)
        )
        max_plane_delta_m = max(max_plane_delta_m, plane_delta_m)
        if plane_delta_m > tolerance_m:
            failed_fenestration_names.append(fenestration_name or host_name)
    return {
        "checked_fenestration_count": checked_count,
        "passed": not failed_fenestration_names,
        "failed_fenestration_names": sorted(name for name in failed_fenestration_names if name),
        "max_plane_delta_mm": round(max_plane_delta_m * 1000.0, 6),
    }


def apply_wall_resolution_bundle_geometry(
    *,
    surface_rows: list[dict[str, object]],
    fenestration_rows: list[dict[str, object]],
    wall_inventory_rows: list[dict[str, object]],
    wall_resolution: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    surface_offsets = surface_offsets_from_wall_resolution(wall_resolution)
    resolved_surface_rows: list[dict[str, object]] = []
    resolved_fenestration_rows = [dict(row) for row in fenestration_rows]
    resolved_wall_inventory_rows = [dict(row) for row in wall_inventory_rows]
    applied_surface_count = 0
    shifted_surface_count = 0
    max_shift_m = 0.0

    for row in surface_rows:
        row_copy = dict(row)
        if str(row_copy.get("surface_type", "")).strip() != "Wall":
            resolved_surface_rows.append(row_copy)
            continue
        surface_name = str(row_copy.get("surface_name", "")).strip()
        offset_payload = surface_offsets.get(surface_name)
        if offset_payload is None:
            raise WorkspaceRuleError(
                f"Missing wall resolution geometry for wall surface: {surface_name}"
            )
        delta_x_m = float(offset_payload.get("delta_x_m", 0.0) or 0.0)
        delta_y_m = float(offset_payload.get("delta_y_m", 0.0) or 0.0)
        applied_surface_count += 1
        shift_magnitude = math.hypot(delta_x_m, delta_y_m)
        if shift_magnitude > 1e-9:
            shifted_surface_count += 1
        max_shift_m = max(max_shift_m, shift_magnitude)
        resolved_surface_rows.append(
            shift_vertex_row_xy(
                row_copy,
                delta_x_m=delta_x_m,
                delta_y_m=delta_y_m,
            )
        )

    if surface_offsets:
        resolved_fenestration_rows = [
            shift_vertex_row_xy(
                row,
                delta_x_m=float(
                    surface_offsets.get(str(row.get("building_surface_name", "")).strip(), {}).get("delta_x_m", 0.0)
                    or 0.0
                ),
                delta_y_m=float(
                    surface_offsets.get(str(row.get("building_surface_name", "")).strip(), {}).get("delta_y_m", 0.0)
                    or 0.0
                ),
            )
            if str(row.get("building_surface_name", "")).strip() in surface_offsets
            else dict(row)
            for row in resolved_fenestration_rows
        ]
        resolved_wall_inventory_rows = [
            shift_wall_inventory_row_by_surface_offsets(row, surface_offsets)
            for row in resolved_wall_inventory_rows
        ]

    return resolved_surface_rows, resolved_fenestration_rows, resolved_wall_inventory_rows, {
        "available": True,
        "reference_policy": dict(wall_resolution.get("reference_policy", {})),
        "resolved_wall_count": len(list(wall_resolution.get("resolved_walls", []))),
        "applied_surface_count": applied_surface_count,
        "shifted_surface_count": shifted_surface_count,
        "max_shift_m": round(max_shift_m, 6),
        "surface_offsets_by_name": surface_offsets,
        "qa_checks": {
            **dict(wall_resolution.get("qa_checks", {})),
            "zone_area_preservation": build_zone_area_preservation_summary(
                surface_rows,
                resolved_surface_rows,
            ),
        },
    }


def translate_point_xy(value: object, *, offset_x_m: float, offset_y_m: float) -> object:
    if not isinstance(value, list) or len(value) < 2:
        return value
    return [round(float(value[0]) - offset_x_m, 3), round(float(value[1]) - offset_y_m, 3)]


def translate_rectangle_xy(rectangle: object, *, offset_x_m: float, offset_y_m: float) -> object:
    if not isinstance(rectangle, list) or len(rectangle) < 4:
        return rectangle
    x1, y1, x2, y2 = [float(value) for value in rectangle[:4]]
    return [
        round(min(x1, x2) - offset_x_m, 3),
        round(min(y1, y2) - offset_y_m, 3),
        round(max(x1, x2) - offset_x_m, 3),
        round(max(y1, y2) - offset_y_m, 3),
    ]


def enrich_surface_rows_with_wall_metadata(
    surface_rows: list[dict[str, object]],
    wall_inventory_rows: list[dict[str, object]],
    wall_resolution: dict[str, object],
) -> list[dict[str, object]]:
    construction_by_surface_name: dict[str, str] = {}
    for wall_row in wall_inventory_rows:
        primary_surface_name = str(wall_row.get("surface_name_primary", "")).strip()
        secondary_surface_name = str(wall_row.get("surface_name_secondary", "")).strip()
        primary_construction_name = str(wall_row.get("construction_name_primary", "")).strip()
        secondary_construction_name = str(wall_row.get("construction_name_secondary", "")).strip()
        if primary_surface_name and primary_construction_name:
            construction_by_surface_name[primary_surface_name] = primary_construction_name
        if secondary_surface_name and secondary_construction_name:
            construction_by_surface_name[secondary_surface_name] = secondary_construction_name
    thickness_by_surface_name = {
        str(surface_name): dict(payload)
        for surface_name, payload in dict(
            dict(wall_resolution.get("wall_thickness_inference", {})).get("surface_thicknesses", {})
        ).items()
        if isinstance(payload, dict)
    }
    adiabatic_boundary_by_surface_name = {
        str(item.get("surface_name", "")).strip(): dict(item)
        for item in list(dict(wall_resolution.get("adiabatic_boundary_summary", {})).get("converted_surfaces", []))
        if isinstance(item, dict) and str(item.get("surface_name", "")).strip()
    }
    resolved_surface_by_name = {
        str(item.get("surface_name", "")).strip(): dict(item)
        for item in list(wall_resolution.get("resolved_surfaces", []))
        if isinstance(item, dict) and str(item.get("surface_name", "")).strip()
    }
    enriched_rows: list[dict[str, object]] = []
    for row in surface_rows:
        row_copy = dict(row)
        if str(row_copy.get("surface_type", "")).strip() == "Wall":
            surface_name = str(row_copy.get("surface_name", "")).strip()
            adiabatic_boundary = adiabatic_boundary_by_surface_name.get(surface_name)
            if adiabatic_boundary is not None:
                row_copy["outside_boundary_condition"] = "Adiabatic"
                row_copy["outside_boundary_condition_object"] = ""
                row_copy["sun_exposure"] = "NoSun"
                row_copy["wind_exposure"] = "NoWind"
                row_copy["view_factor_to_ground"] = "0"
                row_copy["wall_boundary_override_source"] = "adiabatic_layer"
                row_copy["wall_boundary_layer_canonical"] = str(adiabatic_boundary.get("layer_canonical", "")).strip()
            resolved_surface = resolved_surface_by_name.get(surface_name)
            if resolved_surface is not None:
                boundary_condition = str(resolved_surface.get("boundary_condition", "")).strip()
                paired_surface_name = str(resolved_surface.get("paired_surface_name", "")).strip()
                if boundary_condition in {"Outdoors", "Surface", "Adiabatic"}:
                    row_copy["outside_boundary_condition"] = boundary_condition
                    row_copy["outside_boundary_condition_object"] = (
                        paired_surface_name if boundary_condition == "Surface" else ""
                    )
                    row_copy["sun_exposure"] = "SunExposed" if boundary_condition == "Outdoors" else "NoSun"
                    row_copy["wind_exposure"] = "WindExposed" if boundary_condition == "Outdoors" else "NoWind"
                    row_copy["view_factor_to_ground"] = "" if boundary_condition == "Outdoors" else "0"
                construction_name = str(resolved_surface.get("construction_name", "")).strip()
                if construction_name:
                    row_copy["construction_name"] = construction_name
            if surface_name and not str(row_copy.get("construction_name", "")).strip():
                row_copy["construction_name"] = construction_by_surface_name.get(surface_name, "")
            thickness_payload = thickness_by_surface_name.get(surface_name, {})
            wall_thickness_mm = thickness_payload.get("wall_thickness_mm")
            if row_copy.get("inferred_wall_thickness_mm") in {None, ""} and wall_thickness_mm not in {None, ""}:
                row_copy["inferred_wall_thickness_mm"] = int(wall_thickness_mm)
            if not str(row_copy.get("wall_thickness_inference_source", "")).strip():
                row_copy["wall_thickness_inference_source"] = str(thickness_payload.get("source", "")).strip()
        enriched_rows.append(row_copy)
    return enriched_rows


def default_source_zone_name(zone_key: str) -> str:
    normalized_key = canonical_zone_key(zone_key)
    if normalized_key == "PK_PB":
        return "PK + PB"
    if normalized_key == "PB":
        return "PB"
    if normalized_key == "LOGIA":
        return "LOGIA"
    compact_match = re.fullmatch(r"(PN|WC)_0?(\d{1,2})", normalized_key)
    if compact_match:
        return f"{compact_match.group(1)} {int(compact_match.group(2)):02d}"
    return normalized_key.replace("_", " ")


def geometry_zone_aliases_by_key(geometry_payload: dict[str, object] | None) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source_key, target_key in dict((geometry_payload or {}).get("zone_name_aliases", {})).items():
        normalized_source = canonical_zone_key(str(source_key))
        normalized_target = canonical_zone_key(str(target_key))
        if normalized_source and normalized_target:
            aliases[normalized_source] = normalized_target
    return aliases


def geometry_zone_merge_targets_by_key(geometry_payload: dict[str, object] | None) -> dict[str, str]:
    targets: dict[str, str] = {}
    for source_key, target_key in dict((geometry_payload or {}).get("zone_merge_source_to_target_key", {})).items():
        normalized_source = canonical_zone_key(str(source_key))
        normalized_target = canonical_zone_key(str(target_key))
        if normalized_source and normalized_target:
            targets[normalized_source] = normalized_target
    return targets


def resolved_geometry_zone_key(source_zone_name: str, geometry_payload: dict[str, object] | None) -> str:
    source_zone_key = canonical_zone_key(source_zone_name)
    if not source_zone_key:
        return ""
    aliased_zone_key = geometry_zone_aliases_by_key(geometry_payload).get(source_zone_key, source_zone_key)
    return geometry_zone_merge_targets_by_key(geometry_payload).get(aliased_zone_key, aliased_zone_key)


def zone_key_from_csv_zone_name(zone_name: str, apartment_prefix: str = "APARTMENT_A") -> str:
    normalized_name = str(zone_name or "").strip()
    prefix = f"{apartment_prefix}_"
    if normalized_name.startswith(prefix):
        normalized_name = normalized_name[len(prefix):]
    return canonical_zone_key(normalized_name)


def infer_apartment_prefix(
    geometry_payload: dict[str, object],
    surface_rows: list[dict[str, object]] | None = None,
    *,
    fallback: str = "APARTMENT_A",
) -> str:
    object_output_prefix = str(geometry_payload.get("object_output_prefix", "") or "").strip().rstrip("_")
    if object_output_prefix:
        return object_output_prefix

    zone_output_name_by_key = dict(geometry_payload.get("zone_output_name_by_key", {}))
    for zone_key, zone_output_name in zone_output_name_by_key.items():
        normalized_key = canonical_zone_key(str(zone_key))
        normalized_output_name = str(zone_output_name or "").strip()
        suffix = f"_{normalized_key}"
        if normalized_key and normalized_output_name.endswith(suffix):
            return normalized_output_name[: -len(suffix)] or fallback

    for row in list(surface_rows or []):
        zone_name = str(row.get("zone_name", "")).strip()
        match = re.match(r"^([A-Z0-9]+(?:_[A-Z0-9]+)*)_(LOGIA|PK_PB|PN_\d{2}|WC_\d{2})$", zone_name)
        if match:
            return match.group(1)

    return fallback


def building_name_from_prefix(apartment_prefix: str) -> str:
    normalized_prefix = str(apartment_prefix or "").strip()
    if not normalized_prefix:
        return "Apartment_A_Building"
    return "_".join(part.capitalize() for part in normalized_prefix.split("_")) + "_Building"


def build_zone_catalog(
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object],
    surface_rows: list[dict[str, object]],
    apartment_prefix: str = "APARTMENT_A",
) -> list[dict[str, object]]:
    entries_by_key: dict[str, dict[str, object]] = {}
    zone_output_name_by_key = {
        canonical_zone_key(str(zone_key)): str(zone_name).strip()
        for zone_key, zone_name in dict(geometry_payload.get("zone_output_name_by_key", {})).items()
        if canonical_zone_key(str(zone_key)) and str(zone_name).strip()
    }

    def ensure_entry(zone_key: str) -> dict[str, object]:
        normalized_key = canonical_zone_key(zone_key)
        if not normalized_key:
            return {}
        entry = entries_by_key.get(normalized_key)
        if entry is None:
            entry = {
                "zone_key": normalized_key,
                "source_zone_name": "",
                "csv_zone_name": zone_output_name_by_key.get(normalized_key, f"{apartment_prefix}_{normalized_key}"),
                "candidate_zone": {},
            }
            entries_by_key[normalized_key] = entry
        return entry

    for zone in mapping_payload.get("candidate_zones", []):
        if not isinstance(zone, dict):
            continue
        source_zone_name = str(zone.get("zone_name", "")).strip()
        if not source_zone_name:
            continue
        raw_zone_key = str(zone.get("zone_key", "")).strip() or source_zone_name
        raw_canonical_zone_key = canonical_zone_key(raw_zone_key)
        resolved_zone_key = resolved_geometry_zone_key(raw_zone_key, geometry_payload)
        entry = ensure_entry(resolved_zone_key or raw_zone_key)
        if not entry:
            continue
        if resolved_zone_key and resolved_zone_key != raw_canonical_zone_key:
            if not dict(entry.get("candidate_zone", {})):
                entry["candidate_zone"] = dict(zone)
            continue
        existing_source_zone_name = str(entry.get("source_zone_name", "")).strip()
        if existing_source_zone_name and existing_source_zone_name != source_zone_name:
            raise WorkspaceRuleError(
                "Duplicate canonical zone key detected while building bundle zone map: "
                f"{entry['zone_key']} <- {existing_source_zone_name}, {source_zone_name}"
            )
        entry["source_zone_name"] = source_zone_name
        entry["candidate_zone"] = dict(zone)

    source_zone_name_by_key = {
        canonical_zone_key(str(zone_key)): str(source_zone_name).strip()
        for zone_key, source_zone_name in dict(geometry_payload.get("source_zone_name_by_key", {})).items()
        if str(source_zone_name).strip()
    }
    for zone_key, source_zone_name in source_zone_name_by_key.items():
        entry = ensure_entry(zone_key)
        if entry and not str(entry.get("source_zone_name", "")).strip():
            entry["source_zone_name"] = source_zone_name

    geometry_zone_keys = {
        canonical_zone_key(str(zone_key))
        for zone_key in dict(geometry_payload.get("zone_geometry_by_key", {})).keys()
        if canonical_zone_key(str(zone_key))
    }
    rectangle_zone_keys = {
        canonical_zone_key(str(zone_key))
        for zone_key in dict(geometry_payload.get("zone_rectangles_m_by_key", {})).keys()
        if canonical_zone_key(str(zone_key))
    }
    for zone_key in sorted(geometry_zone_keys | rectangle_zone_keys):
        entry = ensure_entry(zone_key)
        if entry and not str(entry.get("source_zone_name", "")).strip():
            entry["source_zone_name"] = default_source_zone_name(zone_key)

    for row in surface_rows:
        if not isinstance(row, dict):
            continue
        csv_zone_name = str(row.get("zone_name", "")).strip()
        if not csv_zone_name:
            continue
        zone_key = zone_key_from_csv_zone_name(csv_zone_name, apartment_prefix=apartment_prefix)
        entry = ensure_entry(zone_key)
        if not entry:
            continue
        entry["csv_zone_name"] = csv_zone_name
        if not str(entry.get("source_zone_name", "")).strip():
            entry["source_zone_name"] = default_source_zone_name(zone_key)

    return [entries_by_key[zone_key] for zone_key in sorted(entries_by_key)]


def build_zone_name_map(
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object] | None = None,
    surface_rows: list[dict[str, object]] | None = None,
    apartment_prefix: str = "APARTMENT_A",
) -> dict[str, str]:
    zone_name_map: dict[str, str] = {}
    zone_catalog = build_zone_catalog(
        mapping_payload,
        geometry_payload or {},
        list(surface_rows or []),
        apartment_prefix=apartment_prefix,
    )
    for entry in zone_catalog:
        source_zone_name = str(entry.get("source_zone_name", "")).strip()
        csv_zone_name = str(entry.get("csv_zone_name", "")).strip()
        if source_zone_name and csv_zone_name:
            zone_name_map[source_zone_name] = csv_zone_name
    return zone_name_map


def build_placeholder_zone_layouts(
    mapping_payload: dict[str, object],
    zone_name_map: dict[str, str],
    geometry_payload: dict[str, object],
    surface_rows: list[dict[str, object]],
    zone_heights_by_name: dict[str, float],
) -> dict[str, dict[str, float | str]]:
    openings_by_source_zone: dict[str, list[dict[str, object]]] = {}
    for opening in mapping_payload.get("candidate_openings", []):
        if not isinstance(opening, dict):
            continue
        source_zone_name = str(opening.get("nearest_zone_name", "")).strip()
        if source_zone_name:
            openings_by_source_zone.setdefault(source_zone_name, []).append(opening)
    zone_layouts: dict[str, dict[str, float | str]] = {}
    apartment_prefix = infer_apartment_prefix(geometry_payload, surface_rows)
    zone_catalog = build_zone_catalog(
        mapping_payload,
        geometry_payload,
        surface_rows,
        apartment_prefix=apartment_prefix,
    )
    zone_geometry_by_key = {
        canonical_zone_key(str(zone_key)): dict(payload)
        for zone_key, payload in dict(geometry_payload.get("zone_geometry_by_key", {})).items()
        if isinstance(payload, dict)
    }
    for zone_entry in zone_catalog:
        zone = dict(zone_entry.get("candidate_zone", {}))
        source_zone_name = str(zone_entry.get("source_zone_name", "")).strip()
        csv_zone_name = str(zone_entry.get("csv_zone_name", "")).strip() or zone_name_map.get(source_zone_name, "")
        if not csv_zone_name:
            continue
        geometry_manifest = zone_geometry_by_key.get(str(zone_entry.get("zone_key", "")), {})
        floor_area_m2 = float(geometry_manifest.get("footprint_area_m2", zone.get("area_m2", 0.0)) or 0.0)
        anchor_xy = zone.get("anchor_xy", []) if isinstance(zone.get("anchor_xy"), list) else []
        if len(anchor_xy) >= 2:
            anchor_x_m = float(anchor_xy[0]) / 1000.0
            anchor_y_m = float(anchor_xy[1]) / 1000.0
        else:
            rectangle: object = []
            if isinstance(geometry_manifest.get("footprint_rectangles_m"), list) and geometry_manifest.get("footprint_rectangles_m"):
                rectangle = geometry_manifest.get("footprint_rectangles_m", [])[0]
            elif isinstance(geometry_manifest.get("outer_block_rect_m"), list):
                rectangle = geometry_manifest.get("outer_block_rect_m", [])
            if isinstance(rectangle, list) and len(rectangle) >= 4:
                anchor_x_m = (float(rectangle[0]) + float(rectangle[2])) / 2.0
                anchor_y_m = (float(rectangle[1]) + float(rectangle[3])) / 2.0
            else:
                anchor_x_m = 0.0
                anchor_y_m = 0.0
        assigned_openings = openings_by_source_zone.get(source_zone_name, [])
        opening_widths_m = [
            max(0.5, float(item.get("width_mm", 0) or 0) / 1000.0)
            for item in assigned_openings
            if item.get("width_mm") not in {None, ""}
        ]
        target_width_m = max(
            2.4,
            floor_area_m2 ** 0.5 if floor_area_m2 > 0 else 2.4,
            (sum(opening_widths_m) + (0.3 * (len(opening_widths_m) + 1))) if opening_widths_m else 0.0,
            (max(opening_widths_m) + 0.8) if opening_widths_m else 0.0,
        )
        target_depth_m = max(1.8, (floor_area_m2 / target_width_m) if target_width_m > 0 and floor_area_m2 > 0 else 1.8)
        if csv_zone_name not in zone_heights_by_name:
            raise WorkspaceRuleError(f"Surface rows are missing human-provided height for zone: {csv_zone_name}")
        zone_height_m = float(zone_heights_by_name[csv_zone_name])
        if zone_height_m <= 0.0:
            raise WorkspaceRuleError(f"Zone height must be greater than 0 for zone: {csv_zone_name}")
        zone_layouts[csv_zone_name] = {
            "source_zone_name": source_zone_name,
            "center_x_m": anchor_x_m,
            "center_y_m": anchor_y_m,
            "width_m": target_width_m,
            "depth_m": target_depth_m,
            "height_m": zone_height_m,
        }
    return zone_layouts


def infer_zone_heights_by_name(surface_rows: list[dict[str, object]]) -> dict[str, float]:
    heights: dict[str, float] = {}
    for row in surface_rows:
        zone_name = str(row.get("zone_name", "")).strip()
        if not zone_name:
            continue
        candidate_values = [
            float(row.get("v1_z", 0.0) or 0.0),
            float(row.get("v2_z", 0.0) or 0.0),
            float(row.get("v3_z", 0.0) or 0.0),
            float(row.get("v4_z", 0.0) or 0.0),
        ]
        heights[zone_name] = max(heights.get(zone_name, 0.0), max(candidate_values))
    return {zone_name: round(value, 3) for zone_name, value in heights.items() if value > 0.0}


def build_zone_wall_thickness_counts(surface_rows: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    by_zone: dict[str, Counter[int]] = {}
    for row in surface_rows:
        if str(row.get("surface_type", "")).strip() != "Wall":
            continue
        zone_name = str(row.get("zone_name", "")).strip()
        wall_thickness_mm = int(row.get("inferred_wall_thickness_mm", 0) or 0)
        if zone_name and wall_thickness_mm > 0:
            by_zone.setdefault(zone_name, Counter()).update([wall_thickness_mm])
    return {
        zone_name: dict(sorted((str(thickness), int(count)) for thickness, count in counter.items()))
        for zone_name, counter in by_zone.items()
    }


def build_output_path_map(
    *,
    geometry_payload_path: Path | None,
    surface_rows_path: Path | None,
    wall_inventory_path: Path | None,
    wall_resolution_path: Path | None,
    fenestration_rows_path: Path | None,
    opening_host_mapping_path: Path | None,
) -> dict[str, dict[str, str]]:
    geometry_output_paths: dict[str, str] = {}
    surface_output_paths: dict[str, str] = {}
    wall_output_paths: dict[str, str] = {}
    fenestration_output_paths: dict[str, str] = {}
    if geometry_payload_path is not None:
        geometry_output_paths["geometry_payload"] = workspace_path(geometry_payload_path)
        for name in ("zone_rectangles.json", "partition_summary.json"):
            sibling = geometry_payload_path.parent / name
            if sibling.exists():
                geometry_output_paths[name.replace(".json", "")] = workspace_path(sibling)
    if surface_rows_path is not None:
        surface_output_paths["surface_rows"] = workspace_path(surface_rows_path)
        sibling = surface_rows_path.parent / "adjacency_summary.json"
        if sibling.exists():
            surface_output_paths["adjacency_summary"] = workspace_path(sibling)
    if wall_inventory_path is not None:
        wall_output_paths["wall_inventory"] = workspace_path(wall_inventory_path)
    if wall_resolution_path is not None:
        wall_output_paths["wall_resolution"] = workspace_path(wall_resolution_path)
    if fenestration_rows_path is not None:
        fenestration_output_paths["fenestration_rows"] = workspace_path(fenestration_rows_path)
    if opening_host_mapping_path is not None and opening_host_mapping_path.exists():
        fenestration_output_paths["opening_host_mapping"] = workspace_path(opening_host_mapping_path)
    return {
        "geometry_output_paths": geometry_output_paths,
        "surface_output_paths": surface_output_paths,
        "wall_output_paths": wall_output_paths,
        "fenestration_output_paths": fenestration_output_paths,
    }


def load_optional_json_object(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        return {}
    return load_json_object(path)


def build_zone_rows_and_manifest(
    *,
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object],
    surface_rows: list[dict[str, object]],
    adjacency_summary: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, str]]:
    apartment_prefix = infer_apartment_prefix(geometry_payload, surface_rows)
    zone_catalog = build_zone_catalog(
        mapping_payload,
        geometry_payload,
        surface_rows,
        apartment_prefix=apartment_prefix,
    )
    zone_name_map = {
        str(entry.get("source_zone_name", "")).strip(): str(entry.get("csv_zone_name", "")).strip()
        for entry in zone_catalog
        if str(entry.get("source_zone_name", "")).strip() and str(entry.get("csv_zone_name", "")).strip()
    }
    zone_heights = infer_zone_heights_by_name(surface_rows)
    zone_layouts = build_placeholder_zone_layouts(
        mapping_payload,
        zone_name_map,
        geometry_payload,
        surface_rows,
        zone_heights,
    )
    zone_wall_thickness_counts = build_zone_wall_thickness_counts(surface_rows)
    source_zone_name_by_key = {
        str(zone_key): str(source_zone_name)
        for zone_key, source_zone_name in dict(geometry_payload.get("source_zone_name_by_key", {})).items()
    }
    zone_geometry_by_key = {
        str(zone_key): dict(payload)
        for zone_key, payload in dict(geometry_payload.get("zone_geometry_by_key", {})).items()
        if isinstance(payload, dict)
    }
    export_origin_offset_m = list(geometry_payload.get("export_origin_offset_m", [0.0, 0.0, 0.0]))
    if len(export_origin_offset_m) < 2:
        export_origin_offset_m = [0.0, 0.0, 0.0]
    offset_x_m = float(export_origin_offset_m[0] or 0.0)
    offset_y_m = float(export_origin_offset_m[1] or 0.0)
    export_origin_mode = str(geometry_payload.get("export_origin_mode", "") or "")
    export_origin_reference = str(geometry_payload.get("export_origin_reference", "") or "")
    geometry_by_source_name: dict[str, dict[str, object]] = {}
    for zone_key, payload in zone_geometry_by_key.items():
        source_zone_name = source_zone_name_by_key.get(zone_key, "")
        if source_zone_name:
            geometry_by_source_name[source_zone_name] = payload
    geometry_by_zone_key = {
        canonical_zone_key(zone_key): payload
        for zone_key, payload in zone_geometry_by_key.items()
        if canonical_zone_key(zone_key)
    }
    zone_rows: list[dict[str, object]] = []
    zone_manifest_rows: list[dict[str, object]] = []
    for zone_entry in zone_catalog:
        zone = dict(zone_entry.get("candidate_zone", {}))
        source_zone_name = str(zone_entry.get("source_zone_name", "")).strip()
        csv_zone_name = str(zone_entry.get("csv_zone_name", "")).strip()
        if not csv_zone_name:
            continue
        zone_key = str(zone_entry.get("zone_key", "")).strip()
        geometry_manifest = dict(geometry_by_source_name.get(source_zone_name, geometry_by_zone_key.get(zone_key, {})))
        layout = dict(zone_layouts.get(csv_zone_name, {}))
        actual_area_m2 = float(geometry_manifest.get("footprint_area_m2", zone.get("area_m2", 0.0)) or 0.0)
        if csv_zone_name not in zone_heights:
            raise WorkspaceRuleError(f"Surface rows are missing human-provided height for zone: {csv_zone_name}")
        zone_height_m = float(zone_heights[csv_zone_name])
        if zone_height_m <= 0.0:
            raise WorkspaceRuleError(f"Zone height must be greater than 0 for zone: {csv_zone_name}")
        zone_rows.append(
            {
                "zone_name": csv_zone_name,
                "relative_north_deg": "0",
                "x_origin_m": "0",
                "y_origin_m": "0",
                "z_origin_m": "0",
                "zone_type": "1",
                "zone_multiplier": "1",
                "ceiling_height_m": f"{zone_height_m:.3f}",
                "volume_m3": f"{(actual_area_m2 * zone_height_m):.3f}" if actual_area_m2 > 0 else "",
                "floor_area_m2": f"{actual_area_m2:.3f}" if actual_area_m2 > 0 else zone.get("area_m2", ""),
                "inside_convection_algorithm": "TARP",
                "outside_convection_algorithm": "",
                "part_of_total_floor_area": "Yes",
            }
        )
        anchor_xy = zone.get("anchor_xy", []) if isinstance(zone.get("anchor_xy"), list) else []
        if len(anchor_xy) >= 2:
            anchor_xy_m = [round(float(anchor_xy[0]) / 1000.0, 3), round(float(anchor_xy[1]) / 1000.0, 3)]
        elif float(layout.get("center_x_m", 0.0) or 0.0) or float(layout.get("center_y_m", 0.0) or 0.0):
            anchor_xy_m = [
                round(float(layout.get("center_x_m", 0.0) or 0.0), 3),
                round(float(layout.get("center_y_m", 0.0) or 0.0), 3),
            ]
        else:
            anchor_xy_m = None
        if anchor_xy_m is not None:
            anchor_xy_m = translate_point_xy(anchor_xy_m, offset_x_m=offset_x_m, offset_y_m=offset_y_m)
        manifest_row: dict[str, object] = {
            "source_zone_name": source_zone_name,
            "csv_zone_name": csv_zone_name,
            "area_m2": zone.get("area_m2"),
            "label_handle": zone.get("label_handle"),
            "anchor_xy_m": anchor_xy_m,
            "box_width_m": round(float(layout.get("width_m", 0.0) or 0.0), 3),
            "box_depth_m": round(float(layout.get("depth_m", 0.0) or 0.0), 3),
            "box_height_m": round(zone_height_m, 3),
        }
        if geometry_manifest:
            translated_geometry_manifest = dict(geometry_manifest)
            if not str(translated_geometry_manifest.get("source_zone_name", "")).strip():
                translated_geometry_manifest["source_zone_name"] = source_zone_name
            if isinstance(translated_geometry_manifest.get("outer_block_rect_m"), list):
                translated_geometry_manifest["outer_block_rect_m"] = translate_rectangle_xy(
                    translated_geometry_manifest.get("outer_block_rect_m"),
                    offset_x_m=offset_x_m,
                    offset_y_m=offset_y_m,
                )
            if isinstance(translated_geometry_manifest.get("footprint_rectangles_m"), list):
                translated_geometry_manifest["footprint_rectangles_m"] = [
                    translate_rectangle_xy(rectangle, offset_x_m=offset_x_m, offset_y_m=offset_y_m)
                    for rectangle in translated_geometry_manifest.get("footprint_rectangles_m", [])
                ]
            manifest_row.update(translated_geometry_manifest)
        manifest_row["internal_wall_count"] = int(dict(adjacency_summary.get("zone_internal_wall_count", {})).get(csv_zone_name, 0))
        manifest_row["external_wall_count"] = int(dict(adjacency_summary.get("zone_external_wall_count", {})).get(csv_zone_name, 0))
        manifest_row["adiabatic_wall_count"] = int(dict(adjacency_summary.get("zone_adiabatic_wall_count", {})).get(csv_zone_name, 0))
        if csv_zone_name in zone_wall_thickness_counts:
            manifest_row["wall_thickness_counts_mm"] = zone_wall_thickness_counts[csv_zone_name]
        manifest_row["export_origin_mode"] = export_origin_mode
        manifest_row["export_origin_offset_m"] = [round(offset_x_m, 3), round(offset_y_m, 3), 0.0]
        manifest_row["export_origin_reference"] = export_origin_reference
        zone_manifest_rows.append(manifest_row)
    return zone_rows, zone_manifest_rows, zone_name_map


def build_bundle_artifacts(
    *,
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object],
    surface_rows: list[dict[str, object]],
    wall_inventory_rows: list[dict[str, object]],
    wall_resolution: dict[str, object],
    fenestration_rows: list[dict[str, object]],
    opening_host_mapping_rows: list[dict[str, object]] | None = None,
    source_paths: dict[str, str | Path] | None = None,
    adjacency_summary: dict[str, object] | None = None,
    project_id: str | None = None,
) -> dict[str, object]:
    adjacency_summary = dict(adjacency_summary or {})
    source_paths = dict(source_paths or {})
    export_origin_offset_m = list(geometry_payload.get("export_origin_offset_m", [0.0, 0.0, 0.0]))
    if len(export_origin_offset_m) < 2:
        export_origin_offset_m = [0.0, 0.0, 0.0]
    offset_x_m = float(export_origin_offset_m[0] or 0.0)
    offset_y_m = float(export_origin_offset_m[1] or 0.0)
    enriched_surface_rows = enrich_surface_rows_with_wall_metadata(
        surface_rows,
        wall_inventory_rows,
        wall_resolution,
    )
    aligned_surface_rows, aligned_fenestration_rows, aligned_wall_inventory_rows, wall_export_geometry_summary = (
        apply_wall_resolution_bundle_geometry(
            surface_rows=enriched_surface_rows,
            fenestration_rows=fenestration_rows,
            wall_inventory_rows=wall_inventory_rows,
            wall_resolution=wall_resolution,
        )
    )
    translated_surface_rows = translate_vertex_rows_xy(
        aligned_surface_rows,
        offset_x_m=offset_x_m,
        offset_y_m=offset_y_m,
    )
    translated_surface_rows = reorder_vertex_rows_for_idf(translated_surface_rows)
    translated_fenestration_rows = reorder_vertex_rows_for_idf([dict(row) for row in aligned_fenestration_rows])
    translated_wall_inventory_rows = translate_wall_inventory_rows_xy(
        aligned_wall_inventory_rows,
        offset_x_m=offset_x_m,
        offset_y_m=offset_y_m,
    )
    opening_host_alignment_summary = build_opening_host_alignment_summary(
        translated_surface_rows,
        translated_fenestration_rows,
    )
    zone_rows, zone_manifest_rows, zone_name_map = build_zone_rows_and_manifest(
        mapping_payload=mapping_payload,
        geometry_payload=geometry_payload,
        surface_rows=enriched_surface_rows,
        adjacency_summary=adjacency_summary,
    )
    wall_library = load_input_wall_construction_library()
    base_construction_rows = list(wall_library.get("bundle_default_construction_rows", [])) or list(SAMPLE_CONSTRUCTION_ROWS)
    base_material_rows = list(wall_library.get("bundle_default_material_rows", [])) or list(SAMPLE_MATERIAL_ROWS)
    base_glazing_rows = list(wall_library.get("bundle_default_glazing_rows", [])) or list(SAMPLE_GLAZING_ROWS)
    base_gas_rows = list(wall_library.get("bundle_default_gas_rows", [])) or list(SAMPLE_GAS_ROWS)
    base_frame_rows = list(wall_library.get("bundle_default_frame_rows", [])) or list(SAMPLE_FRAME_ROWS)
    dynamic_wall_construction_rows, dynamic_wall_material_rows = build_dynamic_wall_library(
        enriched_surface_rows,
        base_construction_rows=base_construction_rows,
        base_material_rows=base_material_rows,
    )
    construction_rows = [*base_construction_rows, *dynamic_wall_construction_rows]
    material_rows = [*base_material_rows, *dynamic_wall_material_rows]
    adiabatic_construction_summary = preserve_designbuilder_adiabatic_full_constructions(translated_surface_rows)
    building_surface_summary = summarize_building_surface_rows(translated_surface_rows)
    sample_geometry = load_sample_geometry_template(zone_name_map)
    sample_geometry_qa = None
    apartment_prefix = infer_apartment_prefix(geometry_payload, surface_rows)
    if sample_geometry is not None:
        sample_idf_template = _resolve_sample_idf_template()
        sample_geometry_qa = {
            "geometry_source": workspace_path(sample_idf_template) if sample_idf_template is not None else "",
            "sample_zone_count": len(sample_geometry.get("zone_rows", [])),
            "sample_building_surface_count": len(sample_geometry.get("building_surface_rows", [])),
            "sample_fenestration_count": len(sample_geometry.get("fenestration_rows", [])),
            "dxf_zone_count": len(zone_rows),
            "dxf_building_surface_count": len(translated_surface_rows),
            "dxf_fenestration_count": len(translated_fenestration_rows),
        }
    geometry_mode = str(geometry_payload.get("geometry_mode", "") or "")
    source_paths_normalized = {
        key: workspace_path(value) if value else ""
        for key, value in source_paths.items()
    }
    output_path_maps = build_output_path_map(
        geometry_payload_path=Path(source_paths["geometry_payload"]) if source_paths.get("geometry_payload") else None,
        surface_rows_path=Path(source_paths["surface_rows"]) if source_paths.get("surface_rows") else None,
        wall_inventory_path=Path(source_paths["wall_inventory"]) if source_paths.get("wall_inventory") else None,
        wall_resolution_path=Path(source_paths["wall_resolution"]) if source_paths.get("wall_resolution") else None,
        fenestration_rows_path=Path(source_paths["fenestration_rows"]) if source_paths.get("fenestration_rows") else None,
        opening_host_mapping_path=Path(source_paths["opening_host_mapping"]) if source_paths.get("opening_host_mapping") else None,
    )
    opening_manifest_rows = list(opening_host_mapping_rows or [])
    tables: dict[str, list[dict[str, object]]] = {
        "Version.csv": [{"version_identifier": "9.4.0.002"}],
        "Site_Location.csv": [{"location_name": "Untitled (01-01:31-12)  (01-01:31-12)", "latitude": "21.03", "longitude": "105.8", "time_zone": "7", "elevation_m": "6"}],
        "Building.csv": [{"building_name": building_name_from_prefix(apartment_prefix), "north_axis_deg": "0", "terrain": "Suburbs", "loads_convergence_tolerance": "0.04", "temperature_convergence_tolerance": "0.4", "solar_distribution": "FullExterior", "maximum_warmup_days": "25", "minimum_warmup_days": "6"}],
        "GlobalGeometryRules.csv": [{"starting_vertex_position": "LowerLeftCorner", "vertex_entry_direction": "CounterClockWise", "coordinate_system": DEFAULT_IDF_COORDINATE_SYSTEM}],
        "Zone.csv": zone_rows,
        "BuildingSurface_Detailed.csv": translated_surface_rows,
        "FenestrationSurface_Detailed.csv": translated_fenestration_rows,
        "Construction.csv": construction_rows,
        "Material.csv": material_rows,
        "WindowMaterial_Gas.csv": base_gas_rows,
        "WindowMaterial_SimpleGlazingSystem.csv": [],
        "WindowMaterial_Glazing.csv": base_glazing_rows,
        "WindowProperty_FrameAndDivider.csv": base_frame_rows,
        "Wall_Inventory.csv": translated_wall_inventory_rows,
    }
    manifest = {
        "bundle_version": "1.0.0",
        "bundle_purpose": "CSV data-entry bundle for Apartment A IDF authoring",
        "source_mapping_version": mapping_payload.get("mapping_version"),
        "source_extract": mapping_payload.get("source_extract"),
        "upstream_source": mapping_payload.get("upstream_source"),
        "geometry_mode": geometry_mode,
        "geometry_source": geometry_payload.get("geometry_source") or None,
        "geometry_template_source": None,
        "geometry_policy": geometry_payload.get("geometry_policy", {}),
        "geometry_policy_source": geometry_payload.get("geometry_policy_source"),
        "geometry_output_paths": output_path_maps.get("geometry_output_paths", {}),
        "surface_output_paths": output_path_maps.get("surface_output_paths", {}),
        "wall_output_paths": output_path_maps.get("wall_output_paths", {}),
        "fenestration_output_paths": output_path_maps.get("fenestration_output_paths", {}),
        "single_block_validation": geometry_payload.get("single_block_validation", {}),
        "sample_geometry_qa": sample_geometry_qa,
        "adjacency_pair_count": int(adjacency_summary.get("adjacency_pair_count", wall_resolution.get("interzone_pair_count", 0)) or 0),
        "wall_thickness_inference": wall_resolution.get("wall_thickness_inference", {}),
        "prefilled_counts": {
            "zone_rows": len(zone_rows),
            "building_surface_rows": len(translated_surface_rows),
            "fenestration_rows": len(translated_fenestration_rows),
            "wall_inventory_rows": len(translated_wall_inventory_rows),
            "construction_rows": len(construction_rows),
            "material_rows": len(material_rows),
            "glazing_rows": len(base_glazing_rows),
            "gas_rows": len(base_gas_rows),
            "frame_rows": len(base_frame_rows),
        },
        "building_surface_summary": building_surface_summary,
        "wall_inventory_summary": wall_resolution.get("wall_inventory_summary", {}),
        "exterior_wall_alignment_summary": {
            **wall_export_geometry_summary,
            "qa_checks": {
                **dict(wall_export_geometry_summary.get("qa_checks", {})),
                "opening_host_alignment": opening_host_alignment_summary,
            },
        },
        "wall_export_geometry_summary": {
            **wall_export_geometry_summary,
            "qa_checks": {
                **dict(wall_export_geometry_summary.get("qa_checks", {})),
                "opening_host_alignment": opening_host_alignment_summary,
            },
        },
        "adiabatic_construction_summary": adiabatic_construction_summary,
        "wall_construction_library": (
            {
                "available": bool(wall_library.get("available")),
                "catalog_mode": wall_library.get("catalog_mode"),
                "construction_source": wall_library.get("construction_source"),
                "material_source": wall_library.get("material_source"),
                "resolver_rule_source": wall_library.get("resolver_rule_source"),
                "object_library_sources": wall_library.get("object_library_sources", {}),
            }
            if geometry_mode.startswith("dxf_inferred_")
            else {}
        ),
        "export_origin_mode": geometry_payload.get("export_origin_mode") or None,
        "export_origin_offset_m": export_origin_offset_m,
        "export_origin_reference": geometry_payload.get("export_origin_reference") or None,
        "zone_name_map": zone_manifest_rows,
        "opening_name_map": opening_manifest_rows,
        "manual_fill_required": [
            (
                "BuildingSurface_Detailed.csv and FenestrationSurface_Detailed.csv were inferred from the Apartment A raw DXF dimensions, outer block extents, and opening annotations, with interzone Surface adjacencies generated where zone boundaries align."
                if geometry_mode.startswith("dxf_inferred_")
                else "BuildingSurface_Detailed.csv contains placeholder rectangular zone boxes derived from zone labels and floor areas, not traced apartment geometry."
            ),
            (
                "Internal doors now generate paired FenestrationSurface objects across adjacent zone walls; exterior openings still need a visual review in DesignBuilder."
                if geometry_mode.startswith("dxf_inferred_")
                else "FenestrationSurface_Detailed.csv is auto-placed onto generated south walls and should be reviewed against real facade orientation and host surfaces."
            ),
            (
                (
                    "Wall and opening library rows now load from 1_input/library/idf_import catalogs, with optional object CSV defaults for Material, Construction, Glazing, Gas, and Frame tables; unmatched wall types still fall back to DXF-derived constructions."
                    if bool(wall_library.get("available"))
                    else "Wall Construction and Material rows now include DXF-derived entries generated from inferred wall thicknesses; WindowMaterial and Frame rows still come from the sample IDF defaults so the bundle can be imported and reviewed without placeholder thermal names."
                )
                if geometry_mode.startswith("dxf_inferred_")
                else "Construction, Material, WindowMaterial, and Frame tables were copied from the sample IDF defaults, so geometry can be reviewed without placeholder thermal names."
            ),
            (
                "The intermediate Wall_Inventory.csv lists each physical wall with total thickness including finish layers, inferred core thickness, and exported line position so external wall CSV data can be mapped onto the generated building surfaces."
                if geometry_mode.startswith("dxf_inferred_")
                else "Wall_Inventory.csv is not populated when the bundle falls back to placeholder sample geometry."
            ),
            (
                "Final wall export planes now come from wall_resolution.json with explicit reference metadata, and hosted fenestration rows are shifted with those resolved wall planes before the rebuilt IDF is written."
                if geometry_mode.startswith("dxf_inferred_")
                else "Wall export geometry is applied from wall_resolution.json when DXF-inferred geometry is available."
            ),
            (
                "The generated zone footprints are DXF-first rectilinear inferences partitioned from measured room cores and anchor-guided free cells; adiabatic wall segments are used where the drawing implies internal buffer space rather than modeled adjacent zones."
                if geometry_mode.startswith("dxf_inferred_")
                else "For DesignBuilder import or energy simulation, replace placeholder surfaces with traced geometry from the DXF extract."
            ),
        ],
    }
    apply_envelope_to_bundle_tables(project_id=project_id, tables=tables, manifest=manifest)
    manifest["prefilled_counts"]["construction_rows"] = len(tables.get("Construction.csv", []))
    manifest["prefilled_counts"]["material_rows"] = len(tables.get("Material.csv", []))
    manifest["prefilled_counts"]["simple_glazing_rows"] = len(
        tables.get("WindowMaterial_SimpleGlazingSystem.csv", [])
    )
    return {
        "tables": tables,
        "manifest": manifest,
    }


def write_bundle_outputs(
    *,
    bundle_output_dir: Path | str,
    bundle_artifacts: dict[str, object],
    project_id: str | None = None,
) -> tuple[list[Path], list[Path]]:
    tables = dict(bundle_artifacts.get("tables", {}))
    manifest = dict(bundle_artifacts.get("manifest", {}))
    resolved_bundle_output_dir = GUARD.resolve(bundle_output_dir)
    resolved_project_id = project_id or _infer_project_id_from_output_dir(resolved_bundle_output_dir)
    path_resolver.assert_output_in_project_scope(resolved_project_id, resolved_bundle_output_dir)
    bundle_written_paths: list[Path] = []
    intermediate_written_paths: list[Path] = []
    intermediate_output_dir = path_resolver.resolve_output_file(
        resolved_project_id,
        "intermediate",
        resolved_bundle_output_dir.name,
    )
    for filename, fieldnames in IDF_BUNDLE_TABLE_FIELDS.items():
        rows = list(tables.get(filename, []))
        content = render_csv_text(fieldnames, rows)
        target_path = resolved_bundle_output_dir / filename
        GUARD.write_text(
            target_path,
            content,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )
        bundle_written_paths.append(target_path)
    wall_inventory_rows = list(tables.get("Wall_Inventory.csv", []))
    wall_inventory_path = intermediate_output_dir / "Wall_Inventory.csv"
    GUARD.write_text(
        wall_inventory_path,
        render_csv_text(WALL_INVENTORY_FIELDS, wall_inventory_rows),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    intermediate_written_paths.append(wall_inventory_path)
    manifest_path = intermediate_output_dir / "bundle_manifest.json"
    GUARD.write_json(
        manifest_path,
        manifest,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    intermediate_written_paths.append(manifest_path)
    building_surface_rows = list(tables.get("BuildingSurface_Detailed.csv", []))
    surface_thicknesses = dict(dict(manifest.get("wall_thickness_inference", {})).get("surface_thicknesses", {}))
    if building_surface_rows and surface_thicknesses:
        wall_positions_path = intermediate_output_dir / "wall_positions.csv"
        build_positions_csv(
            building_surface_rows,
            surface_thicknesses,
            wall_positions_path,
        )
        if wall_positions_path.exists():
            intermediate_written_paths.append(wall_positions_path)
    return bundle_written_paths, intermediate_written_paths


def build_bundle_artifacts_from_paths(
    *,
    mapping_payload_path: Path,
    geometry_payload_path: Path,
    surface_rows_path: Path,
    wall_inventory_path: Path,
    wall_resolution_path: Path,
    fenestration_rows_path: Path,
    opening_host_mapping_path: Path | None = None,
    project_id: str | None = None,
) -> dict[str, object]:
    resolved_opening_host_mapping_path = opening_host_mapping_path
    if resolved_opening_host_mapping_path is None:
        sibling_path = fenestration_rows_path.parent / "opening_host_mapping.json"
        if sibling_path.exists():
            resolved_opening_host_mapping_path = sibling_path
    adjacency_summary_path = surface_rows_path.parent / "adjacency_summary.json"
    return build_bundle_artifacts(
        mapping_payload=load_json_object(mapping_payload_path),
        geometry_payload=load_json_object(geometry_payload_path),
        surface_rows=load_json_list(surface_rows_path),
        wall_inventory_rows=load_json_list(wall_inventory_path),
        wall_resolution=load_json_object(wall_resolution_path),
        fenestration_rows=load_json_list(fenestration_rows_path),
        opening_host_mapping_rows=(
            load_json_list(resolved_opening_host_mapping_path)
            if resolved_opening_host_mapping_path is not None and resolved_opening_host_mapping_path.exists()
            else []
        ),
        adjacency_summary=load_optional_json_object(adjacency_summary_path if adjacency_summary_path.exists() else None),
        source_paths={
            "mapping_payload": mapping_payload_path,
            "geometry_payload": geometry_payload_path,
            "surface_rows": surface_rows_path,
            "wall_inventory": wall_inventory_path,
            "wall_resolution": wall_resolution_path,
            "fenestration_rows": fenestration_rows_path,
            "opening_host_mapping": resolved_opening_host_mapping_path if resolved_opening_host_mapping_path is not None else "",
        },
        project_id=project_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble final CSV bundle and bundle manifest from intermediate artifacts.")
    parser.add_argument("--project", type=str, default=None)
    parser.add_argument("--mapping-payload", type=Path, default=None)
    parser.add_argument("--geometry-payload", type=Path, default=None)
    parser.add_argument("--surface-rows", type=Path, default=None)
    parser.add_argument("--wall-inventory", type=Path, default=None)
    parser.add_argument("--wall-resolution", type=Path, default=None)
    parser.add_argument("--fenestration-rows", type=Path, default=None)
    parser.add_argument("--opening-host-mapping", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)
    resolved_inputs = _resolve_required_bundle_inputs(project_id)
    opening_host_mapping_path = args.opening_host_mapping or _resolve_opening_host_mapping(project_id)
    bundle_artifacts = build_bundle_artifacts_from_paths(
        mapping_payload_path=args.mapping_payload or resolved_inputs["mapping_payload"],
        geometry_payload_path=args.geometry_payload or resolved_inputs["geometry_payload"],
        surface_rows_path=args.surface_rows or resolved_inputs["surface_rows"],
        wall_inventory_path=args.wall_inventory or resolved_inputs["wall_inventory"],
        wall_resolution_path=args.wall_resolution or resolved_inputs["wall_resolution"],
        fenestration_rows_path=args.fenestration_rows or resolved_inputs["fenestration_rows"],
        opening_host_mapping_path=opening_host_mapping_path if opening_host_mapping_path is not None and opening_host_mapping_path.exists() else None,
        project_id=project_id,
    )
    bundle_written_paths, intermediate_written_paths = write_bundle_outputs(
        bundle_output_dir=args.output_dir or _resolve_default_output_dir(project_id),
        bundle_artifacts=bundle_artifacts,
        project_id=project_id,
    )
    manifest = dict(bundle_artifacts.get("manifest", {}))
    counts = dict(manifest.get("prefilled_counts", {}))
    print("Bundle CSV files:", len(bundle_written_paths))
    print("Intermediate bundle artifacts:", len(intermediate_written_paths))
    print("Zone rows:", counts.get("zone_rows", 0))
    print("BuildingSurface rows:", counts.get("building_surface_rows", 0))
    print("Fenestration rows:", counts.get("fenestration_rows", 0))


if __name__ == "__main__":
    main()
