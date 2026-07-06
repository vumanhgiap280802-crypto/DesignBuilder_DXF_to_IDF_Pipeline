#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402

from context.mapping_builder import (  # noqa: E402
    build_mapping_payload,
    opening_type_from_layer_name,
    summarize_opening_attributes,
)
from parsers.dxf_raw_parser import (  # noqa: E402
    DEFAULT_DXF_LAYER_PROFILE,
    Record,
    classify_record_layer,
    load_layer_profile,
    parse_dxf_file,
)
from pipeline.apartment_a_pipeline import (  # noqa: E402
    build_block_definitions,
    build_parser_candidates,
    collect_block_names_from_records,
    filter_apartment_records,
    filter_block_definitions_for_idf,
    resolve_block_dependencies,
)
from transformers.fenestration_builder import (  # noqa: E402
    build_fenestration_row_for_host,
    choose_host_wall_for_opening,
    choose_placement_anchor_for_host,
    resolve_frame_width_m,
    resolve_opening_dimensions_for_surface_mm,
    resolve_opening_dimensions_mm,
)
from transformers.geometry_inference import backfill_missing_layer_boundary_zones  # noqa: E402
from transformers.wall_logic import (  # noqa: E402
    apply_layer_based_adiabatic_boundaries,
    build_parser_layer_segments_by_axis,
    build_wall_host_collections,
    build_surface_shell_closure_summary,
    build_wall_resolution_rows,
    wall_reference_offset_m,
)
from writers.bundle_writer import (  # noqa: E402
    build_dynamic_wall_library,
    build_zone_rows_and_manifest,
    preserve_designbuilder_adiabatic_full_constructions,
    reorder_vertical_face_vertices_for_idf,
)
from writers.rebuild_idf_from_bundle import (  # noqa: E402
    build_building_surface_detailed_lines,
    prepare_adiabatic_surface_constructions,
)


GUARD = WorkspaceGuard(__file__)
WORKSPACE_ROOT = GUARD.root
NOXH_CASE_ID = "noxh_apartment_a_clean"
NOXH_READY_SOURCE = (
    WORKSPACE_ROOT / "1_input" / NOXH_CASE_ID / "clean" / "txt_dxf" / "NOXH_Apartment_A_clean.dxf"
)
NOXH_OUTPUT_ROOT = WORKSPACE_ROOT / "5_output" / NOXH_CASE_ID


class QAFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class QACheck:
    name: str
    description: str
    kind: str
    runner: Callable[[], None]


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise QAFailure(message)


def ensure_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise QAFailure(f"{message}: expected {expected!r}, got {actual!r}")


def ensure_close(
    actual: float,
    expected: float,
    *,
    abs_tol: float = 1e-6,
    rel_tol: float = 0.0,
    message: str,
) -> None:
    if not math.isclose(actual, expected, abs_tol=abs_tol, rel_tol=rel_tol):
        raise QAFailure(f"{message}: expected {expected!r}, got {actual!r}")


def require_path(path: Path) -> Path:
    try:
        return GUARD.assert_read_path(path)
    except Exception as exc:
        raise QAFailure(f"Required path is missing or invalid: {path}") from exc


def load_json(path: Path) -> dict[str, object]:
    return json.loads(require_path(path).read_text(encoding="utf-8"))


def build_noxh_case_context() -> tuple[dict[str, object], dict[str, object]]:
    layer_profile = load_layer_profile(DEFAULT_DXF_LAYER_PROFILE)
    parsed = parse_dxf_file(NOXH_READY_SOURCE, layer_profile_path=DEFAULT_DXF_LAYER_PROFILE)
    records = list(parsed.get("records", []))

    parser_candidates = build_parser_candidates(records, layer_profile, padding=2500.0)
    selection_bbox = tuple(float(value) for value in parser_candidates.get("selection_bbox", []))
    kept_records, filter_summary = filter_apartment_records(records, selection_bbox, layer_profile, parser_candidates)

    block_defs = build_block_definitions(records)
    referenced_block_defs = resolve_block_dependencies(collect_block_names_from_records(kept_records), block_defs)
    referenced_block_defs = filter_block_definitions_for_idf(referenced_block_defs, layer_profile)

    dxf_context = {
        "filtered_records": kept_records,
        "block_defs": referenced_block_defs,
        "records_by_handle": {record.handle: record for record in records if record.handle},
        "filter_summary": filter_summary,
        "room_label_candidates": list(parser_candidates.get("room_label_candidates", [])),
        "title_candidates": list(parser_candidates.get("title_candidates", [])),
        "boundary_candidates": list(parser_candidates.get("boundary_candidates", [])),
        "apartment_extent_candidates": list(parser_candidates.get("apartment_extent_candidates", [])),
        "opening_evidence_candidates": list(parser_candidates.get("opening_candidates", [])),
        "parser_warnings": list(parser_candidates.get("parser_warnings", [])),
        "fallback_usage": dict(parser_candidates.get("fallback_usage", {})),
    }
    return dxf_context, layer_profile


def attrib_record(handle: str, text_value: str, x: float, y: float) -> Record:
    return Record(
        section="FILTERED_RECORDS",
        record_type="ATTRIB",
        raw_lines=[],
        start_pair_index=0,
        end_pair_index=0,
        layer="TAC - CUA+LC",
        handle=handle,
        text_values=[text_value],
        points=[(x, y, 0.0)],
    )


def make_inventory_row(
    *,
    wall_id: str,
    boundary_condition: str,
    surface_name_primary: str,
    start_xy_m: tuple[float, float],
    end_xy_m: tuple[float, float],
    side_primary: str,
    thickness_mm: int,
    wall_role: str,
    surface_name_secondary: str = "",
    zone_name_primary: str = "ZONE_A",
    zone_name_secondary: str = "",
) -> dict[str, object]:
    return {
        "physical_wall_id": wall_id,
        "boundary_condition": boundary_condition,
        "wall_family": "Exterior" if boundary_condition == "Outdoors" else "Partition",
        "wall_role": wall_role,
        "total_thickness_mm": thickness_mm,
        "surface_name_primary": surface_name_primary,
        "surface_name_secondary": surface_name_secondary,
        "zone_name_primary": zone_name_primary,
        "zone_name_secondary": zone_name_secondary,
        "side_primary": side_primary,
        "axis": "horizontal" if abs(start_xy_m[1] - end_xy_m[1]) <= 1e-9 else "vertical",
        "start_x_m": start_xy_m[0],
        "start_y_m": start_xy_m[1],
        "end_x_m": end_xy_m[0],
        "end_y_m": end_xy_m[1],
        "height_m": 3.0,
        "position_basis": (
            "shared_zone_boundary_source_edge"
            if boundary_condition == "Surface"
            else "zone_boundary_source_edge"
        ),
    }


def make_host_wall(
    *,
    surface_name: str,
    zone_name: str,
    start_xy_m: tuple[float, float],
    end_xy_m: tuple[float, float],
    side: str,
    boundary_condition: str,
) -> dict[str, object]:
    return {
        "surface_name": surface_name,
        "zone_name": zone_name,
        "start": [start_xy_m[0], start_xy_m[1]],
        "end": [end_xy_m[0], end_xy_m[1]],
        "side": side,
        "height_m": 3.0,
        "boundary_condition": boundary_condition,
    }


def check_opening_annotations_outside_shell_bbox_kept() -> None:
    layer_profile = load_layer_profile(DEFAULT_DXF_LAYER_PROFILE)
    parsed = parse_dxf_file(NOXH_READY_SOURCE, layer_profile_path=DEFAULT_DXF_LAYER_PROFILE)
    records = list(parsed.get("records", []))

    parser_candidates = build_parser_candidates(records, layer_profile, padding=2500.0)
    selection_bbox = tuple(float(value) for value in parser_candidates.get("selection_bbox", []))
    kept_records, _summary = filter_apartment_records(records, selection_bbox, layer_profile, parser_candidates)
    kept_handles = {record.handle for record in kept_records if record.handle}

    expected_handles = {"2F61", "2F68", "2F69", "2F6A", "2F6C", "2F73", "2F74", "2F75"}
    missing = expected_handles - kept_handles
    ensure(not missing, f"Missing opening annotation handles from filtered extract: {sorted(missing)}")


