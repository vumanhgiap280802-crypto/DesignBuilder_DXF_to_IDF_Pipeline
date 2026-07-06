#!/usr/bin/env python3
"""
Final writer that rebuilds an IDF file from the generated bundle CSVs.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError
from utils import path_resolver


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root

DEFAULT_BUNDLE_INPUT_DIR = Path("5_output") / "<project_id>" / "csv" / "<bundle_dir>"
DEFAULT_IDF_OUTPUT = Path("5_output") / "<project_id>" / "idf" / "<project_id>_generated_from_bundle.idf"
REQUIRED_BUNDLE_FILES = ("Version.csv",)
ADIABATIC_HALF_CONSTRUCTION_SUFFIX = "_AdiabaticHalf"


def _resolve_single_bundle_dir(csv_root: Path, *, project_id: str) -> Path:
    candidates = sorted(
        directory
        for directory in csv_root.iterdir()
        if directory.is_dir() and all((directory / filename).exists() for filename in REQUIRED_BUNDLE_FILES)
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise WorkspaceRuleError(
            f"Multiple bundle directories found for project '{project_id}' in {csv_root.relative_to(ROOT)}."
        )
    raise WorkspaceRuleError(f"No bundle directory found for project '{project_id}' in {csv_root.relative_to(ROOT)}.")


def resolve_default_bundle_input_dir(project_id: str) -> Path:
    csv_root = path_resolver.resolve_output_dir_for_read(project_id, "csv")
    if csv_root is None:
        raise WorkspaceRuleError(f"No CSV output root found for project '{project_id}'.")
    return _resolve_single_bundle_dir(csv_root, project_id=project_id)


def resolve_default_idf_output_path(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "idf", f"{project_id}_generated_from_bundle.idf")


def resolve_bundle_input_dir(bundle_dir: Path | str) -> Path:
    """Resolve and validate the bundle directory used as rebuild input."""
    resolved = GUARD.resolve(bundle_dir)
    if not resolved.exists() or not resolved.is_dir():
        raise WorkspaceRuleError(f"Bundle input directory does not exist: {resolved}")
    for filename in REQUIRED_BUNDLE_FILES:
        GUARD.assert_read_path(resolved / filename)
    return resolved


def resolve_idf_output_path(output_path: Path | str, *, project_id: str | None = None) -> Path:
    """Resolve and validate the final IDF output path."""
    resolved = GUARD.resolve(output_path)
    GUARD.assert_write_path(
        resolved,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    if project_id is not None:
        path_resolver.assert_output_in_project_scope(project_id, resolved)
    return resolved


def read_csv_file(filepath: Path) -> list[dict[str, str]]:
    """Read CSV file and return list of dictionaries."""
    rows = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        raise WorkspaceRuleError(f"Failed to read CSV file {filepath}: {e}")
    return rows


def load_optional_json_object(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    resolved_path = GUARD.assert_read_path(path)
    if not resolved_path.exists():
        raise WorkspaceRuleError(f"Required JSON artifact is missing: {resolved_path}")
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceRuleError(f"Invalid JSON object: {resolved_path}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"JSON root must be an object: {resolved_path}")
    return payload


def escape_value(value: Any) -> str:
    """Escape value for IDF format."""
    if value is None or value == "":
        return ""
    return str(value).strip()


def get_ordered_layer_fields(row: dict[str, str]) -> list[str]:
    """Return layer fields sorted by layer index."""
    return sorted(
        [
            field_name
            for field_name in row
            if field_name.startswith("layer_") and field_name.split("_", 1)[-1].isdigit()
        ],
        key=lambda field_name: int(field_name.split("_", 1)[-1]),
    )


def reverse_construction_name(name: str) -> str:
    """Build the reverse construction name used by DesignBuilder."""
    return f"{name}_Rev"


def create_reversed_construction_row(row: dict[str, str]) -> dict[str, str]:
    """Create a construction row with reversed material layer order."""
    reversed_row = dict(row)
    base_name = escape_value(row.get("construction_name", ""))
    reversed_row["construction_name"] = reverse_construction_name(base_name)

    layer_fields = get_ordered_layer_fields(row)
    layers = [
        escape_value(row.get(field_name, ""))
        for field_name in layer_fields
        if escape_value(row.get(field_name, ""))
    ]
    reversed_layers = list(reversed(layers))

    for field_name in layer_fields:
        reversed_row[field_name] = ""
    for index, layer_name in enumerate(reversed_layers, start=1):
        reversed_row[f"layer_{index}"] = layer_name

    return reversed_row


def default_surface_view_factor(boundary_condition: str, current_value: str) -> str:
    """Match DesignBuilder's preferred explicit view-factor exports."""
    if current_value:
        return current_value
    if boundary_condition in {"Surface", "Adiabatic"}:
        return "0"
    return "AutoCalculate"


