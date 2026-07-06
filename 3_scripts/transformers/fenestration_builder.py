#!/usr/bin/env python3
"""
Build fenestration rows and opening-host mapping from mapping, geometry, surface,
and wall artifacts.

This transformer works only from intermediate artifacts. It does not parse raw
DXF, does not infer geometry, does not build
surfaces, does not resolve final wall thickness, and does not build IDF bundles.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transformers.wall_logic import (  # noqa: E402
    build_wall_host_collections,
    canonical_zone_key,
    interpolate_segment_point,
    load_input_wall_construction_library,
    opening_anchor_xy_m,
    point_to_segment_metrics,
    resolve_input_opening_construction_rule,
)
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils.envelope_library import envelope_construction_for_opening  # noqa: E402
from utils import path_resolver  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_MAPPING_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "mapping_payload.json"
DEFAULT_OPENING_CANDIDATES = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "opening_candidates.json"
DEFAULT_GEOMETRY_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "geometry" / "geometry_payload.json"
DEFAULT_SURFACE_ROWS = Path("5_output") / "<project_id>" / "intermediate" / "surfaces" / "surface_rows.json"
DEFAULT_WALL_INVENTORY = Path("5_output") / "<project_id>" / "intermediate" / "walls" / "wall_inventory.json"
DEFAULT_WALL_RESOLUTION = Path("5_output") / "<project_id>" / "intermediate" / "walls" / "wall_resolution.json"
DEFAULT_OUTPUT_DIR = Path("5_output") / "<project_id>" / "intermediate" / "fenestration"

REVIEW_TYPES = {"", "NeedsReview", "WindowOrDoorNeedsReview"}
UNSUPPORTED_IDF_OPENING_TYPES = {"Hole"}
ADIABATIC_OPENING_EXPORT_EXCEPTIONS = (
    {
        "project_marker": "/apartment_a_new/",
        "opening_id": "OPENING_010",
        "building_surface_name": "APARTMENT_A_NEW_PK_PB_WALL_09",
        "surface_type": "Door",
        "reason": "apartment_a_new_corridor_door_on_adiabatic_wall_allowed",
    },
)
OPENING_SIZE_PATTERN = re.compile(r"\b(\d{2,4})\s*[Xx×*]\s*(\d{2,4})\b")


def workspace_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


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


def _resolve_default_project_inputs(project_id: str) -> dict[str, Path]:
    required = {
        "mapping_payload": path_resolver.resolve_output_file_for_read(project_id, "intermediate/mapping", "mapping_payload.json"),
        "opening_candidates": path_resolver.resolve_output_file_for_read(project_id, "intermediate/mapping", "opening_candidates.json"),
        "geometry_payload": path_resolver.resolve_output_file_for_read(project_id, "intermediate/geometry", "geometry_payload.json"),
        "surface_rows": path_resolver.resolve_output_file_for_read(project_id, "intermediate/surfaces", "surface_rows.json"),
        "wall_inventory": path_resolver.resolve_output_file_for_read(project_id, "intermediate/walls", "wall_inventory.json"),
        "wall_resolution": path_resolver.resolve_output_file_for_read(project_id, "intermediate/walls", "wall_resolution.json"),
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise WorkspaceRuleError(
            f"Missing required fenestration inputs for project '{project_id}': {', '.join(sorted(missing))}"
        )
    return {name: value for name, value in required.items() if value is not None}


def _resolve_default_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/fenestration")


def counter_to_sorted_dict(counter: Counter[Any]) -> dict[str, int]:
    return dict(sorted((str(key), int(value)) for key, value in counter.items()))


def ascii_token(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("+", "_")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    ascii_text = re.sub(r"_+", "_", ascii_text).strip("_")
    return ascii_text.upper() or "UNNAMED"


def allowed_adiabatic_opening_export_reason(
    *,
    geometry_upstream_source: str,
    opening_id: str,
    building_surface_name: str,
    surface_type: str,
) -> str:
    normalized_source = f"/{geometry_upstream_source}".replace("\\", "/")
    for rule in ADIABATIC_OPENING_EXPORT_EXCEPTIONS:
        if rule["project_marker"] not in normalized_source:
            continue
        if rule["opening_id"] != opening_id:
            continue
        if rule["building_surface_name"] != building_surface_name:
            continue
        if rule["surface_type"] != surface_type:
            continue
        return str(rule["reason"])
    return ""


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


def normalize_opening_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = normalized.replace("×", "X").replace("*", "X")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def extract_opening_size_mm(text: object) -> tuple[int, int] | None:
    normalized = normalize_opening_text(str(text or ""))
    match = OPENING_SIZE_PATTERN.search(normalized)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def resolve_opening_dimensions_mm(opening: dict[str, object]) -> tuple[int | None, int | None, str]:
    width_mm = int(float(opening.get("width_mm", 0) or 0)) if opening.get("width_mm") is not None else 0
    height_mm = int(float(opening.get("height_mm", 0) or 0)) if opening.get("height_mm") is not None else 0
    parsed_size = extract_opening_size_mm(opening.get("size_text"))
    if parsed_size is not None:
        parsed_width_mm, parsed_height_mm = parsed_size
        if width_mm != parsed_width_mm or height_mm != parsed_height_mm:
            return parsed_width_mm, parsed_height_mm, "size_text_reparse_override"
        return parsed_width_mm, parsed_height_mm, "size_text"
    if width_mm > 0 and height_mm > 0:
        return width_mm, height_mm, "opening_payload"
    return None, None, "unresolved"


def resolve_opening_dimensions_for_surface_mm(
    opening: dict[str, object],
    surface_type: str,
) -> tuple[int | None, int | None, str]:
    width_mm, height_mm, method = resolve_opening_dimensions_mm(opening)
    if str(surface_type or "").strip() == "Hole" and height_mm is not None:
        return width_mm, None, f"{method}_width_only_hole_no_height"
    return width_mm, height_mm, method


def opening_geometry_length_mm(opening: dict[str, object]) -> int | None:
    bbox = opening.get("matched_opening_geometry_bbox_mm")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None
    try:
        width_mm = abs(float(bbox[2]) - float(bbox[0]))
        height_mm = abs(float(bbox[3]) - float(bbox[1]))
    except (TypeError, ValueError):
        return None
    length_mm = max(width_mm, height_mm)
    if length_mm <= 1e-6:
        return None
    return int(round(length_mm))


@lru_cache(maxsize=1)
def _frame_rows_by_name() -> dict[str, dict[str, str]]:
    wall_library = load_input_wall_construction_library()
    return dict(wall_library.get("frame_rows_by_name", {}))


def resolve_frame_width_m(frame_and_divider_name: str) -> float:
    frame_name = str(frame_and_divider_name or "").strip()
    if not frame_name:
        return 0.0
    frame_row = dict(_frame_rows_by_name().get(frame_name, {}))
    if not frame_row:
        return 0.0
    frame_width_m = parse_optional_float_text(frame_row.get("frame_width_m"))
    if frame_width_m is None or frame_width_m <= 0.0:
        return 0.0
    return float(frame_width_m)


def normalize_rect(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = rect
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def point_in_rectangle(point_xy_m: tuple[float, float], rectangle: tuple[float, float, float, float]) -> bool:
    x1, y1, x2, y2 = normalize_rect(rectangle)
    return x1 - 1e-9 <= point_xy_m[0] <= x2 + 1e-9 and y1 - 1e-9 <= point_xy_m[1] <= y2 + 1e-9


def point_on_segment(
    point_xy_m: tuple[float, float],
    start_xy_m: tuple[float, float],
    end_xy_m: tuple[float, float],
    *,
    tolerance_m: float = 0.02,
) -> bool:
    px, py = point_xy_m
    x1, y1 = start_xy_m
    x2, y2 = end_xy_m
    dx = x2 - x1
    dy = y2 - y1
    length_m = math.hypot(dx, dy)
    if length_m <= 1e-9:
        return math.hypot(px - x1, py - y1) <= tolerance_m
    distance_m = abs((px - x1) * dy - (py - y1) * dx) / length_m
    if distance_m > tolerance_m:
        return False
    return (
        min(x1, x2) - tolerance_m <= px <= max(x1, x2) + tolerance_m
        and min(y1, y2) - tolerance_m <= py <= max(y1, y2) + tolerance_m
    )


def point_in_polygon(
    point_xy_m: tuple[float, float],
    polygon: list[tuple[float, float]],
    *,
    tolerance_m: float = 0.02,
) -> bool:
    if len(polygon) < 3:
        return False

    for index, start_xy_m in enumerate(polygon):
        end_xy_m = polygon[(index + 1) % len(polygon)]
        if point_on_segment(point_xy_m, start_xy_m, end_xy_m, tolerance_m=tolerance_m):
            return True

    px, py = point_xy_m
    inside = False
    previous_x, previous_y = polygon[-1]
    for current_x, current_y in polygon:
        crosses_ray = (current_y > py) != (previous_y > py)
        if crosses_ray:
            intersection_x = current_x + (py - current_y) * (previous_x - current_x) / (previous_y - current_y)
            if px <= intersection_x + tolerance_m:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def interval_overlap_length(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(max(a_start, a_end), max(b_start, b_end)) - max(min(a_start, a_end), min(b_start, b_end)))


def interval_distance_to_value(interval_min: float, interval_max: float, value: float) -> float:
    normalized_min = min(interval_min, interval_max)
    normalized_max = max(interval_min, interval_max)
    if normalized_min <= value <= normalized_max:
        return 0.0
    return min(abs(value - normalized_min), abs(value - normalized_max))


def opening_bbox_m(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    try:
        min_x = float(value[0]) / 1000.0
        min_y = float(value[1]) / 1000.0
        max_x = float(value[2]) / 1000.0
        max_y = float(value[3]) / 1000.0
    except (TypeError, ValueError):
        return None
    return normalize_rect((min_x, min_y, max_x, max_y))


def opening_geometry_hint(opening: dict[str, object]) -> dict[str, float | str] | None:
    bbox_m = opening_bbox_m(opening.get("matched_opening_geometry_bbox_mm"))
    if bbox_m is None:
        return None

    min_x, min_y, max_x, max_y = bbox_m
    width_m = max_x - min_x
    height_m = max_y - min_y
    axis = "horizontal" if width_m >= height_m else "vertical"
    along_min_m = min_x if axis == "horizontal" else min_y
    along_max_m = max_x if axis == "horizontal" else max_y
    fixed_min_m = min_y if axis == "horizontal" else min_x
    fixed_max_m = max_y if axis == "horizontal" else max_x
    return {
        "axis": axis,
        "min_x_m": min_x,
        "min_y_m": min_y,
        "max_x_m": max_x,
        "max_y_m": max_y,
        "center_x_m": (min_x + max_x) / 2.0,
        "center_y_m": (min_y + max_y) / 2.0,
        "along_min_m": along_min_m,
        "along_max_m": along_max_m,
        "along_length_m": max(0.0, along_max_m - along_min_m),
        "fixed_min_m": fixed_min_m,
        "fixed_max_m": fixed_max_m,
        "fixed_center_m": (fixed_min_m + fixed_max_m) / 2.0,
        "fixed_length_m": max(0.0, fixed_max_m - fixed_min_m),
    }


def opening_reference_anchor_xy_m(opening: dict[str, object]) -> tuple[float, float] | None:
    for key in (
        "annotation_anchor_xy",
        "matched_symbol_anchor_xy",
        "annotation_group_anchor_xy",
        "cluster_centroid_xy",
        "anchor_xy",
    ):
        anchor_xy_m = opening_anchor_xy_m(opening.get(key))
        if anchor_xy_m is not None:
            return anchor_xy_m
    return None


def normalize_zone_rectangles_payload(raw_payload: object) -> dict[str, list[tuple[float, float, float, float]]]:
    normalized: dict[str, list[tuple[float, float, float, float]]] = {}
    if not isinstance(raw_payload, dict):
        return normalized
    for zone_key, rectangles in raw_payload.items():
        if not isinstance(zone_key, str) or not isinstance(rectangles, list):
            continue
        normalized_rectangles: list[tuple[float, float, float, float]] = []
        for rectangle in rectangles:
            if not isinstance(rectangle, list) or len(rectangle) < 4:
                continue
            normalized_rectangles.append(
                normalize_rect(tuple(float(value) for value in rectangle[:4]))
            )
        if normalized_rectangles:
            normalized[str(zone_key)] = normalized_rectangles
    return normalized


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


def build_zone_name_map(
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object] | None = None,
    apartment_prefix: str = "APARTMENT_A",
) -> dict[str, str]:
    zone_name_map: dict[str, str] = {}
    zone_output_name_by_key = {
        canonical_zone_key(str(zone_key)): str(zone_name).strip()
        for zone_key, zone_name in dict((geometry_payload or {}).get("zone_output_name_by_key", {})).items()
        if canonical_zone_key(str(zone_key)) and str(zone_name).strip()
    }
    zone_aliases_by_key = geometry_zone_aliases_by_key(geometry_payload)

    def csv_name_for_zone_key(zone_key: str) -> str:
        normalized_key = canonical_zone_key(zone_key)
        return zone_output_name_by_key.get(normalized_key, f"{apartment_prefix}_{normalized_key}")

    for zone in mapping_payload.get("candidate_zones", []):
        if not isinstance(zone, dict):
            continue
        source_zone_name = str(zone.get("zone_name", "")).strip()
        if not source_zone_name:
            continue
        zone_token = resolved_geometry_zone_key(source_zone_name, geometry_payload)
        if not zone_token:
            continue
        csv_zone_name = csv_name_for_zone_key(zone_token)
        zone_name_map[source_zone_name] = csv_zone_name
        alias_zone_key = zone_aliases_by_key.get(canonical_zone_key(source_zone_name), "")
        if alias_zone_key:
            zone_name_map[alias_zone_key.replace("_", " ")] = csv_zone_name
            zone_name_map[alias_zone_key] = csv_zone_name
    for zone_key, source_zone_name in dict((geometry_payload or {}).get("source_zone_name_by_key", {})).items():
        normalized_zone_key = canonical_zone_key(str(zone_key))
        normalized_source_name = str(source_zone_name).strip()
        if normalized_zone_key and normalized_source_name:
            zone_name_map[normalized_source_name] = csv_name_for_zone_key(normalized_zone_key)
    return zone_name_map


def infer_apartment_prefix(
    geometry_payload: dict[str, object],
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
    return fallback


def build_zone_anchor_xy_by_csv(
    mapping_payload: dict[str, object],
    zone_name_map: dict[str, str],
) -> dict[str, tuple[float, float]]:
    zone_anchor_xy_by_csv: dict[str, tuple[float, float]] = {}
    for zone in mapping_payload.get("candidate_zones", []):
        if not isinstance(zone, dict):
            continue
        source_zone_name = str(zone.get("zone_name", "")).strip()
        csv_zone_name = zone_name_map.get(source_zone_name, "")
        anchor_xy = zone.get("anchor_xy")
        if not csv_zone_name or not isinstance(anchor_xy, list) or len(anchor_xy) < 2:
            continue
        zone_anchor_xy_by_csv[csv_zone_name] = (float(anchor_xy[0]) / 1000.0, float(anchor_xy[1]) / 1000.0)
    return zone_anchor_xy_by_csv


def resolve_csv_zone_name(
    source_zone_name: str,
    zone_name_map: dict[str, str],
    available_zone_names: set[str],
) -> str:
    direct_name = zone_name_map.get(source_zone_name, "")
    if direct_name:
        return direct_name

    source_zone_key = canonical_zone_key(source_zone_name)
    if not source_zone_key:
        return ""

    suffix_matches = [
        zone_name
        for zone_name in sorted(available_zone_names)
        if canonical_zone_key(zone_name) == source_zone_key or zone_name.endswith(f"_{source_zone_key}")
    ]
    return suffix_matches[0] if suffix_matches else ""


def build_zone_rectangles_by_csv(
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object],
    zone_name_map: dict[str, str],
    available_zone_names: set[str],
) -> dict[str, list[tuple[float, float, float, float]]]:
    source_zone_name_by_key = {
        str(zone_key): str(source_zone_name)
        for zone_key, source_zone_name in dict(geometry_payload.get("source_zone_name_by_key", {})).items()
    }
    zone_rectangles_m_by_key = normalize_zone_rectangles_payload(
        geometry_payload.get("zone_rectangles_m_by_key", {})
    )

    source_zone_name_by_canonical_key = {
        canonical_zone_key(str(zone.get("zone_name", ""))): str(zone.get("zone_name", ""))
        for zone in mapping_payload.get("candidate_zones", [])
        if isinstance(zone, dict) and str(zone.get("zone_name", "")).strip()
    }

    zone_rectangles_by_csv: dict[str, list[tuple[float, float, float, float]]] = {}
    for zone_key, rectangles in zone_rectangles_m_by_key.items():
        source_zone_name = source_zone_name_by_key.get(zone_key, "")
        if not source_zone_name:
            source_zone_name = source_zone_name_by_canonical_key.get(canonical_zone_key(zone_key), "")
        csv_zone_name = resolve_csv_zone_name(source_zone_name, zone_name_map, available_zone_names)
        if csv_zone_name and rectangles:
            zone_rectangles_by_csv[csv_zone_name] = list(rectangles)
    return zone_rectangles_by_csv


def normalize_zone_polygons_payload(raw_payload: object) -> dict[str, list[list[tuple[float, float]]]]:
    normalized: dict[str, list[list[tuple[float, float]]]] = {}
    if not isinstance(raw_payload, dict):
        return normalized

    for zone_key, polygons in raw_payload.items():
        if not isinstance(zone_key, str) or not isinstance(polygons, list):
            continue

        if polygons and all(isinstance(point, list) and len(point) >= 2 for point in polygons):
            polygon_candidates = [polygons]
        else:
            polygon_candidates = polygons

        normalized_polygons: list[list[tuple[float, float]]] = []
        for polygon in polygon_candidates:
            if not isinstance(polygon, list):
                continue
            points: list[tuple[float, float]] = []
            for point in polygon:
                if not isinstance(point, list) or len(point) < 2:
                    continue
                try:
                    points.append((float(point[0]), float(point[1])))
                except (TypeError, ValueError):
                    continue
            if len(points) >= 3:
                normalized_polygons.append(points)

        if normalized_polygons:
            normalized[str(zone_key)] = normalized_polygons
    return normalized


def build_zone_polygons_by_csv(
    mapping_payload: dict[str, object],
    geometry_payload: dict[str, object],
    zone_name_map: dict[str, str],
    available_zone_names: set[str],
) -> dict[str, list[list[tuple[float, float]]]]:
    source_zone_name_by_key = {
        str(zone_key): str(source_zone_name)
        for zone_key, source_zone_name in dict(geometry_payload.get("source_zone_name_by_key", {})).items()
    }
    zone_polygons_m_by_key = normalize_zone_polygons_payload(
        geometry_payload.get("zone_polygons_m_by_key", {})
    )
    source_zone_name_by_canonical_key = {
        canonical_zone_key(str(zone.get("zone_name", ""))): str(zone.get("zone_name", ""))
        for zone in mapping_payload.get("candidate_zones", [])
        if isinstance(zone, dict) and str(zone.get("zone_name", "")).strip()
    }

    zone_polygons_by_csv: dict[str, list[list[tuple[float, float]]]] = {}
    for zone_key, polygons in zone_polygons_m_by_key.items():
        source_zone_name = source_zone_name_by_key.get(zone_key, "")
        if not source_zone_name:
            source_zone_name = source_zone_name_by_canonical_key.get(canonical_zone_key(zone_key), "")
        csv_zone_name = resolve_csv_zone_name(source_zone_name, zone_name_map, available_zone_names)
        if csv_zone_name and polygons:
            zone_polygons_by_csv[csv_zone_name] = list(polygons)
    return zone_polygons_by_csv


def index_wall_inventory_by_surface_name(
    wall_inventory_rows: list[dict[str, object]],
) -> dict[str, tuple[dict[str, object], str]]:
    index: dict[str, tuple[dict[str, object], str]] = {}
    for row in wall_inventory_rows:
        if not isinstance(row, dict):
            continue
        primary_name = str(row.get("surface_name_primary", "")).strip()
        secondary_name = str(row.get("surface_name_secondary", "")).strip()
        if primary_name:
            index[primary_name] = (row, "primary")
        if secondary_name:
            index[secondary_name] = (row, "secondary")
    return index


def enrich_wall_hosts(
    wall_hosts_by_zone: dict[str, list[dict[str, object]]],
    wall_inventory_rows: list[dict[str, object]],
    wall_resolution: dict[str, object],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, dict[str, object]]]:
    inventory_by_surface = index_wall_inventory_by_surface_name(wall_inventory_rows)
    estimated_opening_hosts = {
        str(surface_name).strip()
        for surface_name in list(wall_resolution.get("estimated_opening_hosts", []))
        if str(surface_name).strip()
    }

    enriched_by_zone: dict[str, list[dict[str, object]]] = {}
    enriched_by_surface_name: dict[str, dict[str, object]] = {}
    for zone_name, host_walls in wall_hosts_by_zone.items():
        enriched_rows: list[dict[str, object]] = []
        for host_wall in host_walls:
            enriched = dict(host_wall)
            surface_name = str(enriched.get("surface_name", "")).strip()
            inventory_row, inventory_role = inventory_by_surface.get(surface_name, ({}, ""))
            if inventory_row:
                enriched["physical_wall_id"] = str(inventory_row.get("physical_wall_id", "")).strip()
                enriched["wall_family"] = str(inventory_row.get("wall_family", "")).strip()
                enriched["inventory_surface_role"] = inventory_role
                enriched["inventory_total_thickness_mm"] = int(inventory_row.get("total_thickness_mm", 0) or 0)
                enriched["inventory_core_thickness_mm"] = int(inventory_row.get("core_thickness_mm", 0) or 0)
            enriched["is_estimated_opening_host"] = surface_name in estimated_opening_hosts
            enriched_rows.append(enriched)
            if surface_name:
                enriched_by_surface_name[surface_name] = enriched
        enriched_by_zone[str(zone_name)] = enriched_rows
    return enriched_by_zone, enriched_by_surface_name


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
        float(host_wall["start"][0]),
        float(host_wall["start"][1]),
        float(host_wall["end"][0]),
        float(host_wall["end"][1]),
    )
    projection_xy_m = interpolate_segment_point(
        float(host_wall["start"][0]),
        float(host_wall["start"][1]),
        float(host_wall["end"][0]),
        float(host_wall["end"][1]),
        along_m,
    )
    return projection_xy_m, along_m, distance_m


def zones_containing_point(
    point_xy_m: tuple[float, float] | None,
    zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]],
    zone_polygons_by_name: dict[str, list[list[tuple[float, float]]]] | None = None,
) -> list[str]:
    if point_xy_m is None:
        return []

    containing: list[str] = []
    polygons_by_name = zone_polygons_by_name or {}
    for zone_name in sorted(set(zone_rectangles_by_name) | set(polygons_by_name)):
        polygons = polygons_by_name.get(zone_name, [])
        rectangles = zone_rectangles_by_name.get(zone_name, [])
        if polygons:
            if any(point_in_polygon(point_xy_m, polygon) for polygon in polygons):
                containing.append(zone_name)
        elif any(point_in_rectangle(point_xy_m, rectangle) for rectangle in rectangles):
            containing.append(zone_name)
    return containing


def preferred_boundary_condition_for_opening(
    *,
    surface_type: str,
    type_code: str,
    source_zone_key: str,
) -> str:
    normalized_type = type_code.upper()
    if normalized_type.startswith("LC"):
        return "Outdoors"
    if surface_type == "Hole":
        return "Surface"
    if surface_type == "Door":
        return "Surface"
    if surface_type == "GlassDoor":
        return "Outdoors"
    if source_zone_key == "LOGIA" and surface_type == "Door":
        return "Surface"
    return "Outdoors"


def preferred_wall_side_from_zone_anchor(
    zone_anchor_xy_m: tuple[float, float] | None,
    opening_anchor_xy_m_value: tuple[float, float] | None,
) -> str | None:
    if zone_anchor_xy_m is None or opening_anchor_xy_m_value is None:
        return None

    delta_x = opening_anchor_xy_m_value[0] - zone_anchor_xy_m[0]
    delta_y = opening_anchor_xy_m_value[1] - zone_anchor_xy_m[1]
    if max(abs(delta_x), abs(delta_y)) < 0.15:
        return None
    if abs(abs(delta_x) - abs(delta_y)) < 0.30:
        return None
    if abs(delta_x) > abs(delta_y):
        return "east" if delta_x >= 0.0 else "west"
    return "north" if delta_y >= 0.0 else "south"


def host_wall_fixed_coord_and_span_m(
    host_wall: dict[str, object],
) -> tuple[str, float, float, float]:
    axis = host_wall_axis(host_wall)
    start = host_wall["start"]
    end = host_wall["end"]
    if axis == "horizontal":
        return (
            axis,
            float(start[1]),
            min(float(start[0]), float(end[0])),
            max(float(start[0]), float(end[0])),
        )
    return (
        axis,
        float(start[0]),
        min(float(start[1]), float(end[1])),
        max(float(start[1]), float(end[1])),
    )


def opening_layer_anchor_for_host(
    opening: dict[str, object],
    host_wall: dict[str, object],
) -> tuple[float, float] | None:
    geometry_hint = opening_geometry_hint(opening)
    if geometry_hint is None:
        return None
    start = host_wall["start"]
    end = host_wall["end"]
    host_dx = abs(float(end[0]) - float(start[0]))
    host_dy = abs(float(end[1]) - float(start[1]))
    if min(host_dx, host_dy) > 0.05:
        projection_xy_m, _, _ = host_wall_projection(
            (float(geometry_hint["center_x_m"]), float(geometry_hint["center_y_m"])),
            host_wall,
        )
        return projection_xy_m
    axis, fixed_coord_m, host_interval_min_m, host_interval_max_m = host_wall_fixed_coord_and_span_m(host_wall)
    if axis != str(geometry_hint["axis"]):
        return None

    center_along_m = min(
        max(float(geometry_hint["center_x_m"] if axis == "horizontal" else geometry_hint["center_y_m"]), host_interval_min_m),
        host_interval_max_m,
    )
    if axis == "horizontal":
        return center_along_m, fixed_coord_m
    return fixed_coord_m, center_along_m


def choose_host_wall_from_annotation(
    *,
    opening: dict[str, object],
    source_zone_name: str,
    source_zone_csv_name: str,
    source_zone_anchor_xy_m: tuple[float, float] | None,
    zone_anchor_xy_by_name: dict[str, tuple[float, float]],
    zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]],
    zone_polygons_by_name: dict[str, list[list[tuple[float, float]]]] | None = None,
    host_walls: list[dict[str, object]],
) -> tuple[dict[str, object] | None, tuple[float, float] | None, dict[str, object]]:
    primary_anchor_xy_m = opening_anchor_xy_m(opening.get("anchor_xy")) or opening_anchor_xy_m(
        opening.get("matched_symbol_anchor_xy")
    )
    if primary_anchor_xy_m is None or not host_walls:
        return None, None, {
            "host_selection_anchor_source": "annotation",
            "host_selection_containing_zones": [],
            "host_selection_preferred_boundary_condition": "",
            "host_selection_candidate_surface_names": [],
        }

    opening_width_m = max(0.5, float(opening.get("width_mm", 1000) or 1000) / 1000.0)
    surface_type = str(opening.get("candidate_fenestration_type", "")).strip()
    type_code = str(opening.get("type_code", "")).strip().upper()
    source_zone_key = canonical_zone_key(source_zone_name)
    containing_zone_names = zones_containing_point(primary_anchor_xy_m, zone_rectangles_by_name, zone_polygons_by_name)
    preferred_boundary_condition = preferred_boundary_condition_for_opening(
        surface_type=surface_type,
        type_code=type_code,
        source_zone_key=source_zone_key,
    )
    reference_zone_name = containing_zone_names[0] if len(containing_zone_names) == 1 else source_zone_csv_name
    reference_zone_anchor_xy_m = zone_anchor_xy_by_name.get(reference_zone_name, source_zone_anchor_xy_m)
    preferred_side = preferred_wall_side_from_zone_anchor(reference_zone_anchor_xy_m, primary_anchor_xy_m)

    scored_hosts: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for host_wall in host_walls:
        distance_m, _, host_length_m = point_to_segment_metrics(
            primary_anchor_xy_m[0],
            primary_anchor_xy_m[1],
            float(host_wall["start"][0]),
            float(host_wall["start"][1]),
            float(host_wall["end"][0]),
            float(host_wall["end"][1]),
        )
        if host_length_m < opening_width_m + 0.1:
            continue

        owner_zone_rank = 0
        adjacent_zone_rank = 0
        if containing_zone_names:
            host_zone_name = str(host_wall.get("zone_name", ""))
            adjacent_zone_name = str(host_wall.get("adjacent_zone_name", ""))
            if host_zone_name in containing_zone_names:
                owner_zone_rank = 0
            elif adjacent_zone_name in containing_zone_names and host_wall.get("boundary_condition") == "Surface":
                owner_zone_rank = 1
            else:
                owner_zone_rank = 2

        boundary_rank = 0 if host_wall.get("boundary_condition") == preferred_boundary_condition else 1
        side_rank = 0
        if preferred_side and reference_zone_name and str(host_wall.get("zone_name", "")) == reference_zone_name:
            side_rank = 0 if str(host_wall.get("side", "")) == preferred_side else 1
        source_zone_owner_rank = 0
        if (
            not containing_zone_names
            and preferred_boundary_condition == "Surface"
            and host_wall.get("boundary_condition") == "Surface"
            and source_zone_csv_name
        ):
            source_zone_owner_rank = 0 if str(host_wall.get("zone_name", "")) == source_zone_csv_name else 1
        if not containing_zone_names and source_zone_csv_name:
            adjacent_zone_rank = (
                0
                if str(host_wall.get("zone_name", "")) == source_zone_csv_name
                or str(host_wall.get("adjacent_zone_name", "")) == source_zone_csv_name
                else 1
            )

        score_key = (
            owner_zone_rank,
            boundary_rank,
            source_zone_owner_rank,
            side_rank,
            round(distance_m, 6),
            adjacent_zone_rank,
            -round(host_length_m, 6),
            str(host_wall.get("surface_name", "")),
        )
        scored_hosts.append((score_key, host_wall))

    scored_hosts.sort(key=lambda item: item[0])
    candidate_surface_names = [
        str(host_wall.get("surface_name", "")).strip()
        for _score_key, host_wall in scored_hosts[:8]
        if str(host_wall.get("surface_name", "")).strip()
    ]
    manifest = {
        "host_selection_anchor_source": "annotation",
        "host_selection_containing_zones": containing_zone_names,
        "host_selection_preferred_boundary_condition": preferred_boundary_condition,
        "host_selection_candidate_surface_names": candidate_surface_names,
    }
    if not scored_hosts:
        return None, primary_anchor_xy_m, manifest

    return scored_hosts[0][1], primary_anchor_xy_m, manifest


def choose_host_wall_for_opening(
    *,
    opening: dict[str, object],
    source_zone_name: str,
    source_zone_csv_name: str,
    source_zone_anchor_xy_m: tuple[float, float] | None,
    zone_anchor_xy_by_name: dict[str, tuple[float, float]],
    zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]],
    zone_polygons_by_name: dict[str, list[list[tuple[float, float]]]] | None = None,
    host_walls: list[dict[str, object]],
) -> tuple[dict[str, object] | None, tuple[float, float] | None, dict[str, object]]:
    geometry_hint = opening_geometry_hint(opening)
    if geometry_hint is None or not host_walls:
        return choose_host_wall_from_annotation(
            opening=opening,
            source_zone_name=source_zone_name,
            source_zone_csv_name=source_zone_csv_name,
            source_zone_anchor_xy_m=source_zone_anchor_xy_m,
            zone_anchor_xy_by_name=zone_anchor_xy_by_name,
            zone_rectangles_by_name=zone_rectangles_by_name,
            zone_polygons_by_name=zone_polygons_by_name,
            host_walls=host_walls,
        )

    surface_type = str(opening.get("candidate_fenestration_type", "")).strip()
    type_code = str(opening.get("type_code", "")).strip().upper()
    source_zone_key = canonical_zone_key(source_zone_name)
    reference_anchor_xy_m = opening_reference_anchor_xy_m(opening)
    containing_zone_names = zones_containing_point(reference_anchor_xy_m, zone_rectangles_by_name, zone_polygons_by_name)
    preferred_boundary_condition = preferred_boundary_condition_for_opening(
        surface_type=surface_type,
        type_code=type_code,
        source_zone_key=source_zone_key,
    )

    scored_hosts: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for host_wall in host_walls:
        axis, fixed_coord_m, host_interval_min_m, host_interval_max_m = host_wall_fixed_coord_and_span_m(host_wall)
        if axis != str(geometry_hint["axis"]):
            continue

        host_length_m = float(host_wall.get("length_m", 0.0) or 0.0)
        overlap_m = interval_overlap_length(
            float(geometry_hint["along_min_m"]),
            float(geometry_hint["along_max_m"]),
            host_interval_min_m,
            host_interval_max_m,
        )
        if overlap_m <= 1e-9:
            continue

        plane_distance_m = interval_distance_to_value(
            float(geometry_hint["fixed_min_m"]),
            float(geometry_hint["fixed_max_m"]),
            fixed_coord_m,
        )
        max_plane_distance_m = max(0.18, float(geometry_hint["fixed_length_m"]) + 0.12)
        if plane_distance_m > max_plane_distance_m:
            continue

        opening_interval_length_m = max(0.05, float(geometry_hint["along_length_m"]))
        overlap_deficit_m = max(0.0, opening_interval_length_m - overlap_m)
        overhang_m = max(0.0, host_interval_min_m - float(geometry_hint["along_min_m"])) + max(
            0.0,
            float(geometry_hint["along_max_m"]) - host_interval_max_m,
        )
        if overlap_m < max(0.20, min(opening_interval_length_m * 0.60, opening_interval_length_m - 0.05)):
            continue

        annotation_zone_rank = 1
        if containing_zone_names:
            host_zone_name = str(host_wall.get("zone_name", ""))
            adjacent_zone_name = str(host_wall.get("adjacent_zone_name", ""))
            if host_zone_name in containing_zone_names:
                annotation_zone_rank = 0
            elif adjacent_zone_name in containing_zone_names and host_wall.get("boundary_condition") == "Surface":
                annotation_zone_rank = 1
            else:
                annotation_zone_rank = 2

        boundary_rank = 0 if host_wall.get("boundary_condition") == preferred_boundary_condition else 1
        source_zone_rank = 1
        if source_zone_csv_name:
            if str(host_wall.get("zone_name", "")) == source_zone_csv_name:
                source_zone_rank = 0
            elif (
                str(host_wall.get("adjacent_zone_name", "")) == source_zone_csv_name
                and host_wall.get("boundary_condition") == "Surface"
            ):
                source_zone_rank = 1
            else:
                source_zone_rank = 2

        score_key = (
            source_zone_rank,
            annotation_zone_rank,
            boundary_rank,
            round(plane_distance_m, 6),
            round(overlap_deficit_m, 6),
            round(overhang_m, 6),
            0 if bool(host_wall.get("is_estimated_opening_host")) else 1,
            round(abs(fixed_coord_m - float(geometry_hint["fixed_center_m"])), 6),
            -round(host_length_m, 6),
            str(host_wall.get("surface_name", "")),
        )
        scored_hosts.append((score_key, host_wall))

    scored_hosts.sort(key=lambda item: item[0])
    candidate_surface_names = [
        str(host_wall.get("surface_name", "")).strip()
        for _score_key, host_wall in scored_hosts[:8]
        if str(host_wall.get("surface_name", "")).strip()
    ]
    manifest = {
        "host_selection_anchor_source": "opening_geometry",
        "host_selection_containing_zones": containing_zone_names,
        "host_selection_preferred_boundary_condition": preferred_boundary_condition,
        "host_selection_candidate_surface_names": candidate_surface_names,
    }
    if not scored_hosts:
        return choose_host_wall_from_annotation(
            opening=opening,
            source_zone_name=source_zone_name,
            source_zone_csv_name=source_zone_csv_name,
            source_zone_anchor_xy_m=source_zone_anchor_xy_m,
            zone_anchor_xy_by_name=zone_anchor_xy_by_name,
            zone_rectangles_by_name=zone_rectangles_by_name,
            zone_polygons_by_name=zone_polygons_by_name,
            host_walls=host_walls,
        )

    selected_host_wall = scored_hosts[0][1]
    return selected_host_wall, opening_layer_anchor_for_host(opening, selected_host_wall), manifest


def transform_local_mm_to_world_m(
    *,
    local_xy_mm: tuple[float, float],
    insert_anchor_xy_m: tuple[float, float],
    rotation_degrees: float,
    scale_x: float,
    scale_y: float,
) -> tuple[float, float]:
    x_m = (local_xy_mm[0] / 1000.0) * scale_x
    y_m = (local_xy_mm[1] / 1000.0) * scale_y
    angle_rad = math.radians(rotation_degrees)
    return (
        insert_anchor_xy_m[0] + (x_m * math.cos(angle_rad)) - (y_m * math.sin(angle_rad)),
        insert_anchor_xy_m[1] + (x_m * math.sin(angle_rad)) + (y_m * math.cos(angle_rad)),
    )


def symbol_parametric_anchor_for_host(
    *,
    opening: dict[str, object],
    host_wall: dict[str, object],
    primary_anchor_xy_m: tuple[float, float],
) -> tuple[tuple[float, float] | None, float]:
    if str(opening.get("candidate_fenestration_type", "")).strip() not in {"Door", "GlassDoor"}:
        return None, float("inf")

    insert_anchor_xy_m = opening_anchor_xy_m(opening.get("matched_symbol_anchor_xy"))
    block_bbox = opening.get("matched_symbol_block_bbox_mm")
    matched_symbol_distance = opening.get("matched_symbol_distance")
    opening_width_mm = float(opening.get("width_mm", 0.0) or 0.0)
    if (
        insert_anchor_xy_m is None
        or not isinstance(block_bbox, list)
        or len(block_bbox) < 4
        or matched_symbol_distance in {None, ""}
        or float(matched_symbol_distance) > 1400.0
        or opening_width_mm <= 0.0
    ):
        return None, float("inf")

    min_x, min_y, max_x, max_y = [float(value) for value in block_bbox[:4]]
    rotation_degrees = float(opening.get("rotation_degrees", 0.0) or 0.0)
    scale_x = float(opening.get("scale_x", 1.0) or 1.0)
    scale_y = float(opening.get("scale_y", 1.0) or 1.0)
    host_dx = float(host_wall["end"][0]) - float(host_wall["start"][0])
    host_dy = float(host_wall["end"][1]) - float(host_wall["start"][1])
    host_length_m = math.hypot(host_dx, host_dy)
    if host_length_m <= 1e-9:
        return None, float("inf")

    local_origin_world = transform_local_mm_to_world_m(
        local_xy_mm=(0.0, 0.0),
        insert_anchor_xy_m=insert_anchor_xy_m,
        rotation_degrees=rotation_degrees,
        scale_x=scale_x,
        scale_y=scale_y,
    )
    local_x_world = transform_local_mm_to_world_m(
        local_xy_mm=(1000.0, 0.0),
        insert_anchor_xy_m=insert_anchor_xy_m,
        rotation_degrees=rotation_degrees,
        scale_x=scale_x,
        scale_y=scale_y,
    )
    local_y_world = transform_local_mm_to_world_m(
        local_xy_mm=(0.0, 1000.0),
        insert_anchor_xy_m=insert_anchor_xy_m,
        rotation_degrees=rotation_degrees,
        scale_x=scale_x,
        scale_y=scale_y,
    )
    x_alignment = abs(
        ((local_x_world[0] - local_origin_world[0]) * host_dx)
        + ((local_x_world[1] - local_origin_world[1]) * host_dy)
    )
    y_alignment = abs(
        ((local_y_world[0] - local_origin_world[0]) * host_dx)
        + ((local_y_world[1] - local_origin_world[1]) * host_dy)
    )
    if x_alignment >= y_alignment:
        local_candidates_mm = [
            (min_x + (opening_width_mm / 2.0), (min_y + max_y) / 2.0),
            (max_x - (opening_width_mm / 2.0), (min_y + max_y) / 2.0),
        ]
    else:
        local_candidates_mm = [
            (((min_x + max_x) / 2.0), min_y + (opening_width_mm / 2.0)),
            (((min_x + max_x) / 2.0), max_y - (opening_width_mm / 2.0)),
        ]

    _, primary_along_m, _ = host_wall_projection(primary_anchor_xy_m, host_wall)
    best_anchor_xy_m: tuple[float, float] | None = None
    best_delta_m = float("inf")
    for local_candidate_mm in local_candidates_mm:
        candidate_xy_m = transform_local_mm_to_world_m(
            local_xy_mm=local_candidate_mm,
            insert_anchor_xy_m=insert_anchor_xy_m,
            rotation_degrees=rotation_degrees,
            scale_x=scale_x,
            scale_y=scale_y,
        )
        _, candidate_along_m, candidate_distance_m = host_wall_projection(candidate_xy_m, host_wall)
        delta_m = abs(candidate_along_m - primary_along_m)
        if candidate_distance_m > 1.25:
            continue
        if delta_m < best_delta_m:
            best_anchor_xy_m = candidate_xy_m
            best_delta_m = delta_m

    return best_anchor_xy_m, best_delta_m


def raw_parallel_interval_hint(
    *,
    placement_segments: list[dict[str, object]],
    host_wall: dict[str, object],
    anchor_xy_m: tuple[float, float],
    opening_width_m: float,
    plane_tolerance_mm: float = 80.0,
) -> tuple[tuple[float, float] | None, float | None, str]:
    axis = host_wall_axis(host_wall)
    start = host_wall["start"]
    end = host_wall["end"]
    fixed_coord_m = float(start[1]) if axis == "horizontal" else float(start[0])
    host_interval_min_m = min(float(start[0]), float(end[0])) if axis == "horizontal" else min(float(start[1]), float(end[1]))
    host_interval_max_m = max(float(start[0]), float(end[0])) if axis == "horizontal" else max(float(start[1]), float(end[1]))
    _, anchor_along_m, _ = host_wall_projection(anchor_xy_m, host_wall)
    width_tolerance_m = max(0.12, opening_width_m * 0.20)
    center_tolerance_m = max(0.35, min(1.20, opening_width_m * 0.45))
    host_overlap_tolerance_m = 0.05
    layer_priority = {
        "TAC - Thay": 0,
        "TAC - Lop hoan thien": 1,
        "TAC - Tuong": 2,
    }
    candidates: list[tuple[tuple[object, ...], tuple[float, float], float, str]] = []

    for segment in placement_segments:
        segment_axis = str(segment.get("axis", "")).strip()
        segment_layer = str(segment.get("layer", "")).strip()
        if segment_axis != axis or segment_layer not in layer_priority:
            continue
        fixed_coord_mm = float(segment.get("fixed_coord_mm", 0.0) or 0.0)
        if abs((fixed_coord_mm / 1000.0) - fixed_coord_m) > (plane_tolerance_mm / 1000.0):
            continue

        interval_min_m = float(segment.get("interval_min_mm", 0.0) or 0.0) / 1000.0
        interval_max_m = float(segment.get("interval_max_mm", 0.0) or 0.0) / 1000.0
        if interval_max_m <= interval_min_m:
            continue
        center_xy_m = (
            ((interval_min_m + interval_max_m) / 2.0, fixed_coord_m)
            if axis == "horizontal"
            else (fixed_coord_m, (interval_min_m + interval_max_m) / 2.0)
        )
        interval_length_m = interval_max_m - interval_min_m
        if interval_length_m < max(0.35, opening_width_m - width_tolerance_m):
            continue
        if interval_length_m > opening_width_m + width_tolerance_m:
            continue

        overlap_min_m = max(interval_min_m, host_interval_min_m - host_overlap_tolerance_m)
        overlap_max_m = min(interval_max_m, host_interval_max_m + host_overlap_tolerance_m)
        overlap_length_m = max(0.0, overlap_max_m - overlap_min_m)
        if overlap_length_m < (interval_length_m * 0.85):
            continue

        edge_overhang_m = max(0.0, host_interval_min_m - interval_min_m) + max(
            0.0,
            interval_max_m - host_interval_max_m,
        )

        _, center_along_m, _ = host_wall_projection(center_xy_m, host_wall)
        center_delta_m = abs(center_along_m - anchor_along_m)
        if center_delta_m > center_tolerance_m:
            continue

        candidates.append(
            (
                (
                    round(abs(interval_length_m - opening_width_m), 6),
                    round(center_delta_m, 6),
                    round(edge_overhang_m, 6),
                    layer_priority[segment_layer],
                    str(segment.get("record_handle", "")),
                    int(segment.get("segment_index", 0) or 0),
                ),
                center_xy_m,
                interval_length_m,
                segment_layer,
            )
        )

    if not candidates:
        return None, None, ""

    candidates.sort(key=lambda item: item[0])
    _, center_xy_m, interval_length_m, record_layer = candidates[0]
    return center_xy_m, interval_length_m, record_layer


def choose_placement_anchor_for_host(
    *,
    opening: dict[str, object],
    host_wall: dict[str, object],
    primary_anchor_xy_m: tuple[float, float] | None,
    placement_segments: list[dict[str, object]],
    opening_width_m: float,
) -> tuple[tuple[float, float] | None, str, float | None]:
    geometry_anchor_xy_m = opening_layer_anchor_for_host(opening, host_wall)
    if geometry_anchor_xy_m is not None:
        return geometry_anchor_xy_m, "opening_layer_geometry", None

    if primary_anchor_xy_m is None:
        return None, "none", None

    surface_type = str(opening.get("candidate_fenestration_type", "")).strip()
    symbol_anchor_xy_m, symbol_anchor_delta_m = symbol_parametric_anchor_for_host(
        opening=opening,
        host_wall=host_wall,
        primary_anchor_xy_m=primary_anchor_xy_m,
    )
    symbol_anchor_max_delta_m = 0.65 if surface_type == "GlassDoor" else 0.25
    if symbol_anchor_xy_m is not None and symbol_anchor_delta_m <= symbol_anchor_max_delta_m:
        primary_anchor_xy_m = symbol_anchor_xy_m
        primary_anchor_source = "matched_symbol_parametric"
    else:
        primary_anchor_source = "annotation"

    raw_hint_anchor_xy_m, raw_hint_width_m, raw_hint_layer = raw_parallel_interval_hint(
        placement_segments=placement_segments,
        host_wall=host_wall,
        anchor_xy_m=primary_anchor_xy_m,
        opening_width_m=opening_width_m,
    )
    if raw_hint_anchor_xy_m is not None:
        width_override_m = None
        if raw_hint_width_m is not None and abs(raw_hint_width_m - opening_width_m) <= max(0.12, opening_width_m * 0.12):
            width_override_m = raw_hint_width_m
        return raw_hint_anchor_xy_m, f"raw_parallel_interval:{raw_hint_layer}", width_override_m
    return primary_anchor_xy_m, primary_anchor_source, None


def build_fenestration_row_for_host(
    *,
    fenestration_name: str,
    surface_type: str,
    construction_name: str,
    frame_and_divider_name: str,
    host_wall: dict[str, object],
    opening_width_m: float,
    opening_height_m: float,
    sill_height_m: float,
    anchor_xy_m: tuple[float, float],
    touches_ceiling: bool = False,
) -> tuple[dict[str, object], dict[str, object]] | None:
    start_x, start_y = [float(value) for value in host_wall["start"]]
    end_x, end_y = [float(value) for value in host_wall["end"]]
    host_length_m = float(host_wall["length_m"])
    host_height_m = float(host_wall["height_m"])
    if host_length_m <= 0.25 or host_height_m <= 0.5:
        return None

    frame_width_m = resolve_frame_width_m(frame_and_divider_name)
    total_width_m = min(max(0.25, opening_width_m), max(0.25, host_length_m - 0.10))
    if total_width_m >= host_length_m:
        return None
    glazing_width_m = total_width_m
    if frame_width_m > 0.0:
        glazing_width_m = max(0.25, total_width_m - (2.0 * frame_width_m))
        glazing_width_m = min(glazing_width_m, max(0.25, host_length_m - 0.10))
    if glazing_width_m >= host_length_m:
        return None

    if surface_type in {"Door", "GlassDoor"}:
        sill_height_m = 0.0

    top_limit_m = host_height_m if touches_ceiling else max(0.0, host_height_m - 0.05)
    total_height_m = max(0.25, opening_height_m)
    if touches_ceiling:
        total_height_m = max(0.25, min(total_height_m, max(0.25, host_height_m - max(0.0, sill_height_m))))
    total_bottom_z = max(0.0, min(sill_height_m, max(0.0, top_limit_m - total_height_m)))
    glazing_height_m = total_height_m
    bottom_z = total_bottom_z
    if frame_width_m > 0.0:
        glazing_height_m = max(0.25, total_height_m - (2.0 * frame_width_m))
        bottom_z = total_bottom_z + frame_width_m
    top_z = min(top_limit_m, bottom_z + glazing_height_m)
    if top_z <= bottom_z + 0.05:
        return None

    _, center_along_m, _ = point_to_segment_metrics(
        anchor_xy_m[0],
        anchor_xy_m[1],
        start_x,
        start_y,
        end_x,
        end_y,
    )
    side_margin_m = 0.05
    center_along_m = max(
        (total_width_m / 2.0) + side_margin_m,
        min(host_length_m - ((total_width_m / 2.0) + side_margin_m), center_along_m),
    )

    left_along_m = center_along_m - (glazing_width_m / 2.0)
    right_along_m = center_along_m + (glazing_width_m / 2.0)
    left_point = interpolate_segment_point(start_x, start_y, end_x, end_y, left_along_m)
    right_point = interpolate_segment_point(start_x, start_y, end_x, end_y, right_along_m)

    row = {
        "fenestration_name": fenestration_name,
        "surface_type": surface_type if surface_type in {"Window", "Door", "GlassDoor"} else "",
        "construction_name": construction_name,
        "building_surface_name": host_wall["surface_name"],
        "outside_boundary_condition_object": "",
        "view_factor_to_ground": "",
        "frame_and_divider_name": frame_and_divider_name,
        "multiplier": "1",
        "number_of_vertices": "4",
        "v1_x": f"{left_point[0]:.3f}",
        "v1_y": f"{left_point[1]:.3f}",
        "v1_z": f"{bottom_z:.3f}",
        "v2_x": f"{left_point[0]:.3f}",
        "v2_y": f"{left_point[1]:.3f}",
        "v2_z": f"{top_z:.3f}",
        "v3_x": f"{right_point[0]:.3f}",
        "v3_y": f"{right_point[1]:.3f}",
        "v3_z": f"{top_z:.3f}",
        "v4_x": f"{right_point[0]:.3f}",
        "v4_y": f"{right_point[1]:.3f}",
        "v4_z": f"{bottom_z:.3f}",
    }
    manifest = {
        "building_surface_name": host_wall["surface_name"],
        "frame_width_m": round(frame_width_m, 3),
        "opening_total_width_m": round(total_width_m, 3),
        "opening_glazing_width_m": round(glazing_width_m, 3),
        "opening_total_height_m": round(total_height_m, 3),
        "opening_glazing_height_m": round(glazing_height_m, 3),
        "opening_total_bottom_z_m": round(total_bottom_z, 3),
        "fenestration_vertices_m": {
            "v1": [round(left_point[0], 3), round(left_point[1], 3), round(bottom_z, 3)],
            "v2": [round(left_point[0], 3), round(left_point[1], 3), round(top_z, 3)],
            "v3": [round(right_point[0], 3), round(right_point[1], 3), round(top_z, 3)],
            "v4": [round(right_point[0], 3), round(right_point[1], 3), round(bottom_z, 3)],
        },
    }
    return row, manifest


def recommended_fenestration_construction_for_host(
    surface_type: str,
    boundary_condition: str,
    *,
    reverse: bool = False,
) -> str:
    library_rule = resolve_input_opening_construction_rule(
        surface_type=surface_type,
        boundary_condition=boundary_condition,
    )
    if library_rule:
        if reverse and str(boundary_condition).strip() == "Surface":
            reverse_name = str(library_rule.get("reverse_construction_name", "")).strip()
            if reverse_name:
                return reverse_name
        construction_name = str(library_rule.get("construction_name", "")).strip()
        if construction_name:
            return construction_name
    if surface_type == "Door":
        if boundary_condition == "Surface":
            return "Project internal door_Rev" if reverse else "Project internal door"
        return "Project external door"
    if surface_type == "GlassDoor":
        return "Perfectly Clear - 1002"
    return "Dbl LoE (e2=.1) Tint 6mm/13mm Arg - 1001"


def recommended_frame_and_divider_for_host(
    surface_type: str,
    boundary_condition: str,
) -> str:
    library_rule = resolve_input_opening_construction_rule(
        surface_type=surface_type,
        boundary_condition=boundary_condition,
    )
    if library_rule:
        frame_name = str(library_rule.get("frame_and_divider_name", "")).strip()
        if frame_name:
            return frame_name
    return "1" if surface_type in {"Window", "GlassDoor"} else ""


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


def translate_point_xy(
    value: object,
    *,
    offset_x_m: float,
    offset_y_m: float,
) -> object:
    if not isinstance(value, list) or len(value) < 2:
        return value
    return [
        round(float(value[0]) - offset_x_m, 3),
        round(float(value[1]) - offset_y_m, 3),
    ]


def translate_opening_host_mapping_rows_xy(
    rows: list[dict[str, object]],
    *,
    offset_x_m: float,
    offset_y_m: float,
) -> list[dict[str, object]]:
    translated_rows: list[dict[str, object]] = []
    for row in rows:
        row_copy = dict(row)
        row_copy["placement_anchor_xy_m"] = translate_point_xy(
            row_copy.get("placement_anchor_xy_m"),
            offset_x_m=offset_x_m,
            offset_y_m=offset_y_m,
        )
        fenestration_vertices = row_copy.get("fenestration_vertices_m")
        if isinstance(fenestration_vertices, dict):
            translated_vertices: dict[str, list[float]] = {}
            for vertex_name, vertex_values in fenestration_vertices.items():
                if not isinstance(vertex_values, list) or len(vertex_values) < 3:
                    translated_vertices[vertex_name] = vertex_values
                    continue
                translated_vertices[vertex_name] = [
                    round(float(vertex_values[0]) - offset_x_m, 3),
                    round(float(vertex_values[1]) - offset_y_m, 3),
                    round(float(vertex_values[2]), 3),
                ]
            row_copy["fenestration_vertices_m"] = translated_vertices
        translated_rows.append(row_copy)
    return translated_rows


def build_fenestration_summary(
    fenestration_rows: list[dict[str, object]],
    opening_host_mapping_rows: list[dict[str, object]],
) -> dict[str, object]:
    resolution_counts = Counter(
        str(row.get("resolution_status", "") or "unknown")
        for row in opening_host_mapping_rows
    )
    review_count = sum(1 for row in opening_host_mapping_rows if bool(row.get("needs_review")))
    placement_source_counts = Counter(
        str(row.get("placement_anchor_source", "") or "none")
        for row in opening_host_mapping_rows
        if str(row.get("resolution_status", "")).strip() == "resolved"
    )
    host_surface_counts = Counter(
        str(row.get("building_surface_name", "") or "")
        for row in opening_host_mapping_rows
        if str(row.get("building_surface_name", "")).strip()
    )
    return {
        "opening_count": len(opening_host_mapping_rows),
        "resolved_opening_count": int(resolution_counts.get("resolved", 0)),
        "unresolved_opening_count": int(resolution_counts.get("unresolved", 0)),
        "needs_review_count": review_count,
        "fenestration_row_count": len(fenestration_rows),
        "placement_anchor_source_counts": counter_to_sorted_dict(placement_source_counts),
        "host_surface_counts": counter_to_sorted_dict(host_surface_counts),
    }


def build_fenestration_artifacts(
    *,
    mapping_payload: dict[str, object],
    opening_candidates: list[dict[str, object]],
    geometry_payload: dict[str, object],
    surface_rows: list[dict[str, object]],
    wall_inventory_rows: list[dict[str, object]],
    wall_resolution: dict[str, object],
    project_id: str | None = None,
) -> dict[str, object]:
    resolved_surface_rows = [dict(row) for row in surface_rows]
    base_wall_hosts_by_zone, _base_wall_hosts_by_surface_name = build_wall_host_collections(resolved_surface_rows)
    wall_hosts_by_zone, wall_hosts_by_surface_name = enrich_wall_hosts(
        base_wall_hosts_by_zone,
        wall_inventory_rows,
        wall_resolution,
    )
    all_host_walls = [
        host_wall
        for host_wall_list in wall_hosts_by_zone.values()
        for host_wall in host_wall_list
    ]
    available_zone_names = set(wall_hosts_by_zone)
    apartment_prefix = infer_apartment_prefix(geometry_payload)
    zone_name_map = build_zone_name_map(
        mapping_payload,
        geometry_payload=geometry_payload,
        apartment_prefix=apartment_prefix,
    )
    zone_anchor_xy_by_csv = build_zone_anchor_xy_by_csv(mapping_payload, zone_name_map)
    zone_rectangles_by_csv = build_zone_rectangles_by_csv(
        mapping_payload,
        geometry_payload,
        zone_name_map,
        available_zone_names,
    )
    zone_polygons_by_csv = build_zone_polygons_by_csv(
        mapping_payload,
        geometry_payload,
        zone_name_map,
        available_zone_names,
    )
    placement_segments = [
        dict(segment)
        for segment in list(mapping_payload.get("opening_placement_segments", []))
        if isinstance(segment, dict)
    ]
    geometry_mode = str(geometry_payload.get("geometry_mode", ""))
    geometry_source = str(geometry_payload.get("geometry_source", "") or "")
    geometry_upstream_source = str(mapping_payload.get("upstream_source", "") or "")
    export_origin_offset_m = list(geometry_payload.get("export_origin_offset_m", [0.0, 0.0, 0.0]))
    if len(export_origin_offset_m) < 2:
        export_origin_offset_m = [0.0, 0.0, 0.0]
    export_origin_offset_x_m = float(export_origin_offset_m[0] or 0.0)
    export_origin_offset_y_m = float(export_origin_offset_m[1] or 0.0)

    fenestration_rows: list[dict[str, object]] = []
    opening_host_mapping_rows: list[dict[str, object]] = []
    for opening in opening_candidates:
        if not isinstance(opening, dict):
            continue

        source_zone_name = str(opening.get("nearest_zone_name", "")).strip()
        source_zone_csv_name = resolve_csv_zone_name(source_zone_name, zone_name_map, available_zone_names)
        surface_type = str(opening.get("candidate_fenestration_type", "")).strip()
        is_import_proxy = bool(opening.get("idf_import_proxy", False))
        idf_surface_type = str(opening.get("idf_import_surface_type", "") or surface_type).strip()
        idf_name_suffix = str(opening.get("idf_import_name_suffix", "") or "").strip()
        idf_construction_override = str(opening.get("idf_import_construction_name", "") or "").strip()
        idf_frame_override = str(opening.get("idf_import_frame_and_divider_name", "") or "").strip()
        idf_proxy_review_reason = str(opening.get("idf_import_proxy_review_reason", "") or "idf_import_proxy").strip()
        opening_id = str(opening.get("opening_id", "OPENING"))
        resolved_width_mm, resolved_height_mm, size_resolution_method = resolve_opening_dimensions_for_surface_mm(
            opening,
            surface_type,
        )
        hole_touches_ceiling = surface_type == "Hole" and resolved_height_mm is None
        if surface_type == "Hole" and resolved_width_mm is None:
            geometry_width_mm = opening_geometry_length_mm(opening)
            if geometry_width_mm is not None:
                resolved_width_mm = geometry_width_mm
                size_resolution_method = "opening_layer_geometry_width_ceiling_height"
        sill_height_m = float(opening.get("sill_height_m", 0.0) or 0.0)
        review_reasons: list[str] = []
        if surface_type in REVIEW_TYPES:
            review_reasons.append("candidate_fenestration_type_needs_review")
        if is_import_proxy and idf_proxy_review_reason:
            review_reasons.append(idf_proxy_review_reason)
        if str(opening.get("mapping_confidence", "")).strip() not in {"high", ""}:
            review_reasons.append("mapping_confidence_below_high")
        if size_resolution_method == "size_text_reparse_override":
            review_reasons.append("size_text_overrode_opening_payload")

        host_wall, placement_anchor_xy_m, host_selection_manifest = choose_host_wall_for_opening(
            opening=opening,
            source_zone_name=source_zone_name,
            source_zone_csv_name=source_zone_csv_name,
            source_zone_anchor_xy_m=zone_anchor_xy_by_csv.get(source_zone_csv_name),
            zone_anchor_xy_by_name=zone_anchor_xy_by_csv,
            zone_rectangles_by_name=zone_rectangles_by_csv,
            zone_polygons_by_name=zone_polygons_by_csv,
            host_walls=all_host_walls,
        )

        mapping_row: dict[str, object] = {
            "opening_id": opening_id,
            "resolution_status": "unresolved",
            "needs_review": False,
            "review_reasons": [],
            "unresolved_reason": "",
            "fenestration_name": "",
            "paired_fenestration_name": "",
            "source_type_code": opening.get("type_code"),
            "candidate_fenestration_type": surface_type,
            "idf_import_proxy": is_import_proxy,
            "idf_surface_type": idf_surface_type,
            "idf_name_suffix": idf_name_suffix,
            "construction_name": "",
            "nearest_zone_name": source_zone_name,
            "nearest_zone_csv_name": source_zone_csv_name,
            "size_text": opening.get("size_text"),
            "width_mm": resolved_width_mm,
            "height_mm": resolved_height_mm,
            "sill_height_m": opening.get("sill_height_m"),
            "size_resolution_method": size_resolution_method,
            "mapping_confidence": opening.get("mapping_confidence"),
            "matched_symbol_insert_handle": opening.get("matched_symbol_insert_handle"),
            "host_boundary_condition": str(host_wall.get("boundary_condition", "")) if host_wall else "",
            "host_adjacent_zone_name": str(host_wall.get("adjacent_zone_name", "")) if host_wall else "",
            "host_wall_physical_id": str(host_wall.get("physical_wall_id", "")) if host_wall else "",
            "host_wall_family": str(host_wall.get("wall_family", "")) if host_wall else "",
            "host_wall_total_thickness_mm": int(host_wall.get("inventory_total_thickness_mm", 0) or 0) if host_wall else 0,
            "geometry_mode": geometry_mode,
            "geometry_source": geometry_source,
            "geometry_upstream_source": geometry_upstream_source,
            "placement_anchor_xy_m": None,
            "placement_anchor_source": "",
            "building_surface_name": str(host_wall.get("surface_name", "")) if host_wall else "",
            "fenestration_vertices_m": None,
            **host_selection_manifest,
        }

        if surface_type in UNSUPPORTED_IDF_OPENING_TYPES and not is_import_proxy:
            mapping_row["unresolved_reason"] = "idf_hole_surface_type_not_supported"
            mapping_row["needs_review"] = True
            mapping_row["review_reasons"] = [*review_reasons, "idf_hole_surface_type_not_supported"]
            opening_host_mapping_rows.append(mapping_row)
            continue

        if resolved_width_mm is None or (resolved_height_mm is None and not hole_touches_ceiling):
            mapping_row["unresolved_reason"] = "opening_size_not_resolved_from_text"
            mapping_row["needs_review"] = True
            mapping_row["review_reasons"] = [*review_reasons, "opening_size_not_resolved_from_text"]
            opening_host_mapping_rows.append(mapping_row)
            continue

        opening_width_m = max(0.5, float(resolved_width_mm) / 1000.0)
        opening_height_m = max(0.5, float(resolved_height_mm) / 1000.0) if resolved_height_mm is not None else 0.5

        if host_wall is None or placement_anchor_xy_m is None:
            mapping_row["unresolved_reason"] = "host_wall_not_resolved"
            mapping_row["needs_review"] = bool(review_reasons)
            mapping_row["review_reasons"] = review_reasons
            opening_host_mapping_rows.append(mapping_row)
            continue
        if (
            str(host_wall.get("boundary_condition", "")) == "Adiabatic"
            and surface_type in {"Door", "GlassDoor", "Window"}
        ):
            adiabatic_exception_reason = allowed_adiabatic_opening_export_reason(
                geometry_upstream_source=geometry_upstream_source,
                opening_id=opening_id,
                building_surface_name=str(host_wall.get("surface_name", "")),
                surface_type=surface_type,
            )
            if not adiabatic_exception_reason:
                mapping_row["unresolved_reason"] = "host_wall_adiabatic_not_valid_for_opening"
                mapping_row["needs_review"] = True
                mapping_row["review_reasons"] = [*review_reasons, "host_wall_adiabatic_not_valid_for_opening"]
                opening_host_mapping_rows.append(mapping_row)
                continue
            mapping_row["adiabatic_opening_exception"] = True
            mapping_row["adiabatic_opening_exception_reason"] = adiabatic_exception_reason

        placement_anchor_xy_m, placement_anchor_source, width_override_m = choose_placement_anchor_for_host(
            opening=opening,
            host_wall=host_wall,
            primary_anchor_xy_m=placement_anchor_xy_m,
            placement_segments=placement_segments,
            opening_width_m=opening_width_m,
        )
        if placement_anchor_xy_m is None:
            mapping_row["unresolved_reason"] = "placement_anchor_not_resolved"
            mapping_row["needs_review"] = True
            mapping_row["review_reasons"] = [*review_reasons, "placement_anchor_not_resolved"]
            opening_host_mapping_rows.append(mapping_row)
            continue
        if width_override_m is not None:
            opening_width_m = width_override_m
            resolved_width_mm = int(round(opening_width_m * 1000.0))
            mapping_row["width_mm"] = resolved_width_mm

        if hole_touches_ceiling:
            host_height_m = float(host_wall.get("height_m", 0.0) or 0.0)
            opening_height_m = max(0.5, host_height_m - max(0.0, sill_height_m))
            resolved_height_mm = int(round(opening_height_m * 1000.0))
            mapping_row["height_mm"] = resolved_height_mm
            mapping_row["hole_height_rule"] = "touch_ceiling"
            mapping_row["hole_top_z_m"] = round(host_height_m, 3)
            if size_resolution_method == "size_text":
                mapping_row["size_resolution_method"] = "size_text_width_hole_touch_ceiling_height"
            elif size_resolution_method == "opening_payload":
                mapping_row["size_resolution_method"] = "opening_payload_width_hole_touch_ceiling_height"

        fenestration_name_token = idf_name_suffix or ascii_token(idf_surface_type or surface_type or "Opening")
        fenestration_name = f"{apartment_prefix}_{opening_id}_{fenestration_name_token}"
        construction_name = (
            idf_construction_override
            or envelope_construction_for_opening(
                project_id,
                opening=opening,
                surface_type=idf_surface_type,
            )
            or recommended_fenestration_construction_for_host(
                idf_surface_type,
                str(host_wall.get("boundary_condition", "")),
            )
        )
        frame_and_divider_name = (
            idf_frame_override
            if is_import_proxy
            else recommended_frame_and_divider_for_host(
                idf_surface_type,
                str(host_wall.get("boundary_condition", "")),
            )
        )
        fenestration_payload = build_fenestration_row_for_host(
            fenestration_name=fenestration_name,
            surface_type=idf_surface_type,
            construction_name=construction_name,
            frame_and_divider_name=frame_and_divider_name,
            host_wall=host_wall,
            opening_width_m=opening_width_m,
            opening_height_m=opening_height_m,
            sill_height_m=sill_height_m,
            anchor_xy_m=placement_anchor_xy_m,
            touches_ceiling=hole_touches_ceiling,
        )
        if fenestration_payload is None:
            mapping_row["unresolved_reason"] = "fenestration_geometry_not_resolved"
            mapping_row["needs_review"] = True
            mapping_row["review_reasons"] = [*review_reasons, "fenestration_geometry_not_resolved"]
            opening_host_mapping_rows.append(mapping_row)
            continue

        fenestration_row, opening_geometry_manifest = fenestration_payload
        paired_fenestration_name = ""
        host_boundary_condition = str(host_wall.get("boundary_condition", ""))
        peer_wall = wall_hosts_by_surface_name.get(str(host_wall.get("paired_surface_name", "")))
        if host_boundary_condition == "Surface" and peer_wall is not None:
            paired_fenestration_name = f"{fenestration_name}_ADJ"
            paired_payload = build_fenestration_row_for_host(
                fenestration_name=paired_fenestration_name,
                surface_type=idf_surface_type,
                construction_name=(
                    idf_construction_override
                    or envelope_construction_for_opening(
                        project_id,
                        opening=opening,
                        surface_type=idf_surface_type,
                    )
                    or recommended_fenestration_construction_for_host(
                        idf_surface_type,
                        str(peer_wall.get("boundary_condition", "")),
                        reverse=True,
                    )
                ),
                frame_and_divider_name=(
                    idf_frame_override
                    if is_import_proxy
                    else recommended_frame_and_divider_for_host(
                        idf_surface_type,
                        str(peer_wall.get("boundary_condition", "")),
                    )
                ),
                host_wall=peer_wall,
                opening_width_m=opening_width_m,
                opening_height_m=opening_height_m,
                sill_height_m=sill_height_m,
                anchor_xy_m=placement_anchor_xy_m,
                touches_ceiling=hole_touches_ceiling,
            )
            if paired_payload is not None:
                paired_row, _paired_geometry_manifest = paired_payload
                fenestration_row["outside_boundary_condition_object"] = paired_fenestration_name
                paired_row["outside_boundary_condition_object"] = fenestration_name
                fenestration_rows.extend([fenestration_row, paired_row])
            else:
                review_reasons.append("paired_interzone_fenestration_not_resolved")
                fenestration_rows.append(fenestration_row)
        else:
            fenestration_rows.append(fenestration_row)

        mapping_row.update(
            {
                "resolution_status": "resolved",
                "fenestration_name": fenestration_name,
                "paired_fenestration_name": paired_fenestration_name,
                "construction_name": construction_name,
                "host_boundary_condition": host_boundary_condition,
                "host_adjacent_zone_name": host_wall.get("adjacent_zone_name"),
                "host_wall_physical_id": host_wall.get("physical_wall_id", ""),
                "host_wall_family": host_wall.get("wall_family", ""),
                "host_wall_total_thickness_mm": int(host_wall.get("inventory_total_thickness_mm", 0) or 0),
                "placement_anchor_xy_m": [round(placement_anchor_xy_m[0], 3), round(placement_anchor_xy_m[1], 3)],
                "placement_anchor_source": placement_anchor_source,
                "building_surface_name": host_wall.get("surface_name"),
                **opening_geometry_manifest,
            }
        )
        mapping_row["needs_review"] = bool(review_reasons)
        mapping_row["review_reasons"] = review_reasons
        opening_host_mapping_rows.append(mapping_row)

    translated_fenestration_rows = translate_vertex_rows_xy(
        fenestration_rows,
        offset_x_m=export_origin_offset_x_m,
        offset_y_m=export_origin_offset_y_m,
    )
    translated_opening_host_mapping_rows = translate_opening_host_mapping_rows_xy(
        opening_host_mapping_rows,
        offset_x_m=export_origin_offset_x_m,
        offset_y_m=export_origin_offset_y_m,
    )

    summary = build_fenestration_summary(
        translated_fenestration_rows,
        translated_opening_host_mapping_rows,
    )
    return {
        "fenestration_rows": translated_fenestration_rows,
        "opening_host_mapping_rows": translated_opening_host_mapping_rows,
        "summary": summary,
    }


def write_fenestration_outputs(
    fenestration_artifacts: dict[str, object],
    *,
    output_dir: Path | str | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    resolved_output_dir = GUARD.resolve(output_dir or _resolve_default_output_dir(path_resolver.resolve_project_id(project_id)))
    if project_id is not None:
        path_resolver.assert_output_in_project_scope(path_resolver.resolve_project_id(project_id), resolved_output_dir)
    targets = {
        "fenestration_rows": resolved_output_dir / "fenestration_rows.json",
        "opening_host_mapping": resolved_output_dir / "opening_host_mapping.json",
    }
    GUARD.write_json(
        targets["fenestration_rows"],
        list(fenestration_artifacts.get("fenestration_rows", [])),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    GUARD.write_json(
        targets["opening_host_mapping"],
        list(fenestration_artifacts.get("opening_host_mapping_rows", [])),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    return {key: workspace_path(path) for key, path in targets.items()}


def build_fenestration_artifacts_from_paths(
    *,
    mapping_payload_path: Path,
    opening_candidates_path: Path,
    geometry_payload_path: Path,
    surface_rows_path: Path,
    wall_inventory_path: Path,
    wall_resolution_path: Path,
    project_id: str | None = None,
) -> dict[str, object]:
    mapping_payload = load_json_object(mapping_payload_path)
    opening_candidates = load_json_list(opening_candidates_path)
    geometry_payload = load_json_object(geometry_payload_path)
    surface_rows = load_json_list(surface_rows_path)
    wall_inventory_rows = load_json_list(wall_inventory_path)
    wall_resolution = load_json_object(wall_resolution_path)
    return build_fenestration_artifacts(
        mapping_payload=mapping_payload,
        opening_candidates=opening_candidates,
        geometry_payload=geometry_payload,
        surface_rows=surface_rows,
        wall_inventory_rows=wall_inventory_rows,
        wall_resolution=wall_resolution,
        project_id=project_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build fenestration rows and opening-host mapping from intermediate artifacts."
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project ID used to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--mapping-payload",
        type=Path,
        default=None,
        help="Path to mapping_payload.json. If omitted, resolves from 5_output/<project_id>/intermediate/mapping.",
    )
    parser.add_argument(
        "--opening-candidates",
        type=Path,
        default=None,
        help="Path to opening_candidates.json. If omitted, resolves from 5_output/<project_id>/intermediate/mapping.",
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
        "--wall-inventory",
        type=Path,
        default=None,
        help="Path to wall_inventory.json. If omitted, resolves from 5_output/<project_id>/intermediate/walls.",
    )
    parser.add_argument(
        "--wall-resolution",
        type=Path,
        default=None,
        help="Path to wall_resolution.json. If omitted, resolves from 5_output/<project_id>/intermediate/walls.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory where fenestration intermediate JSON outputs will be written. If omitted, defaults to 5_output/<project_id>/intermediate/fenestration.",
    )
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)
    resolved_inputs = _resolve_default_project_inputs(project_id)

    fenestration_artifacts = build_fenestration_artifacts_from_paths(
        mapping_payload_path=args.mapping_payload or resolved_inputs["mapping_payload"],
        opening_candidates_path=args.opening_candidates or resolved_inputs["opening_candidates"],
        geometry_payload_path=args.geometry_payload or resolved_inputs["geometry_payload"],
        surface_rows_path=args.surface_rows or resolved_inputs["surface_rows"],
        wall_inventory_path=args.wall_inventory or resolved_inputs["wall_inventory"],
        wall_resolution_path=args.wall_resolution or resolved_inputs["wall_resolution"],
        project_id=project_id,
    )
    written_paths = write_fenestration_outputs(
        fenestration_artifacts,
        output_dir=args.output_dir or _resolve_default_output_dir(project_id),
        project_id=project_id,
    )
    summary = dict(fenestration_artifacts.get("summary", {}))
    print("Resolved openings:", summary.get("resolved_opening_count", 0))
    print("Unresolved openings:", summary.get("unresolved_opening_count", 0))
    print("Needs review:", summary.get("needs_review_count", 0))
    print("Fenestration rows:", summary.get("fenestration_row_count", 0))
    print("Fenestration rows path:", written_paths.get("fenestration_rows", ""))
    print("Opening host mapping path:", written_paths.get("opening_host_mapping", ""))


if __name__ == "__main__":
    main()