def check_explicit_opening_geometry_layers_are_kept() -> None:
    dxf_context, _layer_profile = build_noxh_case_context()
    kept_handles = {record.handle for record in dxf_context["filtered_records"] if getattr(record, "handle", "")}
    expected_geometry_handles = {"4BA8", "4BAF", "4BB0", "4BB1", "4BB2", "4BB3", "4BB4", "4BB5", "4BB7"}
    missing = expected_geometry_handles - kept_handles
    ensure(not missing, f"Missing explicit opening-geometry handles from filtered extract: {sorted(missing)}")


def check_hole_as_window_layer_is_modeled_as_hole() -> None:
    ensure_equal(
        opening_type_from_layer_name("EM_HOLE_AS_WINDOW"),
        "Hole",
        "EM_HOLE_AS_WINDOW layer type mismatch",
    )
    ensure_equal(opening_type_from_layer_name("EM_WINDOW"), "Window", "EM_WINDOW layer type mismatch")


def check_opening_candidates_prefer_explicit_layers_with_nearby_text() -> None:
    dxf_context, layer_profile = build_noxh_case_context()
    mapping_payload = build_mapping_payload(
        dxf_context=dxf_context,
        layer_profile=layer_profile,
    )

    openings_by_owner = {
        str(opening.get("annotation_owner_handle", "")): opening
        for opening in list(mapping_payload.get("candidate_openings", []))
        if isinstance(opening, dict) and opening.get("annotation_owner_handle")
    }

    ensure_equal(len(openings_by_owner), 9, "Unexpected opening count")
    ensure_equal(
        {zone.get("zone_key") for zone in list(mapping_payload.get("candidate_zones", [])) if isinstance(zone, dict)},
        {"LOGIA", "PK_PB", "PN_01", "PN_02", "WC_01", "WC_02"},
        "Unexpected candidate zones",
    )
    ensure_equal(openings_by_owner["2F61"]["matched_opening_geometry_handle"], "4BB0", "Opening 2F61 geometry handle mismatch")
    ensure_equal(openings_by_owner["2F61"]["opening_detection_method"], "opening_layer_nearby_text", "Opening 2F61 detection method mismatch")
    ensure_equal(openings_by_owner["2F61"]["anchor_xy"], openings_by_owner["2F61"]["matched_opening_geometry_anchor_xy"], "Opening 2F61 anchor mismatch")
    ensure_equal(openings_by_owner["2F77"]["matched_opening_geometry_handle"], "4BB3", "Opening 2F77 geometry handle mismatch")
    ensure_equal(openings_by_owner["2FBE"]["matched_opening_geometry_handle"], "4BB2", "Opening 2FBE geometry handle mismatch")
    ensure_equal(openings_by_owner["2FA6"]["matched_opening_geometry_handle"], "4BA8", "Opening 2FA6 geometry handle mismatch")
    ensure_equal(openings_by_owner["2FA6"]["candidate_fenestration_type"], "GlassDoor", "Opening 2FA6 type mismatch")
    ensure_equal(openings_by_owner["2FCA"]["matched_opening_geometry_handle"], "4BB7", "Opening 2FCA geometry handle mismatch")
    ensure_equal(openings_by_owner["2FCA"]["matched_opening_geometry_type_conflict"], True, "Opening 2FCA conflict flag mismatch")
    ensure_equal(openings_by_owner["2FCA"]["candidate_fenestration_type"], "Door", "Opening 2FCA type mismatch")
    ensure("2FB1" not in openings_by_owner, "Opening 2FB1 should not survive candidate selection")


def check_opening_geometry_bbox_selects_correct_interzone_host_wall() -> None:
    opening = {
        "candidate_fenestration_type": "Door",
        "type_code": "DN1",
        "width_mm": 850,
        "matched_opening_geometry_bbox_mm": [4245.163, 6040.038, 5095.163, 6180.038],
        "anchor_xy": [4670.163, 6110.038],
        "annotation_anchor_xy": [4440.889, 6042.181],
        "matched_symbol_anchor_xy": [4440.889, 6042.181],
    }
    zone_rectangles = {
        "APARTMENT_A_PK_PB": [(4.090, 4.550, 8.655, 6.110)],
        "APARTMENT_A_PN_01": [(1.350, 6.110, 5.220, 9.560)],
        "APARTMENT_A_WC_01": [(1.350, 4.550, 4.090, 6.110)],
    }
    host_walls = [
        {
            "surface_name": "APARTMENT_A_PK_PB_WALL_03",
            "zone_name": "APARTMENT_A_PK_PB",
            "adjacent_zone_name": "APARTMENT_A_PN_01",
            "boundary_condition": "Surface",
            "start": [5.220, 6.110],
            "end": [4.090, 6.110],
            "length_m": 1.130,
            "is_estimated_opening_host": True,
        },
        {
            "surface_name": "APARTMENT_A_PN_01_WALL_02",
            "zone_name": "APARTMENT_A_PN_01",
            "adjacent_zone_name": "APARTMENT_A_PK_PB",
            "boundary_condition": "Surface",
            "start": [4.090, 6.110],
            "end": [5.220, 6.110],
            "length_m": 1.130,
            "is_estimated_opening_host": True,
        },
        {
            "surface_name": "APARTMENT_A_PK_PB_WALL_06",
            "zone_name": "APARTMENT_A_PK_PB",
            "adjacent_zone_name": "APARTMENT_A_PN_02",
            "boundary_condition": "Surface",
            "start": [5.220, 4.550],
            "end": [5.220, 1.130],
            "length_m": 3.420,
            "is_estimated_opening_host": True,
        },
    ]

    host_wall, placement_anchor_xy_m, manifest = choose_host_wall_for_opening(
        opening=opening,
        source_zone_name="WC 01",
        source_zone_csv_name="APARTMENT_A_WC_01",
        source_zone_anchor_xy_m=None,
        zone_anchor_xy_by_name={},
        zone_rectangles_by_name=zone_rectangles,
        host_walls=host_walls,
    )

    ensure(host_wall is not None, "Expected a host wall for opening geometry test")
    ensure_equal(host_wall["surface_name"], "APARTMENT_A_PK_PB_WALL_03", "Selected host wall mismatch")
    ensure(placement_anchor_xy_m is not None, "Expected placement anchor from opening geometry")
    ensure_close(float(placement_anchor_xy_m[0]), 4.670163, abs_tol=1e-6, message="Placement anchor x mismatch")
    ensure_close(float(placement_anchor_xy_m[1]), 6.110, abs_tol=1e-6, message="Placement anchor y mismatch")
    ensure_equal(manifest["host_selection_anchor_source"], "opening_geometry", "Host selection anchor source mismatch")


def check_placement_anchor_prefers_projected_opening_layer_geometry() -> None:
    opening = {
        "candidate_fenestration_type": "Window",
        "width_mm": 600,
        "matched_opening_geometry_bbox_mm": [1400.163, 5430.038, 1500.163, 6030.038],
        "matched_symbol_anchor_xy": [1690.163, 5409.204],
    }
    host_wall = {
        "surface_name": "APARTMENT_A_WC_01_WALL_04",
        "zone_name": "APARTMENT_A_WC_01",
        "adjacent_zone_name": "",
        "boundary_condition": "Outdoors",
        "start": [1.350, 6.110],
        "end": [1.350, 4.550],
        "length_m": 1.560,
    }

    placement_anchor_xy_m, placement_anchor_source, width_override_m = choose_placement_anchor_for_host(
        opening=opening,
        host_wall=host_wall,
        primary_anchor_xy_m=(1.690163, 5.409204),
        placement_segments=[],
        opening_width_m=0.6,
    )

    ensure(placement_anchor_xy_m is not None, "Expected a projected placement anchor")
    ensure_close(float(placement_anchor_xy_m[0]), 1.350, abs_tol=1e-6, message="Projected anchor x mismatch")
    ensure_close(float(placement_anchor_xy_m[1]), 5.730038, abs_tol=1e-6, message="Projected anchor y mismatch")
    ensure_equal(placement_anchor_source, "opening_layer_geometry", "Placement anchor source mismatch")
    ensure(width_override_m is None, "Did not expect a width override from opening layer geometry")