def default_fenestration_view_factor(current_value: str) -> str:
    """Use explicit zero view factor when the bundle leaves it blank."""
    return current_value or "0"


def collect_surface_vertices(row: dict[str, str], num_vertices: str) -> list[tuple[str, str, str]]:
    vertices = []
    for i in range(1, int(num_vertices) + 1):
        vx = escape_value(row.get(f"v{i}_x", ""))
        vy = escape_value(row.get(f"v{i}_y", ""))
        vz = escape_value(row.get(f"v{i}_z", ""))
        if vx and vy and vz:
            vertices.append((vx, vy, vz))
    return vertices


def designbuilder_adiabatic_construction_name(construction_name: str) -> str:
    if construction_name.endswith(ADIABATIC_HALF_CONSTRUCTION_SUFFIX):
        return construction_name[: -len(ADIABATIC_HALF_CONSTRUCTION_SUFFIX)]
    return construction_name


def build_building_surface_detailed_lines(
    row: dict[str, str],
    vertices: list[tuple[str, str, str]],
) -> list[str]:
    name = escape_value(row.get("surface_name", ""))
    surf_type = escape_value(row.get("surface_type", "Wall"))
    const_name = escape_value(row.get("construction_name", ""))
    zone_name = escape_value(row.get("zone_name", ""))
    boundary = escape_value(row.get("outside_boundary_condition", "Outdoors"))
    boundary_obj = escape_value(row.get("outside_boundary_condition_object", ""))
    sun_exp = escape_value(row.get("sun_exposure", "SunExposed"))
    wind_exp = escape_value(row.get("wind_exposure", "WindExposed"))
    view_factor = default_surface_view_factor(
        boundary,
        escape_value(row.get("view_factor_to_ground", "")),
    )

    is_adiabatic_wall = surf_type == "Wall" and boundary == "Adiabatic"
    if is_adiabatic_wall:
        const_name = designbuilder_adiabatic_construction_name(const_name)
        sun_exp = "NoSun"
        wind_exp = "NoWind"
        view_factor = "0"

    lines = [
        "BuildingSurface:Detailed,",
        f"  {name},",
        f"  {surf_type},",
        f"  {const_name},",
        f"  {zone_name},",
        f"  {boundary},",
        f"  {boundary_obj},",
        f"  {sun_exp},",
        f"  {wind_exp},",
        f"  {view_factor},",
        f"  {len(vertices)},",
    ]
    for i, (vx, vy, vz) in enumerate(vertices):
        if i < len(vertices) - 1:
            lines.append(f"  {vx},")
            lines.append(f"  {vy},")
            lines.append(f"  {vz},")
        else:
            lines.append(f"  {vx},")
            lines.append(f"  {vy},")
            lines.append(f"  {vz};")
    return lines