def check_window_glazing_vertices_are_reduced_by_frame_width() -> None:
    host_wall = {
        "surface_name": "APARTMENT_A_PN_02_WALL_05",
        "zone_name": "APARTMENT_A_PN_02",
        "adjacent_zone_name": "",
        "boundary_condition": "Outdoors",
        "start": [0.0, 3.420],
        "end": [0.0, 0.0],
        "length_m": 3.420,
        "height_m": 3.0,
    }

    payload = build_fenestration_row_for_host(
        fenestration_name="APARTMENT_A_OPENING_002_WINDOW",
        surface_type="Window",
        construction_name="Dbl LoE (e2=.1) Tint 6mm/13mm Arg - 1001",
        frame_and_divider_name="1",
        host_wall=host_wall,
        opening_width_m=1.4,
        opening_height_m=1.235,
        sill_height_m=1.4,
        anchor_xy_m=(0.0, 2.03),
    )

    ensure(payload is not None, "Expected fenestration row payload")
    row, manifest = payload

    frame_width_m = resolve_frame_width_m("1")
    expected_glazing_width_m = 1.4 - (2.0 * frame_width_m)
    expected_glazing_height_m = 1.235 - (2.0 * frame_width_m)
    expected_bottom_z_m = 1.4 + frame_width_m
    expected_top_z_m = expected_bottom_z_m + expected_glazing_height_m
    expected_v1_y = 2.03 + (expected_glazing_width_m / 2.0)
    expected_v4_y = 2.03 - (expected_glazing_width_m / 2.0)

    ensure_equal(row["v1_y"], f"{expected_v1_y:.3f}", "Window v1_y mismatch")
    ensure_equal(row["v4_y"], f"{expected_v4_y:.3f}", "Window v4_y mismatch")
    ensure_equal(row["v1_z"], f"{expected_bottom_z_m:.3f}", "Window v1_z mismatch")
    ensure_equal(row["v3_z"], f"{expected_top_z_m:.3f}", "Window v3_z mismatch")
    ensure_close(float(manifest["frame_width_m"]), frame_width_m, abs_tol=1e-6, message="Frame width manifest mismatch")
    ensure_close(float(manifest["opening_total_width_m"]), 1.4, abs_tol=1e-6, message="Total opening width mismatch")
    ensure_close(float(manifest["opening_glazing_width_m"]), expected_glazing_width_m, abs_tol=1e-6, message="Glazing width mismatch")
    ensure_close(float(manifest["opening_total_height_m"]), 1.235, abs_tol=1e-6, message="Total opening height mismatch")
    ensure_close(float(manifest["opening_glazing_height_m"]), expected_glazing_height_m, abs_tol=1e-6, message="Glazing height mismatch")
    ensure_close(float(manifest["opening_total_bottom_z_m"]), 1.4, abs_tol=1e-6, message="Opening total bottom z mismatch")


def check_zone_rows_include_surface_only_logia_zone() -> None:
    mapping_payload = {
        "candidate_zones": [
            {
                "zone_name": "PK + PB",
                "zone_key": "PK_PB",
                "area_m2": 20.0,
                "anchor_xy": [6858.084, 6329.398],
            }
        ],
        "candidate_openings": [],
    }
    geometry_payload = {
        "zone_geometry_by_key": {
            "LOGIA": {
                "zone_key": "LOGIA",
                "source_zone_name": "",
                "footprint_area_m2": 3.943,
                "footprint_rectangles_m": [[5.15, 8.435, 8.655, 9.56]],
            }
        },
        "zone_rectangles_m_by_key": {
            "LOGIA": [[5.15, 8.435, 8.655, 9.56]],
        },
        "source_zone_name_by_key": {
            "PK_PB": "PK + PB",
        },
        "export_origin_offset_m": [1.35, 1.13, 0.0],
        "export_origin_mode": "outer_corner",
        "export_origin_reference": "APARTMENT_A",
    }
    surface_rows = [
        {
            "surface_name": "APARTMENT_A_LOGIA_WALL_04",
            "surface_type": "Wall",
            "zone_name": "APARTMENT_A_LOGIA",
            "outside_boundary_condition": "Outdoors",
            "outside_boundary_condition_object": "",
            "v1_x": "8.655",
            "v1_y": "9.560",
            "v1_z": "0.000",
            "v2_x": "8.655",
            "v2_y": "9.560",
            "v2_z": "3.300",
            "v3_x": "5.150",
            "v3_y": "9.560",
            "v3_z": "3.300",
            "v4_x": "5.150",
            "v4_y": "9.560",
            "v4_z": "0.000",
            "inferred_wall_thickness_mm": 250,
        },
        {
            "surface_name": "APARTMENT_A_PK_PB_WALL_01",
            "surface_type": "Wall",
            "zone_name": "APARTMENT_A_PK_PB",
            "outside_boundary_condition": "Outdoors",
            "outside_boundary_condition_object": "",
            "v1_x": "4.090",
            "v1_y": "4.550",
            "v1_z": "0.000",
            "v2_x": "4.090",
            "v2_y": "4.550",
            "v2_z": "3.300",
            "v3_x": "8.655",
            "v3_y": "4.550",
            "v3_z": "3.300",
            "v4_x": "8.655",
            "v4_y": "4.550",
            "v4_z": "0.000",
            "inferred_wall_thickness_mm": 250,
        }
    ]

    zone_rows, zone_manifest_rows, zone_name_map = build_zone_rows_and_manifest(
        mapping_payload=mapping_payload,
        geometry_payload=geometry_payload,
        surface_rows=surface_rows,
        adjacency_summary={},
    )

    zone_names = {row["zone_name"] for row in zone_rows}
    ensure("APARTMENT_A_LOGIA" in zone_names, "LOGIA zone row is missing")
    ensure_equal(zone_name_map["LOGIA"], "APARTMENT_A_LOGIA", "LOGIA zone name mapping mismatch")
    logia_manifest = next(row for row in zone_manifest_rows if row["csv_zone_name"] == "APARTMENT_A_LOGIA")
    ensure_equal(logia_manifest["source_zone_name"], "LOGIA", "LOGIA source zone mismatch")
    ensure_close(float(logia_manifest["footprint_area_m2"]), 3.943, abs_tol=1e-6, message="LOGIA footprint area mismatch")


def check_em_wall_layers_keep_their_own_canonical_layer() -> None:
    layer_profile = load_layer_profile(DEFAULT_DXF_LAYER_PROFILE)
    external_record = Record(
        section="FILTERED_RECORDS",
        record_type="LWPOLYLINE",
        raw_lines=[],
        start_pair_index=0,
        end_pair_index=0,
        layer="EM_EXTERNAL_WALL_180",
    )
    internal_record = Record(
        section="FILTERED_RECORDS",
        record_type="LWPOLYLINE",
        raw_lines=[],
        start_pair_index=0,
        end_pair_index=0,
        layer="EM_INTERNAL_WALL_140",
    )
    adiabatic_record = Record(
        section="FILTERED_RECORDS",
        record_type="LWPOLYLINE",
        raw_lines=[],
        start_pair_index=0,
        end_pair_index=0,
        layer="EM_ADIABATIC_WALL_250",
    )

    external_primary = dict(classify_record_layer(external_record, layer_profile).get("primary", {}) or {})
    internal_primary = dict(classify_record_layer(internal_record, layer_profile).get("primary", {}) or {})
    adiabatic_primary = dict(classify_record_layer(adiabatic_record, layer_profile).get("primary", {}) or {})

    ensure_equal(external_primary["role"], "external_wall", "External wall role mismatch")
    ensure_equal(external_primary["match_source"], "canonical", "External wall match source mismatch")
    ensure_equal(external_primary["canonical_layer"], "EM_EXTERNAL_WALL_180", "External wall canonical layer mismatch")
    ensure_equal(internal_primary["role"], "internal_wall", "Internal wall role mismatch")
    ensure_equal(internal_primary["match_source"], "canonical", "Internal wall match source mismatch")
    ensure_equal(internal_primary["canonical_layer"], "EM_INTERNAL_WALL_140", "Internal wall canonical layer mismatch")
    ensure_equal(adiabatic_primary["role"], "adiabatic_wall", "Adiabatic wall role mismatch")
    ensure_equal(adiabatic_primary["match_source"], "canonical", "Adiabatic wall match source mismatch")
    ensure_equal(adiabatic_primary["canonical_layer"], "EM_ADIABATIC_WALL_250", "Adiabatic wall canonical layer mismatch")


def check_thin_internal_em_wall_segments_are_normalized_to_partition() -> None:
    layer_profile = load_layer_profile(DEFAULT_DXF_LAYER_PROFILE)
    mapping_payload = {
        "opening_placement_segments": [
            {
                "record_handle": "38FF",
                "record_type": "LWPOLYLINE",
                "layer": "EM_INTERNAL_WALL_140",
                "axis": "vertical",
                "fixed_coord_mm": 4090.163,
                "interval_min_mm": 4550.038,
                "interval_max_mm": 6110.038,
            }
        ]
    }

    segments_by_axis, summary = build_parser_layer_segments_by_axis(
        mapping_payload=mapping_payload,
        layer_profile=layer_profile,
    )

    ensure_equal(len(segments_by_axis["vertical"]), 1, "Expected a single vertical parser segment")
    segment = segments_by_axis["vertical"][0]
    ensure_equal(segment["layer_role"], "partition", "Parser segment role mismatch")
    ensure_equal(segment["layer_canonical"], "EM_INTERNAL_WALL_140", "Parser segment canonical layer mismatch")
    ensure_equal(segment["nominal_thickness_mm"], 140, "Parser segment thickness mismatch")
    ensure_equal(summary["role_counts"], {"partition": 1}, "Parser segment role summary mismatch")
    ensure_equal(summary["canonical_layer_counts"], {"EM_INTERNAL_WALL_140": 1}, "Parser segment canonical summary mismatch")


def check_adiabatic_wall_layer_overrides_outdoor_boundary() -> None:
    layer_profile = load_layer_profile(DEFAULT_DXF_LAYER_PROFILE)
    surface_rows = [
        {
            "surface_name": "ZONE_A_WALL_01",
            "surface_type": "Wall",
            "construction_name": "",
            "zone_name": "ZONE_A",
            "outside_boundary_condition": "Outdoors",
            "outside_boundary_condition_object": "",
            "sun_exposure": "SunExposed",
            "wind_exposure": "WindExposed",
            "view_factor_to_ground": "",
            "number_of_vertices": "4",
            "v1_x": "0.000",
            "v1_y": "0.000",
            "v1_z": "0.000",
            "v2_x": "0.000",
            "v2_y": "0.000",
            "v2_z": "3.000",
            "v3_x": "5.000",
            "v3_y": "0.000",
            "v3_z": "3.000",
            "v4_x": "5.000",
            "v4_y": "0.000",
            "v4_z": "0.000",
        }
    ]
    _wall_hosts_by_zone, wall_hosts_by_surface_name = build_wall_host_collections(surface_rows)
    mapping_payload = {
        "opening_placement_segments": [
            {
                "record_handle": "AD01",
                "record_type": "LWPOLYLINE",
                "layer": "EM_ADIABATIC_WALL_250",
                "axis": "horizontal",
                "fixed_coord_mm": 0.0,
                "interval_min_mm": 0.0,
                "interval_max_mm": 5000.0,
            }
        ]
    }

    summary = apply_layer_based_adiabatic_boundaries(
        surface_rows,
        wall_hosts_by_surface_name,
        mapping_payload=mapping_payload,
        layer_profile=layer_profile,
    )

    ensure_equal(summary["converted_surface_count"], 1, "Adiabatic layer conversion count mismatch")
    ensure_equal(surface_rows[0]["outside_boundary_condition"], "Adiabatic", "Surface boundary override mismatch")
    ensure_equal(surface_rows[0]["outside_boundary_condition_object"], "", "Adiabatic boundary object mismatch")
    ensure_equal(surface_rows[0]["sun_exposure"], "NoSun", "Adiabatic sun exposure mismatch")
    ensure_equal(surface_rows[0]["wind_exposure"], "NoWind", "Adiabatic wind exposure mismatch")
    ensure_equal(surface_rows[0]["view_factor_to_ground"], "0", "Adiabatic view factor mismatch")
    ensure_equal(
        wall_hosts_by_surface_name["ZONE_A_WALL_01"]["boundary_condition"],
        "Adiabatic",
        "Host wall boundary override mismatch",
    )


def check_adiabatic_wall_exports_explicit_adiabatic_boundary() -> None:
    row = {
        "surface_name": "ZONE_A_WALL_01",
        "surface_type": "Wall",
        "construction_name": "IBST_W180_PL15_BR150_PL15_AdiabaticHalf",
        "zone_name": "ZONE_A",
        "outside_boundary_condition": "Adiabatic",
    }
    vertices = [
        ("0.000", "0.000", "0.000"),
        ("5.000", "0.000", "0.000"),
        ("5.000", "0.000", "3.000"),
        ("0.000", "0.000", "3.000"),
    ]

    lines = build_building_surface_detailed_lines(row, vertices)

    ensure_equal(lines[0], "BuildingSurface:Detailed,", "Adiabatic wall object type mismatch")
    ensure("Wall:Adiabatic," not in lines, "Adiabatic walls should keep detailed geometry")
    ensure_equal(lines[1], "  ZONE_A_WALL_01,", "Adiabatic wall name mismatch")
    ensure_equal(
        lines[2],
        "  Wall,",
        "Adiabatic wall class and construction mismatch",
    )
    ensure_equal(lines[3], "  IBST_W180_PL15_BR150_PL15,", "Adiabatic wall construction mismatch")
    ensure_equal(lines[4], "  ZONE_A,", "Adiabatic wall zone mismatch")
    ensure_equal(
        lines[5],
        "  Adiabatic,",
        "Adiabatic wall boundary mismatch",
    )
    ensure_equal(lines[6], "  ,", "Adiabatic wall boundary object should be blank")
    ensure_equal(lines[7], "  NoSun,", "Adiabatic wall sun exposure mismatch")
    ensure_equal(lines[8], "  NoWind,", "Adiabatic wall wind exposure mismatch")
    ensure_equal(lines[9], "  0,", "Adiabatic wall view factor mismatch")


def check_adiabatic_designbuilder_import_keeps_full_construction() -> None:
    surface_rows = [
        {
            "surface_name": "ZONE_A_WALL_01",
            "surface_type": "Wall",
            "construction_name": "IBST_W180_PL15_BR150_PL15",
            "outside_boundary_condition": "Adiabatic",
        }
    ]

    summary = preserve_designbuilder_adiabatic_full_constructions(surface_rows)

    ensure_equal(summary["checked_surface_count"], 1, "Adiabatic construction checked count mismatch")
    ensure_equal(summary["normalized_surface_count"], 0, "Full construction should not be normalized")
    ensure_equal(surface_rows[0]["construction_name"], "IBST_W180_PL15_BR150_PL15", "Adiabatic full construction mismatch")