def prepare_interzone_surface_constructions(
    construction_rows: list[dict[str, str]],
    surface_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """
    Ensure paired interzone surfaces use forward/reverse constructions.

    DesignBuilder exports the second side of a `Surface`-linked pair with a
    dedicated `_Rev` construction name, even when the layers are symmetric.
    Rebuilding without that reverse construction can cause DesignBuilder to fall
    back to default project constructions on import.
    """
    if not construction_rows or not surface_rows:
        return construction_rows, surface_rows

    construction_lookup = {
        escape_value(row.get("construction_name", "")): row
        for row in construction_rows
        if escape_value(row.get("construction_name", ""))
    }
    surface_lookup = {
        escape_value(row.get("surface_name", "")): row
        for row in surface_rows
        if escape_value(row.get("surface_name", ""))
    }

    inserted_reverse_rows: dict[str, dict[str, str]] = {}
    processed_pairs: set[tuple[str, str]] = set()

    for row in surface_rows:
        boundary = escape_value(row.get("outside_boundary_condition", ""))
        if boundary.lower() != "surface":
            continue

        surface_name = escape_value(row.get("surface_name", ""))
        paired_surface_name = escape_value(row.get("outside_boundary_condition_object", ""))
        if not surface_name or not paired_surface_name:
            continue

        pair_key = tuple(sorted((surface_name, paired_surface_name)))
        if pair_key in processed_pairs:
            continue
        processed_pairs.add(pair_key)

        base_construction_name = escape_value(row.get("construction_name", ""))
        if not base_construction_name:
            continue

        paired_row = surface_lookup.get(paired_surface_name)
        if paired_row is None:
            continue

        paired_construction_name = escape_value(paired_row.get("construction_name", ""))
        if not paired_construction_name:
            continue

        if base_construction_name.lower().endswith("_rev") or paired_construction_name.lower().endswith("_rev"):
            continue
        if base_construction_name != paired_construction_name:
            continue

        reverse_name = reverse_construction_name(base_construction_name)
        if reverse_name not in construction_lookup:
            base_construction_row = construction_lookup.get(base_construction_name)
            if base_construction_row is None:
                continue
            reversed_row = create_reversed_construction_row(base_construction_row)
            inserted_reverse_rows[base_construction_name] = reversed_row
            construction_lookup[reverse_name] = reversed_row

        paired_row["construction_name"] = reverse_name

    if not inserted_reverse_rows:
        return construction_rows, surface_rows

    prepared_construction_rows: list[dict[str, str]] = []
    for row in construction_rows:
        prepared_construction_rows.append(row)
        name = escape_value(row.get("construction_name", ""))
        reverse_row = inserted_reverse_rows.get(name)
        if reverse_row is not None:
            prepared_construction_rows.append(reverse_row)

    return prepared_construction_rows, surface_rows


def prepare_adiabatic_surface_constructions(
    construction_rows: list[dict[str, str]],
    surface_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not construction_rows or not surface_rows:
        return construction_rows

    construction_lookup = {
        escape_value(row.get("construction_name", "")): row
        for row in construction_rows
        if escape_value(row.get("construction_name", ""))
    }
    inserted_reverse_rows: dict[str, dict[str, str]] = {}

    for row in surface_rows:
        if escape_value(row.get("surface_type", "")) != "Wall":
            continue
        boundary = escape_value(row.get("outside_boundary_condition", ""))
        if boundary != "Adiabatic":
            continue

        base_construction_name = designbuilder_adiabatic_construction_name(
            escape_value(row.get("construction_name", ""))
        )
        if not base_construction_name or base_construction_name.lower().endswith("_rev"):
            continue

        reverse_name = reverse_construction_name(base_construction_name)
        if reverse_name in construction_lookup:
            continue

        base_construction_row = construction_lookup.get(base_construction_name)
        if base_construction_row is None:
            continue

        reversed_row = create_reversed_construction_row(base_construction_row)
        inserted_reverse_rows[base_construction_name] = reversed_row
        construction_lookup[reverse_name] = reversed_row

    if not inserted_reverse_rows:
        return construction_rows

    prepared_construction_rows: list[dict[str, str]] = []
    for row in construction_rows:
        prepared_construction_rows.append(row)
        name = escape_value(row.get("construction_name", ""))
        reverse_row = inserted_reverse_rows.get(name)
        if reverse_row is not None:
            prepared_construction_rows.append(reverse_row)

    return prepared_construction_rows


def build_idf_from_bundle(
    bundle_dir: Path,
    *,
    wall_resolution: dict[str, object] | None = None,
    wall_resolution_path: Path | None = None,
) -> str:
    """Build complete IDF content from CSV bundle."""
    resolve_bundle_input_dir(bundle_dir)
    lines = []
    surface_file = bundle_dir / "BuildingSurface_Detailed.csv"
    surface_rows = read_csv_file(surface_file) if surface_file.exists() else []
    resolved_wall_resolution = dict(wall_resolution or {})
    if resolved_wall_resolution and surface_rows:
        if not isinstance(resolved_wall_resolution.get("surface_export_geometry_by_name"), dict):
            raise WorkspaceRuleError(
                "wall_resolution.json is missing surface_export_geometry_by_name for final rebuild."
            )
    
    # Header
    lines.append("! ============================================================================")
    lines.append("! Auto-generated IDF from CSV Bundle")
    if resolved_wall_resolution:
        wall_resolution_label = (
            str(wall_resolution_path)
            if wall_resolution_path is not None
            else "wall_resolution.json"
        )
        lines.append(f"! Wall geometry source: {wall_resolution_label}")
        lines.append(
            f"! Resolved wall count: {len(list(resolved_wall_resolution.get('resolved_walls', [])))}"
        )
    lines.append("! ============================================================================")
    lines.append("")
    
    # 1. Version
    version_file = bundle_dir / "Version.csv"
    if version_file.exists():
        rows = read_csv_file(version_file)
        if rows:
            row = rows[0]
            version = escape_value(row.get("version_identifier", "9.4.0.002"))
            lines.append("Version,")
            lines.append(f"  {version};")
            lines.append("")
    
    # 2. Site:Location
    site_file = bundle_dir / "Site_Location.csv"
    if site_file.exists():
        rows = read_csv_file(site_file)
        if rows:
            row = rows[0]
            name = escape_value(row.get("location_name", "Untitled"))
            lat = escape_value(row.get("latitude", "0"))
            lon = escape_value(row.get("longitude", "0"))
            tz = escape_value(row.get("time_zone", "0"))
            elev = escape_value(row.get("elevation_m", "0"))
            lines.append("Site:Location,")
            lines.append(f"  {name},")
            lines.append(f"  {lat},")
            lines.append(f"  {lon},")
            lines.append(f"  {tz},")
            lines.append(f"  {elev};")
            lines.append("")
    
    # 3. Building
    building_file = bundle_dir / "Building.csv"
    if building_file.exists():
        rows = read_csv_file(building_file)
        if rows:
            row = rows[0]
            name = escape_value(row.get("building_name", "Building"))
            north = escape_value(row.get("north_axis_deg", "0"))
            terrain = escape_value(row.get("terrain", "Suburbs"))
            loads_conv = escape_value(row.get("loads_convergence_tolerance", "0.04"))
            temp_conv = escape_value(row.get("temperature_convergence_tolerance", "0.4"))
            solar_dist = escape_value(row.get("solar_distribution", "FullExterior"))
            max_warmup = escape_value(row.get("maximum_warmup_days", "25"))
            min_warmup = escape_value(row.get("minimum_warmup_days", "6"))
            lines.append("Building,")
            lines.append(f"  {name},")
            lines.append(f"  {north},")
            lines.append(f"  {terrain},")
            lines.append(f"  {loads_conv},")
            lines.append(f"  {temp_conv},")
            lines.append(f"  {solar_dist},")
            lines.append(f"  {max_warmup},")
            lines.append(f"  {min_warmup};")
            lines.append("")
    
    # 4. GlobalGeometryRules
    geo_file = bundle_dir / "GlobalGeometryRules.csv"
    if geo_file.exists():
        rows = read_csv_file(geo_file)
        if rows:
            row = rows[0]
            start_vertex = escape_value(row.get("starting_vertex_position", "LowerLeftCorner"))
            vertex_dir = escape_value(row.get("vertex_entry_direction", "CounterClockWise"))
            coord_sys = escape_value(row.get("coordinate_system", "World"))
            lines.append("GlobalGeometryRules,")
            lines.append(f"  {start_vertex},")
            lines.append(f"  {vertex_dir},")
            lines.append(f"  {coord_sys};")
            lines.append("")
    
    # 5. Materials
    material_file = bundle_dir / "Material.csv"
    if material_file.exists():
        rows = read_csv_file(material_file)
        for row in rows:
            name = escape_value(row.get("material_name", ""))
            if not name:
                continue
            rough = escape_value(row.get("roughness", "Smooth"))
            thick = escape_value(row.get("thickness_m", "0.1"))
            cond = escape_value(row.get("conductivity_w_per_mk", "0.5"))
            dens = escape_value(row.get("density_kg_per_m3", "1000"))
            spec_heat = escape_value(row.get("specific_heat_j_per_kgk", "1000"))
            emit = escape_value(row.get("thermal_emittance", "0.9"))
            sol_abs = escape_value(row.get("solar_absorptance", "0.7"))
            vis_abs = escape_value(row.get("visible_absorptance", "0.7"))
            lines.append("Material,")
            lines.append(f"  {name},")
            lines.append(f"  {rough},")
            lines.append(f"  {thick},")
            lines.append(f"  {cond},")
            lines.append(f"  {dens},")
            lines.append(f"  {spec_heat},")
            lines.append(f"  {emit},")
            lines.append(f"  {sol_abs},")
            lines.append(f"  {vis_abs};")
            lines.append("")
    
    # 6. WindowMaterial:Glazing
    glazing_file = bundle_dir / "WindowMaterial_Glazing.csv"
    if glazing_file.exists():
        rows = read_csv_file(glazing_file)
        for row in rows:
            name = escape_value(row.get("glazing_name", ""))
            if not name:
                continue
            opt_type = escape_value(row.get("optical_data_type", "SpectralAverage"))
            spec_data = escape_value(row.get("spectral_data_set_name", ""))
            thick = escape_value(row.get("thickness_m", "0.006"))
            sol_trans = escape_value(row.get("solar_transmittance", "0.5"))
            sol_ref_f = escape_value(row.get("solar_reflectance_front", "0.1"))
            sol_ref_b = escape_value(row.get("solar_reflectance_back", "0.1"))
            vis_trans = escape_value(row.get("visible_transmittance", "0.5"))
            vis_ref_f = escape_value(row.get("visible_reflectance_front", "0.05"))
            vis_ref_b = escape_value(row.get("visible_reflectance_back", "0.05"))
            ir_trans = escape_value(row.get("ir_transmittance", "0"))
            ir_emit_f = escape_value(row.get("ir_emissivity_front", "0.84"))
            ir_emit_b = escape_value(row.get("ir_emissivity_back", "0.84"))
            cond = escape_value(row.get("conductivity_w_per_mk", "0.9"))
            dirt = escape_value(row.get("dirt_correction_factor", "1"))
            lines.append("WindowMaterial:Glazing,")
            lines.append(f"  {name},")
            lines.append(f"  {opt_type},")
            lines.append(f"  {spec_data},")
            lines.append(f"  {thick},")
            lines.append(f"  {sol_trans},")
            lines.append(f"  {sol_ref_f},")
            lines.append(f"  {sol_ref_b},")
            lines.append(f"  {vis_trans},")
            lines.append(f"  {vis_ref_f},")
            lines.append(f"  {vis_ref_b},")
            lines.append(f"  {ir_trans},")
            lines.append(f"  {ir_emit_f},")
            lines.append(f"  {ir_emit_b},")
            lines.append(f"  {cond},")
            lines.append(f"  {dirt};")
            lines.append("")

    # 6b. WindowMaterial:SimpleGlazingSystem
    simple_glazing_file = bundle_dir / "WindowMaterial_SimpleGlazingSystem.csv"
    if simple_glazing_file.exists():
        rows = read_csv_file(simple_glazing_file)
        for row in rows:
            name = escape_value(row.get("simple_glazing_name", ""))
            if not name:
                continue
            u_factor = escape_value(row.get("u_factor_w_per_m2k", ""))
            shgc = escape_value(row.get("solar_heat_gain_coefficient", ""))
            visible_trans = escape_value(row.get("visible_transmittance", ""))
            lines.append("WindowMaterial:SimpleGlazingSystem,")
            lines.append(f"  {name},")
            lines.append(f"  {u_factor},")
            lines.append(f"  {shgc},")
            lines.append(f"  {visible_trans};")
            lines.append("")
    
    # 7. WindowMaterial:Gas
    gas_file = bundle_dir / "WindowMaterial_Gas.csv"
    if gas_file.exists():
        rows = read_csv_file(gas_file)
        for row in rows:
            name = escape_value(row.get("gas_layer_name", ""))
            if not name:
                continue
            gas_type = escape_value(row.get("gas_type", "Air"))
            thick = escape_value(row.get("thickness_m", "0.013"))
            lines.append("WindowMaterial:Gas,")
            lines.append(f"  {name},")
            lines.append(f"  {gas_type},")
            lines.append(f"  {thick};")
            lines.append("")
    
    # 8. WindowProperty:FrameAndDivider
    frame_file = bundle_dir / "WindowProperty_FrameAndDivider.csv"
    if frame_file.exists():
        rows = read_csv_file(frame_file)
        for row in rows:
            name = escape_value(row.get("frame_divider_name", ""))
            if not name:
                continue
            width = escape_value(row.get("frame_width_m", "0.04"))
            lines.append("WindowProperty:FrameAndDivider,")
            lines.append(f"  {name},")
            lines.append(f"  {width};")
            lines.append("")
    
    # 9. Construction
    construction_file = bundle_dir / "Construction.csv"
    if construction_file.exists():
        rows = read_csv_file(construction_file)
        rows, surface_rows = prepare_interzone_surface_constructions(rows, surface_rows)
        rows = prepare_adiabatic_surface_constructions(rows, surface_rows)
        for row in rows:
            name = escape_value(row.get("construction_name", ""))
            if not name:
                continue
            layer_fields = get_ordered_layer_fields(row)
            layers = [
                escape_value(row.get(field_name, ""))
                for field_name in layer_fields
                if escape_value(row.get(field_name, ""))
            ]
            lines.append("Construction,")
            if not layers:
                lines.append(f"  {name};")
                lines.append("")
                continue
            lines.append(f"  {name},")
            for index, layer_name in enumerate(layers):
                suffix = ";" if index == len(layers) - 1 else ","
                lines.append(f"  {layer_name}{suffix}")
            lines.append("")
    
    # 10. Zones
    zone_file = bundle_dir / "Zone.csv"
    if zone_file.exists():
        rows = read_csv_file(zone_file)
        for row in rows:
            name = escape_value(row.get("zone_name", ""))
            if not name:
                continue
            north = escape_value(row.get("relative_north_deg", "0"))
            x = escape_value(row.get("x_origin_m", "0"))
            y = escape_value(row.get("y_origin_m", "0"))
            z = escape_value(row.get("z_origin_m", "0"))
            zone_type = escape_value(row.get("zone_type", "1"))
            mult = escape_value(row.get("zone_multiplier", "1"))
            ceil_h = escape_value(row.get("ceiling_height_m", "3.0"))
            volume = escape_value(row.get("volume_m3", "0"))
            floor_area = escape_value(row.get("floor_area_m2", "0"))
            conv_in = escape_value(row.get("inside_convection_algorithm", "TARP"))
            conv_out = escape_value(row.get("outside_convection_algorithm", ""))
            part_floor = escape_value(row.get("part_of_total_floor_area", "Yes"))
            lines.append("Zone,")
            lines.append(f"  {name},")
            lines.append(f"  {north},")
            lines.append(f"  {x},")
            lines.append(f"  {y},")
            lines.append(f"  {z},")
            lines.append(f"  {zone_type},")
            lines.append(f"  {mult},")
            lines.append(f"  {ceil_h},")
            lines.append(f"  {volume},")
            lines.append(f"  {floor_area},")
            lines.append(f"  {conv_in},")
            lines.append(f"  {conv_out},")
            lines.append(f"  {part_floor};")
            lines.append("")
    
    # 11. BuildingSurface:Detailed
    if surface_rows:
        rows = surface_rows
        for row in rows:
            name = escape_value(row.get("surface_name", ""))
            if not name:
                continue
            num_vertices = escape_value(row.get("number_of_vertices", "3"))
            vertices = collect_surface_vertices(row, num_vertices)
            lines.extend(build_building_surface_detailed_lines(row, vertices))
            lines.append("")
    
    # 12. FenestrationSurface:Detailed
    fen_file = bundle_dir / "FenestrationSurface_Detailed.csv"
    if fen_file.exists():
        rows = read_csv_file(fen_file)
        for row in rows:
            name = escape_value(row.get("fenestration_name", ""))
            if not name:
                continue
            surf_type = escape_value(row.get("surface_type", "Window"))
            const_name = escape_value(row.get("construction_name", ""))
            host_surf = escape_value(row.get("building_surface_name", ""))
            boundary_obj = escape_value(row.get("outside_boundary_condition_object", ""))
            view_factor = default_fenestration_view_factor(
                escape_value(row.get("view_factor_to_ground", ""))
            )
            frame_name = escape_value(row.get("frame_and_divider_name", ""))
            mult = escape_value(row.get("multiplier", "1"))
            num_vertices = escape_value(row.get("number_of_vertices", "4"))
            vertices = collect_surface_vertices(row, num_vertices)
            
            lines.append("FenestrationSurface:Detailed,")
            lines.append(f"  {name},")
            lines.append(f"  {surf_type},")
            lines.append(f"  {const_name},")
            lines.append(f"  {host_surf},")
            lines.append(f"  {boundary_obj},")
            lines.append(f"  {view_factor},")
            lines.append(f"  {frame_name},")
            lines.append(f"  {mult},")
            lines.append(f"  {len(vertices)},")
            for i, (vx, vy, vz) in enumerate(vertices):
                if i < len(vertices) - 1:
                    lines.append(f"  {vx},")
                    lines.append(f"  {vy},")
                    lines.append(f"  {vz},")
                else:
                    lines.append(f"  {vx},")
                    lines.append(f"  {vy},")
                    lines.append(f"  {vz};")
            lines.append("")
    
    return "\n".join(lines)


def rebuild_idf_from_bundle(
    bundle_dir: Path | str = DEFAULT_BUNDLE_INPUT_DIR,
    *,
    output_path: Path | str = DEFAULT_IDF_OUTPUT,
    wall_resolution_path: Path | str | None = None,
    project_id: str | None = None,
) -> Path:
    """Validate bundle input, rebuild the IDF text, and write the final file."""
    resolved_bundle_dir = resolve_bundle_input_dir(bundle_dir)
    resolved_output_path = resolve_idf_output_path(output_path, project_id=project_id)
    resolved_wall_resolution_path = None
    if wall_resolution_path not in {None, ""}:
        resolved_wall_resolution_path = GUARD.assert_read_path(wall_resolution_path)
    idf_content = build_idf_from_bundle(
        resolved_bundle_dir,
        wall_resolution=load_optional_json_object(resolved_wall_resolution_path),
        wall_resolution_path=resolved_wall_resolution_path,
    )
    GUARD.write_text(
        resolved_output_path,
        idf_content,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    return resolved_output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild IDF file from CSV bundle."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve project-scoped bundle and IDF output paths.",
    )
    parser.add_argument(
        "--input-dir",
        default=None,
        help="Input CSV bundle directory. If omitted, resolves from 5_output/<project_id>/csv.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output IDF file to rebuild. If omitted, defaults to 5_output/<project_id>/idf/<project_id>_generated_from_bundle.idf.",
    )
    parser.add_argument(
        "--wall-resolution",
        default="",
        help="Optional wall_resolution.json path for final wall geometry provenance and validation.",
    )
    
    args = parser.parse_args()
    
    project_id = path_resolver.resolve_project_id(args.project)
    bundle_dir = resolve_bundle_input_dir(args.input_dir or resolve_default_bundle_input_dir(project_id))
    output_path = resolve_idf_output_path(args.output or resolve_default_idf_output_path(project_id), project_id=project_id)

    print(f"Reading CSV bundle from: {bundle_dir.relative_to(ROOT)}")
    print(f"Rebuilding IDF file: {output_path.relative_to(ROOT)}")
    rebuild_idf_from_bundle(
        bundle_dir,
        output_path=output_path,
        wall_resolution_path=args.wall_resolution or None,
        project_id=project_id,
    )

    print(f"IDF_REBUILD_COMPLETE")
    print(f"Output: {output_path.relative_to(ROOT)}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