def check_adiabatic_designbuilder_import_strips_half_construction_suffix() -> None:
    surface_rows = [
        {
            "surface_name": "ZONE_A_WALL_01",
            "surface_type": "Wall",
            "construction_name": "IBST_W180_PL15_BR150_PL15_AdiabaticHalf",
            "outside_boundary_condition": "Adiabatic",
        }
    ]

    summary = preserve_designbuilder_adiabatic_full_constructions(surface_rows)

    ensure_equal(summary["checked_surface_count"], 1, "Adiabatic construction checked count mismatch")
    ensure_equal(summary["normalized_surface_count"], 1, "Half construction normalization count mismatch")
    ensure_equal(surface_rows[0]["construction_name"], "IBST_W180_PL15_BR150_PL15", "Adiabatic normalized construction mismatch")


def check_dynamic_wall_library_keeps_adiabatic_reverse_construction() -> None:
    construction_rows, material_rows = build_dynamic_wall_library(
        [
            {
                "surface_name": "ZONE_A_WALL_01",
                "surface_type": "Wall",
                "construction_name": "IBST_W180_PL15_BR150_PL15",
                "outside_boundary_condition": "Adiabatic",
            }
        ],
        base_construction_rows=[],
        base_material_rows=[],
    )

    construction_names = {row["construction_name"] for row in construction_rows}
    material_names = {row["material_name"] for row in material_rows}
    ensure(
        "IBST_W180_PL15_BR150_PL15" in construction_names,
        "Adiabatic base construction missing from dynamic wall library",
    )
    ensure(
        "IBST_W180_PL15_BR150_PL15_Rev" in construction_names,
        "Adiabatic reverse construction missing from dynamic wall library",
    )
    ensure(
        "IBST_BRICK_CLAY_150_.15" in material_names,
        "Adiabatic 150mm brick material missing from dynamic wall library",
    )


def check_rebuild_adds_adiabatic_reverse_construction() -> None:
    construction_rows = [
        {
            "construction_name": "WALL_A",
            "layer_1": "Outside",
            "layer_2": "Core",
            "layer_3": "Inside",
        }
    ]
    surface_rows = [
        {
            "surface_name": "ZONE_A_WALL_01",
            "surface_type": "Wall",
            "construction_name": "WALL_A",
            "outside_boundary_condition": "Adiabatic",
        }
    ]

    prepared_rows = prepare_adiabatic_surface_constructions(construction_rows, surface_rows)

    ensure_equal(len(prepared_rows), 2, "Adiabatic reverse construction row count mismatch")
    ensure_equal(prepared_rows[1]["construction_name"], "WALL_A_Rev", "Adiabatic reverse construction name mismatch")
    ensure_equal(prepared_rows[1]["layer_1"], "Inside", "Adiabatic reverse construction layer 1 mismatch")
    ensure_equal(prepared_rows[1]["layer_3"], "Outside", "Adiabatic reverse construction layer 3 mismatch")


def check_missing_zone_name_can_be_backfilled_from_em_room_boundary_overlap() -> None:
    outer_bbox_mm = (
        1350.163263762128,
        1130.037791887529,
        8655.162750861193,
        9560.037791888635,
    )
    boundary_candidates = [
        {
            "handle": "33DB",
            "source_layer": "EM_ROOM_BOUNDARY",
            "layer_role": "room_boundary",
            "candidate_scope": "room",
            "candidate_confidence": "high",
            "priority": 95,
            "closed_polyline": True,
            "bbox_xy": [
                5220.163263762128,
                8435.037791888637,
                8655.163263762128,
                9560.037791888635,
            ],
            "bbox_area_mm2": 3864375.0,
            "points_xy": [
                [5220.163263762128, 9560.037791888635],
                [8655.163263762128, 9560.037791888635],
                [8655.163263762128, 8435.037791888637],
                [5220.163263762128, 8435.037791888637],
            ],
        }
    ]
    assigned_rectangles_by_key: dict[str, list[tuple[float, float, float, float]]] = {}
    assignment_debug_by_key: dict[str, dict[str, object]] = {}
    source_zone_name_by_key = {
        "PK_PB": "PK + PB",
        "PN_01": "PN 01",
        "PN_02": "PN 02",
        "WC_01": "WC 01",
        "WC_02": "WC 02",
    }
    zone_target_area_by_key = {
        "PK_PB": 20.0,
        "PN_01": 11.0,
        "PN_02": 10.98,
        "WC_01": 3.11,
        "WC_02": 2.83,
    }
    reference_rectangles_local_by_key = {
        "LOGIA": (3.925, 7.305, 6.925, 8.180),
    }
    remembered_zone_targets_by_key = {
        "LOGIA": {
            "area_m2": 2.58,
        }
    }

    backfill_missing_layer_boundary_zones(
        boundary_candidates=boundary_candidates,
        outer_bbox_mm=outer_bbox_mm,
        assigned_rectangles_by_key=assigned_rectangles_by_key,
        assignment_debug_by_key=assignment_debug_by_key,
        source_zone_name_by_key=source_zone_name_by_key,
        zone_target_area_by_key=zone_target_area_by_key,
        reference_rectangles_local_by_key=reference_rectangles_local_by_key,
        remembered_zone_targets_by_key=remembered_zone_targets_by_key,
    )

    ensure_equal(source_zone_name_by_key["LOGIA"], "LOGIA", "Backfilled source zone name mismatch")
    ensure_close(float(zone_target_area_by_key["LOGIA"]), 2.58, abs_tol=1e-6, message="Backfilled zone target area mismatch")
    ensure("LOGIA" in assigned_rectangles_by_key, "Expected LOGIA rectangles to be assigned during backfill")
    ensure_equal(assignment_debug_by_key["LOGIA"]["boundary_handle"], "33DB", "Backfill boundary handle mismatch")
    ensure_equal(assignment_debug_by_key["LOGIA"]["assignment_mode"], "seed_overlap_backfill", "Backfill assignment mode mismatch")


def check_summarize_opening_attributes_prefers_nearest_size_text() -> None:
    multiplied_size_text = f"850{chr(215)}2200"
    size_text, sill_height_text, type_code, attr_payload = summarize_opening_attributes(
        [
            attrib_record("1001", "900 x 2200", 1000.0, 1000.0),
            attrib_record("1002", multiplied_size_text, 10.0, 10.0),
            attrib_record("1003", "DN1", 12.0, 12.0),
        ],
        reference_anchor_xy=(0.0, 0.0),
    )

    ensure_equal(size_text, "850X2200", "Selected opening size text mismatch")
    ensure_equal(sill_height_text, "", "Unexpected sill height text")
    ensure_equal(type_code, "DN1", "Selected opening type code mismatch")
    selected_size = next(payload for payload in attr_payload if payload["selected_as_size_text"])
    ensure_equal(selected_size["handle"], "1002", "Nearest size text handle mismatch")
    ensure_equal(selected_size["normalized_text"], "850X2200", "Nearest size text normalization mismatch")


def check_resolve_opening_dimensions_reparses_size_text_before_idf() -> None:
    multiplied_size_text = f"850{chr(215)}2200"
    width_mm, height_mm, method = resolve_opening_dimensions_mm(
        {
            "size_text": multiplied_size_text,
            "width_mm": 900,
            "height_mm": 2100,
        }
    )

    ensure_equal(width_mm, 850, "Reparsed opening width mismatch")
    ensure_equal(height_mm, 2200, "Reparsed opening height mismatch")
    ensure_equal(method, "size_text_reparse_override", "Opening size resolution method mismatch")


def check_hole_dimensions_ignore_lc_height() -> None:
    width_mm, height_mm, method = resolve_opening_dimensions_for_surface_mm(
        {
            "size_text": "2475X1250",
            "width_mm": 2475,
            "height_mm": 1250,
        },
        "Hole",
    )

    ensure_equal(width_mm, 2475, "Hole width from LC annotation mismatch")
    ensure_equal(height_mm, None, "Hole should not keep LC annotation height")
    ensure_equal(method, "size_text_width_only_hole_no_height", "Hole dimension method mismatch")


def check_reorder_vertical_wall_vertices_to_bottom_edge_first() -> None:
    row = {
        "surface_type": "Wall",
        "number_of_vertices": "4",
        "v1_x": "0.000",
        "v1_y": "4.980",
        "v1_z": "0.000",
        "v2_x": "0.000",
        "v2_y": "4.980",
        "v2_z": "3.000",
        "v3_x": "0.000",
        "v3_y": "3.420",
        "v3_z": "3.000",
        "v4_x": "0.000",
        "v4_y": "3.420",
        "v4_z": "0.000",
    }

    reordered = reorder_vertical_face_vertices_for_idf(row)
    ensure_equal(reordered["v1_y"], "4.980", "Wall v1_y ordering mismatch")
    ensure_equal(reordered["v1_z"], "0.000", "Wall v1_z ordering mismatch")
    ensure_equal(reordered["v2_y"], "3.420", "Wall v2_y ordering mismatch")
    ensure_equal(reordered["v2_z"], "0.000", "Wall v2_z ordering mismatch")
    ensure_equal(reordered["v3_y"], "3.420", "Wall v3_y ordering mismatch")
    ensure_equal(reordered["v3_z"], "3.000", "Wall v3_z ordering mismatch")
    ensure_equal(reordered["v4_y"], "4.980", "Wall v4_y ordering mismatch")
    ensure_equal(reordered["v4_z"], "3.000", "Wall v4_z ordering mismatch")


def check_reorder_window_vertices_to_bottom_edge_first() -> None:
    row = {
        "surface_type": "Window",
        "number_of_vertices": "4",
        "v1_x": "0.000",
        "v1_y": "2.730",
        "v1_z": "1.400",
        "v2_x": "0.000",
        "v2_y": "2.730",
        "v2_z": "2.635",
        "v3_x": "0.000",
        "v3_y": "1.330",
        "v3_z": "2.635",
        "v4_x": "0.000",
        "v4_y": "1.330",
        "v4_z": "1.400",
    }

    reordered = reorder_vertical_face_vertices_for_idf(row)
    ensure_equal(reordered["v1_y"], "2.730", "Window v1_y ordering mismatch")
    ensure_equal(reordered["v1_z"], "1.400", "Window v1_z ordering mismatch")
    ensure_equal(reordered["v2_y"], "1.330", "Window v2_y ordering mismatch")
    ensure_equal(reordered["v2_z"], "1.400", "Window v2_z ordering mismatch")
    ensure_equal(reordered["v3_y"], "1.330", "Window v3_y ordering mismatch")
    ensure_equal(reordered["v3_z"], "2.635", "Window v3_z ordering mismatch")
    ensure_equal(reordered["v4_y"], "2.730", "Window v4_y ordering mismatch")
    ensure_equal(reordered["v4_z"], "2.635", "Window v4_z ordering mismatch")


def check_wall_reference_offset_mapping_supports_explicit_outer_face_reference() -> None:
    ensure_close(
        wall_reference_offset_m("zone_boundary_inside_face_to_outer_face", 250),
        0.25,
        abs_tol=1e-9,
        message="External full-thickness reference offset mismatch",
    )
    ensure_close(
        wall_reference_offset_m("zone_boundary_inside_face_to_centerline", 180),
        0.09,
        abs_tol=1e-9,
        message="External centerline reference offset mismatch",
    )
    ensure_close(
        wall_reference_offset_m("zone_boundary_outer_face", 250),
        0.0,
        abs_tol=1e-9,
        message="Outer-face reference offset mismatch",
    )
    ensure_close(
        wall_reference_offset_m("zone_boundary_as_drawn", 250),
        0.0,
        abs_tol=1e-9,
        message="As-drawn reference offset mismatch",
    )
    ensure_close(
        wall_reference_offset_m("shared_zone_boundary_centerline", 140),
        0.0,
        abs_tol=1e-9,
        message="Shared centerline reference offset mismatch",
    )


def check_external_inside_face_reference_offsets_full_thickness() -> None:
    wall_inventory_rows = [
        make_inventory_row(
            wall_id="WALL_001",
            boundary_condition="Outdoors",
            surface_name_primary="ZONE_A_WALL_01",
            start_xy_m=(0.0, 4.0),
            end_xy_m=(5.0, 4.0),
            side_primary="north",
            thickness_mm=250,
            wall_role="external_wall",
        )
    ]
    wall_hosts_by_surface_name = {
        "ZONE_A_WALL_01": make_host_wall(
            surface_name="ZONE_A_WALL_01",
            zone_name="ZONE_A",
            start_xy_m=(0.0, 4.0),
            end_xy_m=(5.0, 4.0),
            side="north",
            boundary_condition="Outdoors",
        )
    }
    reference_policy = {
        "external_reference_type": "zone_boundary_inside_face_to_outer_face",
        "interzone_reference_type": "shared_zone_boundary_centerline",
        "single_zone_reference_type": "zone_boundary_inside_face",
    }

    resolved_walls, surface_geometry = build_wall_resolution_rows(
        wall_inventory_rows=wall_inventory_rows,
        wall_hosts_by_surface_name=wall_hosts_by_surface_name,
        reference_policy=reference_policy,
    )

    ensure_equal(resolved_walls[0]["reference_offset_mm"], 250, "Resolved wall reference offset mismatch")
    ensure_equal(resolved_walls[0]["final_export_line"]["start"], [0.0, 4.25], "Resolved wall start line mismatch")
    ensure_equal(resolved_walls[0]["final_export_line"]["end"], [5.0, 4.25], "Resolved wall end line mismatch")
    ensure_equal(surface_geometry["ZONE_A_WALL_01"]["reference_offset_mm"], 250, "Surface geometry reference offset mismatch")


def check_external_outer_face_reference_keeps_as_drawn_line() -> None:
    wall_inventory_rows = [
        make_inventory_row(
            wall_id="WALL_001",
            boundary_condition="Outdoors",
            surface_name_primary="ZONE_A_WALL_01",
            start_xy_m=(0.0, 4.0),
            end_xy_m=(5.0, 4.0),
            side_primary="north",
            thickness_mm=250,
            wall_role="external_wall",
        )
    ]
    wall_hosts_by_surface_name = {
        "ZONE_A_WALL_01": make_host_wall(
            surface_name="ZONE_A_WALL_01",
            zone_name="ZONE_A",
            start_xy_m=(0.0, 4.0),
            end_xy_m=(5.0, 4.0),
            side="north",
            boundary_condition="Outdoors",
        )
    }
    reference_policy = {
        "external_reference_type": "zone_boundary_outer_face",
        "interzone_reference_type": "shared_zone_boundary_centerline",
        "single_zone_reference_type": "zone_boundary_inside_face",
    }

    resolved_walls, surface_geometry = build_wall_resolution_rows(
        wall_inventory_rows=wall_inventory_rows,
        wall_hosts_by_surface_name=wall_hosts_by_surface_name,
        reference_policy=reference_policy,
    )

    ensure_equal(resolved_walls[0]["reference_offset_mm"], 0, "Resolved wall offset should stay zero")
    ensure_equal(
        resolved_walls[0]["final_export_line"],
        {
            "start": resolved_walls[0]["source_edge"]["start"],
            "end": resolved_walls[0]["source_edge"]["end"],
        },
        "Resolved wall export line should stay as drawn",
    )
    ensure_equal(
        surface_geometry["ZONE_A_WALL_01"]["final_export_line"],
        surface_geometry["ZONE_A_WALL_01"]["source_edge"],
        "Surface geometry export line should stay as drawn",
    )


def check_interzone_centerline_reference_stays_unchanged() -> None:
    wall_inventory_rows = [
        make_inventory_row(
            wall_id="WALL_001",
            boundary_condition="Surface",
            surface_name_primary="ZONE_A_WALL_02",
            surface_name_secondary="ZONE_B_WALL_02",
            start_xy_m=(5.0, 0.0),
            end_xy_m=(5.0, 4.0),
            side_primary="east",
            thickness_mm=140,
            wall_role="partition",
            zone_name_primary="ZONE_A",
            zone_name_secondary="ZONE_B",
        )
    ]
    wall_hosts_by_surface_name = {
        "ZONE_A_WALL_02": make_host_wall(
            surface_name="ZONE_A_WALL_02",
            zone_name="ZONE_A",
            start_xy_m=(5.0, 0.0),
            end_xy_m=(5.0, 4.0),
            side="east",
            boundary_condition="Surface",
        ),
        "ZONE_B_WALL_02": make_host_wall(
            surface_name="ZONE_B_WALL_02",
            zone_name="ZONE_B",
            start_xy_m=(5.0, 4.0),
            end_xy_m=(5.0, 0.0),
            side="west",
            boundary_condition="Surface",
        ),
    }
    reference_policy = {
        "external_reference_type": "zone_boundary_inside_face_to_outer_face",
        "interzone_reference_type": "shared_zone_boundary_centerline",
        "single_zone_reference_type": "zone_boundary_inside_face",
    }

    resolved_walls, surface_geometry = build_wall_resolution_rows(
        wall_inventory_rows=wall_inventory_rows,
        wall_hosts_by_surface_name=wall_hosts_by_surface_name,
        reference_policy=reference_policy,
    )

    ensure_equal(resolved_walls[0]["reference_type"], "shared_zone_boundary_centerline", "Interzone reference type mismatch")
    ensure_equal(resolved_walls[0]["reference_offset_mm"], 0, "Interzone reference offset mismatch")
    ensure_equal(
        resolved_walls[0]["final_export_line"],
        {
            "start": resolved_walls[0]["source_edge"]["start"],
            "end": resolved_walls[0]["source_edge"]["end"],
        },
        "Interzone export line should stay on source edge",
    )
    ensure_equal(
        surface_geometry["ZONE_A_WALL_02"]["final_export_line"],
        surface_geometry["ZONE_A_WALL_02"]["source_edge"],
        "Primary surface export line should stay on source edge",
    )
    ensure_equal(
        surface_geometry["ZONE_B_WALL_02"]["final_export_line"],
        surface_geometry["ZONE_B_WALL_02"]["source_edge"],
        "Secondary surface export line should stay on source edge",
    )


def check_surface_shell_closure_qa_detects_double_offset_against_dxf_evidence() -> None:
    surface_export_geometry_by_name = {
        "ZONE_A_WALL_01": {
            "surface_name": "ZONE_A_WALL_01",
            "boundary_condition": "Outdoors",
            "thickness_mm": 250,
            "reference_type": "zone_boundary_inside_face_to_outer_face",
            "normal_direction": {"label": "north"},
            "source_edge": {"start": [0.0, 9.56], "end": [3.8, 9.56]},
            "final_export_line": {"start": [0.0, 9.81], "end": [3.8, 9.81]},
        }
    }
    resolved_surface_payloads = [
        {
            "surface_name": "ZONE_A_WALL_01",
            "boundary_condition": "Outdoors",
            "wall_layer_canonical": "EM_EXTERNAL_WALL_250",
            "layer_record_handles": ["H1"],
            "wall_thickness_mm": 250,
        }
    ]
    geometry_payload = {
        "boundary_candidates": [
            {
                "handle": "H1",
                "layer_canonical": "EM_EXTERNAL_WALL_250",
                "bbox_xy": [1600.0, 9310.0, 5400.0, 9560.0],
            }
        ]
    }

    summary = build_surface_shell_closure_summary(
        surface_export_geometry_by_name=surface_export_geometry_by_name,
        resolved_surface_payloads=resolved_surface_payloads,
        geometry_payload=geometry_payload,
    )

    ensure_equal(summary["passed"], False, "Shell closure QA should detect the double offset")
    ensure_equal(summary["failed_surface_names"], ["ZONE_A_WALL_01"], "Failed surface list mismatch")
    detail = summary["details_by_surface_name"]["ZONE_A_WALL_01"]
    ensure_equal(detail["expected_reference_type"], "zone_boundary_outer_face", "Expected reference type mismatch")
    ensure_equal(detail["policy_reference_type"], "zone_boundary_inside_face_to_outer_face", "Policy reference type mismatch")
    ensure_equal(detail["reference_type_matches_evidence"], False, "Reference type evidence flag mismatch")
    ensure_close(float(detail["final_to_expected_mm"]), 250.0, abs_tol=1e-6, message="Final-to-expected shell distance mismatch")


def check_noxh_apartment_a_clean_uses_outer_face_reference_without_extra_offset() -> None:
    geometry_payload = load_json(
        NOXH_OUTPUT_ROOT / "intermediate" / "geometry" / "geometry_payload.json"
    )
    wall_resolution = load_json(
        NOXH_OUTPUT_ROOT / "intermediate" / "walls" / "wall_resolution.json"
    )
    focus_surface_names = [
        "APARTMENT_A_LOGIA_WALL_04",
        "APARTMENT_A_PN_01_WALL_05",
        "APARTMENT_A_PN_02_WALL_05",
        "APARTMENT_A_WC_01_WALL_04",
    ]

    ensure_equal(
        wall_resolution["reference_policy"]["external_reference_type"],
        "zone_boundary_outer_face",
        "noxh_apartment_a_clean external reference policy mismatch",
    )
    summary = build_surface_shell_closure_summary(
        surface_export_geometry_by_name=wall_resolution["surface_export_geometry_by_name"],
        resolved_surface_payloads=wall_resolution["resolved_surfaces"],
        geometry_payload=geometry_payload,
    )

    ensure_equal(summary["passed"], True, "noxh_apartment_a_clean shell closure QA should pass")
    for surface_name in focus_surface_names:
        surface_geometry = wall_resolution["surface_export_geometry_by_name"][surface_name]
        detail = summary["details_by_surface_name"][surface_name]
        ensure_equal(surface_geometry["reference_offset_mm"], 0, f"{surface_name} reference offset mismatch")
        ensure_equal(surface_geometry["final_export_line"], surface_geometry["source_edge"], f"{surface_name} final export line mismatch")
        ensure_equal(detail["expected_reference_type"], "zone_boundary_outer_face", f"{surface_name} expected reference mismatch")
        ensure(float(detail["source_to_expected_mm"]) <= 1.0, f"{surface_name} source edge drift exceeds tolerance")
        ensure(float(detail["final_to_expected_mm"]) <= 1.0, f"{surface_name} final export drift exceeds tolerance")


def check_noxh_apartment_a_clean_bundle_uses_world_coordinates_for_designbuilder_import() -> None:
    bundle_dir = NOXH_OUTPUT_ROOT / "csv" / "NOXH_Apartment_A_clean_idf_input_bundle"
    require_path(bundle_dir / "GlobalGeometryRules.csv")
    require_path(bundle_dir / "Zone.csv")

    with (bundle_dir / "GlobalGeometryRules.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ensure(rows, "GlobalGeometryRules.csv should contain at least one row")
    ensure_equal(rows[0]["coordinate_system"], "World", "Bundle coordinate system mismatch")

    with (bundle_dir / "Zone.csv").open(encoding="utf-8-sig", newline="") as handle:
        zone_rows = list(csv.DictReader(handle))
    for row in zone_rows:
        ensure_equal(row["x_origin_m"], "0", f"Zone {row['zone_name']} x_origin_m mismatch")
        ensure_equal(row["y_origin_m"], "0", f"Zone {row['zone_name']} y_origin_m mismatch")
        ensure_equal(row["z_origin_m"], "0", f"Zone {row['zone_name']} z_origin_m mismatch")


CHECKS: list[QACheck] = [
    QACheck(
        name="opening_filter_scope",
        description="Keep owner-linked opening annotation outside shell bbox",
        kind="unit",
        runner=check_opening_annotations_outside_shell_bbox_kept,
    ),
    QACheck(
        name="opening_layer_geometry_scope",
        description="Keep explicit opening geometry on EM_* layers",
        kind="unit",
        runner=check_explicit_opening_geometry_layers_are_kept,
    ),
    QACheck(
        name="hole_as_window_layer_type",
        description="Treat EM_HOLE_AS_WINDOW as a hole imported through a window proxy",
        kind="unit",
        runner=check_hole_as_window_layer_is_modeled_as_hole,
    ),
    QACheck(
        name="hole_lc_height_ignored",
        description="Use LC annotation width for holes without treating railing height as opening height",
        kind="unit",
        runner=check_hole_dimensions_ignore_lc_height,
    ),
    QACheck(
        name="opening_layer_text_matching",
        description="Prefer explicit opening layers with nearby text",
        kind="unit",
        runner=check_opening_candidates_prefer_explicit_layers_with_nearby_text,
    ),
    QACheck(
        name="opening_geometry_host_mapping",
        description="Map host wall from opening geometry bbox",
        kind="unit",
        runner=check_opening_geometry_bbox_selects_correct_interzone_host_wall,
    ),
    QACheck(
        name="opening_geometry_anchor_projection",
        description="Project opening placement anchor to layer geometry",
        kind="unit",
        runner=check_placement_anchor_prefers_projected_opening_layer_geometry,
    ),
    QACheck(
        name="window_frame_glazing_dimensions",
        description="Reduce glazing vertices by frame width without hardcoding",
        kind="unit",
        runner=check_window_glazing_vertices_are_reduced_by_frame_width,
    ),
    QACheck(
        name="bundle_zone_export",
        description="Export LOGIA zone even when it exists only in surface rows",
        kind="unit",
        runner=check_zone_rows_include_surface_only_logia_zone,
    ),
    QACheck(
        name="wall_layer_priority",
        description="Preserve EM wall canonical layers and normalize thin partitions",
        kind="unit",
        runner=check_em_wall_layers_keep_their_own_canonical_layer,
    ),
    QACheck(
        name="wall_layer_partition_normalization",
        description="Normalize EM_INTERNAL_WALL_140 segments to partition",
        kind="unit",
        runner=check_thin_internal_em_wall_segments_are_normalized_to_partition,
    ),
    QACheck(
        name="adiabatic_wall_layer_boundary",
        description="Convert outdoor walls on adiabatic wall layers to Adiabatic",
        kind="unit",
        runner=check_adiabatic_wall_layer_overrides_outdoor_boundary,
    ),
    QACheck(
        name="adiabatic_wall_designbuilder_import_object",
        description="Export adiabatic walls with explicit Adiabatic boundary and full construction",
        kind="unit",
        runner=check_adiabatic_wall_exports_explicit_adiabatic_boundary,
    ),
    QACheck(
        name="adiabatic_full_construction",
        description="Keep full internal partition construction for DesignBuilder adiabatic walls",
        kind="unit",
        runner=check_adiabatic_designbuilder_import_keeps_full_construction,
    ),
    QACheck(
        name="adiabatic_half_suffix_normalization",
        description="Strip stale half-construction suffixes before DesignBuilder import",
        kind="unit",
        runner=check_adiabatic_designbuilder_import_strips_half_construction_suffix,
    ),
    QACheck(
        name="adiabatic_dynamic_wall_reverse_construction",
        description="Emit reverse construction for adiabatic wall constructions from the catalog",
        kind="unit",
        runner=check_dynamic_wall_library_keeps_adiabatic_reverse_construction,
    ),
    QACheck(
        name="adiabatic_rebuild_reverse_construction",
        description="Add reverse construction rows when rebuilding adiabatic wall IDF surfaces",
        kind="unit",
        runner=check_rebuild_adds_adiabatic_reverse_construction,
    ),
    QACheck(
        name="geometry_layer_boundary_backfill",
        description="Backfill unnamed zones from EM_ROOM_BOUNDARY overlap",
        kind="unit",
        runner=check_missing_zone_name_can_be_backfilled_from_em_room_boundary_overlap,
    ),
    QACheck(
        name="opening_size_text_selection",
        description="Choose nearest opening size text",
        kind="unit",
        runner=check_summarize_opening_attributes_prefers_nearest_size_text,
    ),
    QACheck(
        name="opening_size_text_reparse",
        description="Reparse opening size text before IDF build",
        kind="unit",
        runner=check_resolve_opening_dimensions_reparses_size_text_before_idf,
    ),
    QACheck(
        name="idf_wall_vertex_order",
        description="Reorder wall vertices to bottom-edge-first",
        kind="unit",
        runner=check_reorder_vertical_wall_vertices_to_bottom_edge_first,
    ),
    QACheck(
        name="idf_window_vertex_order",
        description="Reorder window vertices to bottom-edge-first",
        kind="unit",
        runner=check_reorder_window_vertices_to_bottom_edge_first,
    ),
    QACheck(
        name="wall_reference_offset_mapping",
        description="Map wall reference policy to offsets correctly",
        kind="unit",
        runner=check_wall_reference_offset_mapping_supports_explicit_outer_face_reference,
    ),
    QACheck(
        name="external_wall_full_thickness_offset",
        description="Offset external walls by full thickness when requested",
        kind="unit",
        runner=check_external_inside_face_reference_offsets_full_thickness,
    ),
    QACheck(
        name="external_wall_outer_face_reference",
        description="Keep as-drawn line for outer-face reference",
        kind="unit",
        runner=check_external_outer_face_reference_keeps_as_drawn_line,
    ),
    QACheck(
        name="interzone_centerline_reference",
        description="Keep interzone walls on shared centerline",
        kind="unit",
        runner=check_interzone_centerline_reference_stays_unchanged,
    ),
    QACheck(
        name="surface_shell_closure_detection",
        description="Detect shell double-offset against DXF evidence",
        kind="unit",
        runner=check_surface_shell_closure_qa_detects_double_offset_against_dxf_evidence,
    ),
    QACheck(
        name="noxh_apartment_a_clean_reference_policy",
        description="Verify noxh_apartment_a_clean exports outer-face reference without drift",
        kind="integration",
        runner=check_noxh_apartment_a_clean_uses_outer_face_reference_without_extra_offset,
    ),
    QACheck(
        name="noxh_apartment_a_clean_bundle_coordinates",
        description="Verify noxh_apartment_a_clean bundle uses world coordinates",
        kind="integration",
        runner=check_noxh_apartment_a_clean_bundle_uses_world_coordinates_for_designbuilder_import,
    ),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run workspace regression QA without pytest.")
    parser.add_argument(
        "--check",
        action="append",
        dest="checks",
        default=[],
        help="Run only the named QA check. May be passed multiple times.",
    )
    parser.add_argument(
        "--skip-integration",
        action="store_true",
        help="Skip integration checks that require generated output artifacts.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available QA checks and exit.",
    )
    return parser.parse_args(argv)


def iter_selected_checks(selected_names: set[str], skip_integration: bool) -> list[QACheck]:
    checks: list[QACheck] = []
    for check in CHECKS:
        if selected_names and check.name not in selected_names:
            continue
        if skip_integration and check.kind == "integration":
            continue
        checks.append(check)
    return checks


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        for check in CHECKS:
            print(f"{check.name} [{check.kind}] - {check.description}")
        return 0

    selected_names = set(args.checks or [])
    unknown = sorted(selected_names - {check.name for check in CHECKS})
    if unknown:
        for name in unknown:
            print(f"UNKNOWN {name}")
        return 2

    selected_checks = iter_selected_checks(selected_names, args.skip_integration)
    if not selected_checks:
        print("No QA checks selected.")
        return 0

    failures: list[tuple[QACheck, str]] = []
    for check in selected_checks:
        try:
            check.runner()
        except Exception as exc:
            failures.append((check, str(exc)))
            print(f"FAIL {check.name}: {exc}")
        else:
            print(f"PASS {check.name}")

    print(
        "QA_SUMMARY "
        f"passed={len(selected_checks) - len(failures)} "
        f"failed={len(failures)} "
        f"selected={len(selected_checks)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
