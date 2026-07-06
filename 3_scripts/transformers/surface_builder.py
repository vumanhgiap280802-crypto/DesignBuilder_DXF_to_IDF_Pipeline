#!/usr/bin/env python3
"""
Build intermediate surface rows and adjacency context from inferred geometry.

This transformer works only from geometry payload artifacts. It does not parse raw
DXF, does not infer geometry, and does not build final CSV/IDF bundles.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils import path_resolver  # noqa: E402
from utils.common import load_json_object, normalize_rect, workspace_path  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_GEOMETRY_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "geometry" / "geometry_payload.json"
DEFAULT_OUTPUT_DIR = Path("5_output") / "<project_id>" / "intermediate" / "surfaces"


def _resolve_default_geometry_payload(project_id: str) -> Path:
    resolved = path_resolver.resolve_output_file_for_read(project_id, "intermediate/geometry", "geometry_payload.json")
    if resolved is None:
        raise WorkspaceRuleError(f"No geometry payload found for project '{project_id}'.")
    return resolved


def _resolve_default_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/surfaces")


def wall_vertex_payload(
    x1: float,
    y1: float,
    z1: float,
    x2: float,
    y2: float,
    z2: float,
    x3: float,
    y3: float,
    z3: float,
    x4: float,
    y4: float,
    z4: float,
) -> dict[str, str]:
    return {
        "number_of_vertices": "4",
        "v1_x": f"{x1:.3f}",
        "v1_y": f"{y1:.3f}",
        "v1_z": f"{z1:.3f}",
        "v2_x": f"{x2:.3f}",
        "v2_y": f"{y2:.3f}",
        "v2_z": f"{z2:.3f}",
        "v3_x": f"{x3:.3f}",
        "v3_y": f"{y3:.3f}",
        "v3_z": f"{z3:.3f}",
        "v4_x": f"{x4:.3f}",
        "v4_y": f"{y4:.3f}",
        "v4_z": f"{z4:.3f}",
    }


def surface_vertex_payload(vertices: list[tuple[float, float, float]]) -> dict[str, str]:
    payload = {"number_of_vertices": str(len(vertices))}
    for index, (x_value, y_value, z_value) in enumerate(vertices, start=1):
        payload[f"v{index}_x"] = f"{x_value:.3f}"
        payload[f"v{index}_y"] = f"{y_value:.3f}"
        payload[f"v{index}_z"] = f"{z_value:.3f}"
    return payload


def polygon_signed_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += (x1 * y2) - (x2 * y1)
    return area / 2.0


def _cross_2d(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return ((second[0] - first[0]) * (third[1] - first[1])) - (
        (second[1] - first[1]) * (third[0] - first[0])
    )


def clean_polygon_points(points: list[tuple[float, float]], *, tolerance_m: float = 1e-6) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for point_x, point_y in points:
        point = (float(point_x), float(point_y))
        if cleaned and math.hypot(point[0] - cleaned[-1][0], point[1] - cleaned[-1][1]) <= tolerance_m:
            continue
        cleaned.append(point)
    if len(cleaned) >= 2 and math.hypot(cleaned[0][0] - cleaned[-1][0], cleaned[0][1] - cleaned[-1][1]) <= tolerance_m:
        cleaned.pop()

    changed = True
    while changed and len(cleaned) >= 3:
        changed = False
        next_points: list[tuple[float, float]] = []
        for index, point in enumerate(cleaned):
            prev_point = cleaned[index - 1]
            next_point = cleaned[(index + 1) % len(cleaned)]
            if abs(_cross_2d(prev_point, point, next_point)) <= tolerance_m:
                changed = True
                continue
            next_points.append(point)
        if next_points:
            cleaned = next_points
    if len(cleaned) < 3 or abs(polygon_signed_area(cleaned)) <= tolerance_m:
        return []
    return cleaned


def ensure_counterclockwise_polygon(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    polygon = clean_polygon_points(points)
    if not polygon:
        return []
    if polygon_signed_area(polygon) < 0.0:
        return list(reversed(polygon))
    return polygon


def snap_scalar_values(values: list[float], *, tolerance_m: float) -> dict[float, float]:
    if not values:
        return {}

    snapped: dict[float, float] = {}
    current_group: list[float] = []
    for value in sorted(set(float(item) for item in values)):
        if current_group and abs(value - current_group[-1]) > tolerance_m:
            canonical_value = sum(current_group) / len(current_group)
            for group_value in current_group:
                snapped[group_value] = canonical_value
            current_group = []
        current_group.append(value)

    if current_group:
        canonical_value = sum(current_group) / len(current_group)
        for group_value in current_group:
            snapped[group_value] = canonical_value
    return snapped


def snap_zone_polygon_vertices(
    zone_polygons_by_name: dict[str, list[list[tuple[float, float]]]],
    *,
    tolerance_m: float = 0.01,
) -> dict[str, list[list[tuple[float, float]]]]:
    cleaned_by_zone: dict[str, list[list[tuple[float, float]]]] = {}
    x_values: list[float] = []
    y_values: list[float] = []

    for zone_name, polygons in zone_polygons_by_name.items():
        for raw_polygon in polygons:
            polygon = ensure_counterclockwise_polygon(raw_polygon)
            if len(polygon) < 3:
                continue
            cleaned_by_zone.setdefault(zone_name, []).append(polygon)
            for x_value, y_value in polygon:
                x_values.append(x_value)
                y_values.append(y_value)

    x_snap = snap_scalar_values(x_values, tolerance_m=tolerance_m)
    y_snap = snap_scalar_values(y_values, tolerance_m=tolerance_m)
    snapped_by_zone: dict[str, list[list[tuple[float, float]]]] = {}
    for zone_name, polygons in cleaned_by_zone.items():
        for polygon in polygons:
            snapped_polygon = ensure_counterclockwise_polygon(
                [
                    (x_snap.get(x_value, x_value), y_snap.get(y_value, y_value))
                    for x_value, y_value in polygon
                ]
            )
            if len(snapped_polygon) >= 3:
                snapped_by_zone.setdefault(zone_name, []).append(snapped_polygon)
    return snapped_by_zone


def rectangle_to_polygon(rectangle: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    min_x, min_y, max_x, max_y = normalize_rect(rectangle)
    return [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]


def merge_oriented_boundary_segments(
    segments: list[tuple[str, float, float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    grouped: dict[tuple[str, float], list[tuple[float, float]]] = {}
    for orientation, fixed_coord, start, end in segments:
        key = (orientation, round(fixed_coord, 6))
        grouped.setdefault(key, []).append((start, end))

    merged: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for (orientation, fixed_coord), spans in grouped.items():
        if orientation == "H_EAST":
            spans = sorted(spans, key=lambda item: item[0])
            current_start, current_end = spans[0]
            for start, end in spans[1:]:
                if abs(current_end - start) <= 1e-6:
                    current_end = end
                else:
                    merged.append(((current_start, fixed_coord), (current_end, fixed_coord)))
                    current_start, current_end = start, end
            merged.append(((current_start, fixed_coord), (current_end, fixed_coord)))
        elif orientation == "H_WEST":
            spans = sorted(spans, key=lambda item: item[0], reverse=True)
            current_start, current_end = spans[0]
            for start, end in spans[1:]:
                if abs(current_end - start) <= 1e-6:
                    current_end = end
                else:
                    merged.append(((current_start, fixed_coord), (current_end, fixed_coord)))
                    current_start, current_end = start, end
            merged.append(((current_start, fixed_coord), (current_end, fixed_coord)))
        elif orientation == "V_NORTH":
            spans = sorted(spans, key=lambda item: item[0])
            current_start, current_end = spans[0]
            for start, end in spans[1:]:
                if abs(current_end - start) <= 1e-6:
                    current_end = end
                else:
                    merged.append(((fixed_coord, current_start), (fixed_coord, current_end)))
                    current_start, current_end = start, end
            merged.append(((fixed_coord, current_start), (fixed_coord, current_end)))
        elif orientation == "V_SOUTH":
            spans = sorted(spans, key=lambda item: item[0], reverse=True)
            current_start, current_end = spans[0]
            for start, end in spans[1:]:
                if abs(current_end - start) <= 1e-6:
                    current_end = end
                else:
                    merged.append(((fixed_coord, current_start), (fixed_coord, current_end)))
                    current_start, current_end = start, end
            merged.append(((fixed_coord, current_start), (fixed_coord, current_end)))

    return merged


def build_union_boundary_segments(
    rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if not rectangles:
        return []

    normalized_rectangles = [normalize_rect(rect) for rect in rectangles]
    xs = sorted({round(value, 6) for rect in normalized_rectangles for value in (rect[0], rect[2])})
    ys = sorted({round(value, 6) for rect in normalized_rectangles for value in (rect[1], rect[3])})
    if len(xs) < 2 or len(ys) < 2:
        return []

    occupied = [[False for _ in range(len(ys) - 1)] for _ in range(len(xs) - 1)]
    for x_index in range(len(xs) - 1):
        center_x = (xs[x_index] + xs[x_index + 1]) / 2.0
        for y_index in range(len(ys) - 1):
            center_y = (ys[y_index] + ys[y_index + 1]) / 2.0
            occupied[x_index][y_index] = any(
                rect[0] < center_x < rect[2] and rect[1] < center_y < rect[3]
                for rect in normalized_rectangles
            )

    raw_segments: list[tuple[str, float, float, float]] = []
    max_x_index = len(xs) - 2
    max_y_index = len(ys) - 2
    for x_index in range(len(xs) - 1):
        for y_index in range(len(ys) - 1):
            if not occupied[x_index][y_index]:
                continue

            x1 = xs[x_index]
            x2 = xs[x_index + 1]
            y1 = ys[y_index]
            y2 = ys[y_index + 1]

            if y_index == 0 or not occupied[x_index][y_index - 1]:
                raw_segments.append(("H_EAST", y1, x1, x2))
            if y_index == max_y_index or not occupied[x_index][y_index + 1]:
                raw_segments.append(("H_WEST", y2, x2, x1))
            if x_index == 0 or not occupied[x_index - 1][y_index]:
                raw_segments.append(("V_SOUTH", x1, y2, y1))
            if x_index == max_x_index or not occupied[x_index + 1][y_index]:
                raw_segments.append(("V_NORTH", x2, y1, y2))

    return merge_oriented_boundary_segments(raw_segments)


def build_wall_surface_row(
    *,
    zone_name: str,
    surface_name: str,
    start: tuple[float, float],
    end: tuple[float, float],
    height_m: float,
    outside_boundary_condition: str,
    outside_boundary_condition_object: str,
    construction_name: str,
    reverse_construction: bool = False,
) -> dict[str, object]:
    sun_exposure = "SunExposed" if outside_boundary_condition == "Outdoors" else "NoSun"
    wind_exposure = "WindExposed" if outside_boundary_condition == "Outdoors" else "NoWind"
    x1, y1 = start
    x2, y2 = end
    return {
        "surface_name": surface_name,
        "surface_type": "Wall",
        "construction_name": construction_name,
        "zone_name": zone_name,
        "outside_boundary_condition": outside_boundary_condition,
        "outside_boundary_condition_object": outside_boundary_condition_object,
        "sun_exposure": sun_exposure,
        "wind_exposure": wind_exposure,
        "view_factor_to_ground": "",
        "inferred_construction_reverse": reverse_construction,
        "inferred_wall_thickness_mm": None,
        "wall_thickness_inference_source": "",
        **wall_vertex_payload(
            x1,
            y1,
            0.0,
            x1,
            y1,
            height_m,
            x2,
            y2,
            height_m,
            x2,
            y2,
            0.0,
        ),
    }


def boundary_segment_metadata(
    start: tuple[float, float],
    end: tuple[float, float],
) -> dict[str, object] | None:
    x1, y1 = start
    x2, y2 = end
    if abs(y1 - y2) <= 1e-6:
        return {
            "axis": "horizontal",
            "fixed_coord": y1,
            "var_min": min(x1, x2),
            "var_max": max(x1, x2),
            "side": "south" if x1 < x2 else "north",
        }
    if abs(x1 - x2) <= 1e-6:
        return {
            "axis": "vertical",
            "fixed_coord": x1,
            "var_min": min(y1, y2),
            "var_max": max(y1, y2),
            "side": "east" if y1 < y2 else "west",
        }
    return None


def oriented_segment_points(
    *,
    axis: str,
    side: str,
    fixed_coord: float,
    var_min: float,
    var_max: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    if axis == "horizontal":
        if side == "south":
            return (var_min, fixed_coord), (var_max, fixed_coord)
        return (var_max, fixed_coord), (var_min, fixed_coord)

    if side == "east":
        return (fixed_coord, var_min), (fixed_coord, var_max)
    return (fixed_coord, var_max), (fixed_coord, var_min)


def interval_overlaps(
    existing_intervals: list[tuple[float, float]],
    candidate_interval: tuple[float, float],
) -> bool:
    candidate_min, candidate_max = candidate_interval
    for interval_min, interval_max in existing_intervals:
        if min(interval_max, candidate_max) - max(interval_min, candidate_min) > 1e-6:
            return True
    return False


def subtract_intervals(
    base_interval: tuple[float, float],
    used_intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    interval_start, interval_end = base_interval
    remaining: list[tuple[float, float]] = [(interval_start, interval_end)]
    for used_start, used_end in sorted(used_intervals):
        next_remaining: list[tuple[float, float]] = []
        for current_start, current_end in remaining:
            overlap_start = max(current_start, used_start)
            overlap_end = min(current_end, used_end)
            if overlap_end - overlap_start <= 1e-6:
                next_remaining.append((current_start, current_end))
                continue
            if overlap_start - current_start > 1e-6:
                next_remaining.append((current_start, overlap_start))
            if current_end - overlap_end > 1e-6:
                next_remaining.append((overlap_end, current_end))
        remaining = next_remaining
    return remaining


def segment_is_exterior_relative_to_outer_rect(
    segment: dict[str, object],
    outer_rectangle: tuple[float, float, float, float] | None,
    *,
    exterior_gap_tolerance_m: float,
) -> bool:
    if outer_rectangle is None:
        return True

    outer_min_x, outer_min_y, outer_max_x, outer_max_y = normalize_rect(outer_rectangle)
    axis = str(segment.get("axis", ""))
    side = str(segment.get("side", ""))
    fixed_coord = float(segment.get("fixed_coord", 0.0))

    if axis == "horizontal":
        if side == "south":
            return fixed_coord - outer_min_y <= exterior_gap_tolerance_m + 1e-9
        if side == "north":
            return outer_max_y - fixed_coord <= exterior_gap_tolerance_m + 1e-9
        return False

    if side == "west":
        return fixed_coord - outer_min_x <= exterior_gap_tolerance_m + 1e-9
    if side == "east":
        return outer_max_x - fixed_coord <= exterior_gap_tolerance_m + 1e-9
    return False


def general_boundary_segment_metadata(
    start: tuple[float, float],
    end: tuple[float, float],
) -> dict[str, object]:
    metadata = boundary_segment_metadata(start, end)
    if metadata is not None:
        return metadata
    return {
        "axis": "diagonal",
        "fixed_coord": None,
        "var_min": 0.0,
        "var_max": math.hypot(end[0] - start[0], end[1] - start[1]),
        "side": "",
    }


def segment_length(start: tuple[float, float], end: tuple[float, float]) -> float:
    return math.hypot(end[0] - start[0], end[1] - start[1])


def segment_point(
    start: tuple[float, float],
    end: tuple[float, float],
    position: float,
) -> tuple[float, float]:
    return (
        start[0] + ((end[0] - start[0]) * position),
        start[1] + ((end[1] - start[1]) * position),
    )


def project_point_to_segment_position(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = (dx * dx) + (dy * dy)
    if length_sq <= 1e-12:
        return 0.0
    return (((point[0] - start[0]) * dx) + ((point[1] - start[1]) * dy)) / length_sq


def point_line_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    length = segment_length(start, end)
    if length <= 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    return abs(((end[0] - start[0]) * (start[1] - point[1])) - ((start[0] - point[0]) * (end[1] - start[1]))) / length


def collinear_overlap_payload(
    first_segment: dict[str, object],
    second_segment: dict[str, object],
    *,
    min_overlap_m: float,
    tolerance_m: float = 0.02,
) -> dict[str, object] | None:
    first_start = tuple(first_segment["start"])  # type: ignore[arg-type]
    first_end = tuple(first_segment["end"])  # type: ignore[arg-type]
    second_start = tuple(second_segment["start"])  # type: ignore[arg-type]
    second_end = tuple(second_segment["end"])  # type: ignore[arg-type]
    first_length = segment_length(first_start, first_end)
    second_length = segment_length(second_start, second_end)
    if first_length <= 1e-9 or second_length <= 1e-9:
        return None

    first_dx = first_end[0] - first_start[0]
    first_dy = first_end[1] - first_start[1]
    second_dx = second_end[0] - second_start[0]
    second_dy = second_end[1] - second_start[1]
    cross = abs((first_dx * second_dy) - (first_dy * second_dx))
    if cross / (first_length * second_length) > 1e-4:
        return None
    if point_line_distance(second_start, first_start, first_end) > tolerance_m:
        return None
    if point_line_distance(second_end, first_start, first_end) > tolerance_m:
        return None

    second_positions_on_first = sorted(
        [
            project_point_to_segment_position(second_start, first_start, first_end),
            project_point_to_segment_position(second_end, first_start, first_end),
        ]
    )
    first_start_position = max(0.0, second_positions_on_first[0])
    first_end_position = min(1.0, second_positions_on_first[1])
    if first_end_position - first_start_position <= 1e-9:
        return None
    overlap_start = segment_point(first_start, first_end, first_start_position)
    overlap_end = segment_point(first_start, first_end, first_end_position)
    overlap_length = segment_length(overlap_start, overlap_end)
    if overlap_length < min_overlap_m:
        return None

    second_start_position = project_point_to_segment_position(overlap_start, second_start, second_end)
    second_end_position = project_point_to_segment_position(overlap_end, second_start, second_end)
    second_interval = tuple(sorted((max(0.0, second_start_position), min(1.0, second_end_position))))
    return {
        "first_interval": (first_start_position, first_end_position),
        "second_interval": second_interval,
        "overlap_length_m": overlap_length,
        "first_start": overlap_start,
        "first_end": overlap_end,
        "second_start": segment_point(second_start, second_end, second_interval[0]),
        "second_end": segment_point(second_start, second_end, second_interval[1]),
    }


def interval_already_used(
    used_intervals: list[tuple[float, float]],
    candidate_interval: tuple[float, float],
) -> bool:
    return interval_overlaps(used_intervals, candidate_interval)


def add_wall_host(
    wall_hosts_by_zone: dict[str, list[dict[str, object]]],
    host: dict[str, object],
) -> None:
    zone_name = str(host.get("zone_name", "")).strip()
    if zone_name:
        wall_hosts_by_zone.setdefault(zone_name, []).append(host)


def build_zone_surfaces_from_polygons_with_adjacency(
    *,
    zone_polygons_by_name: dict[str, list[list[tuple[float, float]]]],
    zone_heights_by_name: dict[str, float],
    outer_boundary_rect: tuple[float, float, float, float] | None = None,
    min_overlap_m: float = 0.01,
    exterior_gap_tolerance_m: float = 0.50,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]], dict[str, object]]:
    surface_rows: list[dict[str, object]] = []
    wall_hosts_by_zone: dict[str, list[dict[str, object]]] = {zone_name: [] for zone_name in zone_polygons_by_name}
    wall_index_by_zone: Counter[str] = Counter()
    floor_index_by_zone: Counter[str] = Counter()
    roof_index_by_zone: Counter[str] = Counter()
    boundary_segments: list[dict[str, object]] = []
    zone_internal_wall_count: Counter[str] = Counter()
    zone_external_wall_count: Counter[str] = Counter()
    zone_adiabatic_wall_count: Counter[str] = Counter()
    snapped_zone_polygons_by_name = snap_zone_polygon_vertices(zone_polygons_by_name)

    for zone_name, polygons in snapped_zone_polygons_by_name.items():
        height_m = zone_heights_by_name[zone_name]
        for polygon_index, raw_polygon in enumerate(polygons, start=1):
            polygon = ensure_counterclockwise_polygon(raw_polygon)
            if not polygon:
                continue
            if len(polygon) < 3:
                continue
            floor_index_by_zone[zone_name] += 1
            roof_index_by_zone[zone_name] += 1
            floor_name = f"{zone_name}_FLOOR_{floor_index_by_zone[zone_name]:02d}"
            roof_name = f"{zone_name}_ROOF_{roof_index_by_zone[zone_name]:02d}"
            surface_rows.append(
                {
                    "surface_name": floor_name,
                    "surface_type": "Floor",
                    "construction_name": "Project external floor",
                    "zone_name": zone_name,
                    "outside_boundary_condition": "Ground",
                    "outside_boundary_condition_object": "",
                    "sun_exposure": "NoSun",
                    "wind_exposure": "NoWind",
                    "view_factor_to_ground": "",
                    **surface_vertex_payload([(x_value, y_value, 0.0) for x_value, y_value in polygon]),
                }
            )
            surface_rows.append(
                {
                    "surface_name": roof_name,
                    "surface_type": "Roof",
                    "construction_name": "Project internal floor_Reversed",
                    "zone_name": zone_name,
                    "outside_boundary_condition": "Outdoors",
                    "outside_boundary_condition_object": "",
                    "sun_exposure": "SunExposed",
                    "wind_exposure": "WindExposed",
                    "view_factor_to_ground": "",
                    **surface_vertex_payload([(x_value, y_value, height_m) for x_value, y_value in reversed(polygon)]),
                }
            )

            for edge_index, start in enumerate(polygon):
                end = polygon[(edge_index + 1) % len(polygon)]
                if segment_length(start, end) <= 1e-6:
                    continue
                boundary_segments.append(
                    {
                        "segment_id": f"{zone_name}_POLY_{polygon_index:02d}_SEG_{edge_index + 1:02d}",
                        "zone_name": zone_name,
                        "start": start,
                        "end": end,
                        "height_m": height_m,
                        **general_boundary_segment_metadata(start, end),
                    }
                )

    used_intervals_by_segment: dict[str, list[tuple[float, float]]] = {
        str(segment["segment_id"]): [] for segment in boundary_segments
    }
    adjacency_pair_rows: list[dict[str, object]] = []
    for first_index, first_segment in enumerate(boundary_segments):
        for second_segment in boundary_segments[first_index + 1 :]:
            if first_segment["zone_name"] == second_segment["zone_name"]:
                continue
            overlap = collinear_overlap_payload(
                first_segment,
                second_segment,
                min_overlap_m=min_overlap_m,
            )
            if overlap is None:
                continue
            first_segment_id = str(first_segment["segment_id"])
            second_segment_id = str(second_segment["segment_id"])
            first_interval = overlap["first_interval"]
            second_interval = overlap["second_interval"]
            if interval_already_used(used_intervals_by_segment[first_segment_id], first_interval):
                continue
            if interval_already_used(used_intervals_by_segment[second_segment_id], second_interval):
                continue
            used_intervals_by_segment[first_segment_id].append(first_interval)
            used_intervals_by_segment[second_segment_id].append(second_interval)

            first_zone_name = str(first_segment["zone_name"])
            second_zone_name = str(second_segment["zone_name"])
            wall_index_by_zone[first_zone_name] += 1
            wall_index_by_zone[second_zone_name] += 1
            first_surface_name = f"{first_zone_name}_WALL_{wall_index_by_zone[first_zone_name]:02d}"
            second_surface_name = f"{second_zone_name}_WALL_{wall_index_by_zone[second_zone_name]:02d}"
            first_start = overlap["first_start"]
            first_end = overlap["first_end"]
            second_start = overlap["second_start"]
            second_end = overlap["second_end"]
            first_height_m = float(first_segment["height_m"])
            second_height_m = float(second_segment["height_m"])
            first_row = build_wall_surface_row(
                zone_name=first_zone_name,
                surface_name=first_surface_name,
                start=first_start,
                end=first_end,
                height_m=first_height_m,
                outside_boundary_condition="Surface",
                outside_boundary_condition_object=second_surface_name,
                construction_name="",
                reverse_construction=False,
            )
            second_row = build_wall_surface_row(
                zone_name=second_zone_name,
                surface_name=second_surface_name,
                start=second_start,
                end=second_end,
                height_m=second_height_m,
                outside_boundary_condition="Surface",
                outside_boundary_condition_object=first_surface_name,
                construction_name="",
                reverse_construction=True,
            )
            surface_rows.extend([first_row, second_row])

            first_host = {
                "surface_name": first_surface_name,
                "zone_name": first_zone_name,
                "start": [first_start[0], first_start[1]],
                "end": [first_end[0], first_end[1]],
                "length_m": segment_length(first_start, first_end),
                "height_m": first_height_m,
                "boundary_condition": "Surface",
                "adjacent_zone_name": second_zone_name,
                "paired_surface_name": second_surface_name,
                "axis": str(first_segment["axis"]),
                "side": str(first_segment["side"]),
                "construction_reverse": False,
            }
            second_host = {
                "surface_name": second_surface_name,
                "zone_name": second_zone_name,
                "start": [second_start[0], second_start[1]],
                "end": [second_end[0], second_end[1]],
                "length_m": segment_length(second_start, second_end),
                "height_m": second_height_m,
                "boundary_condition": "Surface",
                "adjacent_zone_name": first_zone_name,
                "paired_surface_name": first_surface_name,
                "axis": str(second_segment["axis"]),
                "side": str(second_segment["side"]),
                "construction_reverse": True,
            }
            add_wall_host(wall_hosts_by_zone, first_host)
            add_wall_host(wall_hosts_by_zone, second_host)
            zone_internal_wall_count.update([first_zone_name, second_zone_name])
            adjacency_pair_rows.append(
                {
                    "first_zone_name": first_zone_name,
                    "second_zone_name": second_zone_name,
                    "first_surface_name": first_surface_name,
                    "second_surface_name": second_surface_name,
                    "axis": str(first_segment["axis"]),
                    "overlap_length_m": round(float(overlap["overlap_length_m"]), 3),
                }
            )

    wall_hosts_by_surface_name: dict[str, dict[str, object]] = {
        str(host["surface_name"]): host
        for hosts in wall_hosts_by_zone.values()
        for host in hosts
    }

    for segment in boundary_segments:
        segment_id = str(segment["segment_id"])
        zone_name = str(segment["zone_name"])
        height_m = float(segment["height_m"])
        start = tuple(segment["start"])  # type: ignore[arg-type]
        end = tuple(segment["end"])  # type: ignore[arg-type]
        for interval_start, interval_end in subtract_intervals((0.0, 1.0), used_intervals_by_segment[segment_id]):
            if interval_end - interval_start <= 1e-6:
                continue
            wall_index_by_zone[zone_name] += 1
            surface_name = f"{zone_name}_WALL_{wall_index_by_zone[zone_name]:02d}"
            wall_start = segment_point(start, end, interval_start)
            wall_end = segment_point(start, end, interval_end)
            is_diagonal = str(segment.get("axis", "")) == "diagonal"
            boundary_condition = (
                "Outdoors"
                if is_diagonal
                or segment_is_exterior_relative_to_outer_rect(
                    segment,
                    outer_boundary_rect,
                    exterior_gap_tolerance_m=exterior_gap_tolerance_m,
                )
                else "Adiabatic"
            )
            row = build_wall_surface_row(
                zone_name=zone_name,
                surface_name=surface_name,
                start=wall_start,
                end=wall_end,
                height_m=height_m,
                outside_boundary_condition=boundary_condition,
                outside_boundary_condition_object="",
                construction_name="",
                reverse_construction=False,
            )
            surface_rows.append(row)
            host = {
                "surface_name": surface_name,
                "zone_name": zone_name,
                "start": [wall_start[0], wall_start[1]],
                "end": [wall_end[0], wall_end[1]],
                "length_m": segment_length(wall_start, wall_end),
                "height_m": height_m,
                "boundary_condition": boundary_condition,
                "adjacent_zone_name": "",
                "paired_surface_name": "",
                "axis": str(segment["axis"]),
                "side": str(segment["side"]),
                "construction_reverse": False,
            }
            add_wall_host(wall_hosts_by_zone, host)
            wall_hosts_by_surface_name[surface_name] = host
            if boundary_condition == "Outdoors":
                zone_external_wall_count.update([zone_name])
            else:
                zone_adiabatic_wall_count.update([zone_name])

    adjacency_summary = {
        "adjacency_pair_count": len(adjacency_pair_rows),
        "adjacency_pairs": adjacency_pair_rows,
        "zone_internal_wall_count": dict(zone_internal_wall_count),
        "zone_external_wall_count": dict(zone_external_wall_count),
        "zone_adiabatic_wall_count": dict(zone_adiabatic_wall_count),
        "wall_hosts_by_surface_name": wall_hosts_by_surface_name,
        "surface_geometry_source": "zone_polygons_m_by_key",
    }
    return surface_rows, wall_hosts_by_zone, adjacency_summary


def build_zone_surfaces_with_adjacency(
    *,
    zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]],
    zone_heights_by_name: dict[str, float],
    outer_boundary_rect: tuple[float, float, float, float] | None = None,
    adjacency_tolerance_m: float = 0.45,
    min_overlap_m: float = 0.01,
    exterior_gap_tolerance_m: float = 0.50,
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, object]]], dict[str, object]]:
    surface_rows: list[dict[str, object]] = []
    wall_hosts_by_zone: dict[str, list[dict[str, object]]] = {zone_name: [] for zone_name in zone_rectangles_by_name}
    wall_index_by_zone: Counter[str] = Counter()
    boundary_segments: list[dict[str, object]] = []
    zone_internal_wall_count: Counter[str] = Counter()
    zone_external_wall_count: Counter[str] = Counter()
    zone_adiabatic_wall_count: Counter[str] = Counter()

    for zone_name, rectangles in zone_rectangles_by_name.items():
        height_m = zone_heights_by_name[zone_name]
        for rectangle_index, (min_x, min_y, max_x, max_y) in enumerate(rectangles, start=1):
            floor_name = f"{zone_name}_FLOOR_{rectangle_index:02d}"
            roof_name = f"{zone_name}_ROOF_{rectangle_index:02d}"
            surface_rows.append(
                {
                    "surface_name": floor_name,
                    "surface_type": "Floor",
                    "construction_name": "Project external floor",
                    "zone_name": zone_name,
                    "outside_boundary_condition": "Ground",
                    "outside_boundary_condition_object": "",
                    "sun_exposure": "NoSun",
                    "wind_exposure": "NoWind",
                    "view_factor_to_ground": "",
                    **wall_vertex_payload(
                        min_x,
                        min_y,
                        0.0,
                        max_x,
                        min_y,
                        0.0,
                        max_x,
                        max_y,
                        0.0,
                        min_x,
                        max_y,
                        0.0,
                    ),
                }
            )
            surface_rows.append(
                {
                    "surface_name": roof_name,
                    "surface_type": "Roof",
                    "construction_name": "Project internal floor_Reversed",
                    "zone_name": zone_name,
                    "outside_boundary_condition": "Outdoors",
                    "outside_boundary_condition_object": "",
                    "sun_exposure": "SunExposed",
                    "wind_exposure": "WindExposed",
                    "view_factor_to_ground": "",
                    **wall_vertex_payload(
                        min_x,
                        max_y,
                        height_m,
                        max_x,
                        max_y,
                        height_m,
                        max_x,
                        min_y,
                        height_m,
                        min_x,
                        min_y,
                        height_m,
                    ),
                }
            )

        for segment_index, (start, end) in enumerate(build_union_boundary_segments(rectangles), start=1):
            metadata = boundary_segment_metadata(start, end)
            if metadata is None:
                continue
            boundary_segments.append(
                {
                    "segment_id": f"{zone_name}_SEG_{segment_index:02d}",
                    "zone_name": zone_name,
                    "start": start,
                    "end": end,
                    "height_m": height_m,
                    **metadata,
                }
            )

    used_intervals_by_segment: dict[str, list[tuple[float, float]]] = {
        str(segment["segment_id"]): [] for segment in boundary_segments
    }
    adjacency_pair_rows: list[dict[str, object]] = []
    for first_index, first_segment in enumerate(boundary_segments):
        for second_segment in boundary_segments[first_index + 1 :]:
            if first_segment["zone_name"] == second_segment["zone_name"]:
                continue
            if first_segment["axis"] != second_segment["axis"]:
                continue

            if first_segment["axis"] == "horizontal":
                if {first_segment["side"], second_segment["side"]} != {"south", "north"}:
                    continue
            else:
                if {first_segment["side"], second_segment["side"]} != {"east", "west"}:
                    continue

            fixed_distance_m = abs(float(first_segment["fixed_coord"]) - float(second_segment["fixed_coord"]))
            if fixed_distance_m > adjacency_tolerance_m:
                continue

            overlap_min = max(float(first_segment["var_min"]), float(second_segment["var_min"]))
            overlap_max = min(float(first_segment["var_max"]), float(second_segment["var_max"]))
            if overlap_max - overlap_min < min_overlap_m:
                continue

            first_segment_id = str(first_segment["segment_id"])
            second_segment_id = str(second_segment["segment_id"])
            overlap_interval = (overlap_min, overlap_max)
            if interval_overlaps(used_intervals_by_segment[first_segment_id], overlap_interval):
                continue
            if interval_overlaps(used_intervals_by_segment[second_segment_id], overlap_interval):
                continue

            used_intervals_by_segment[first_segment_id].append(overlap_interval)
            used_intervals_by_segment[second_segment_id].append(overlap_interval)

            first_zone_name = str(first_segment["zone_name"])
            second_zone_name = str(second_segment["zone_name"])
            first_height_m = float(first_segment["height_m"])
            second_height_m = float(second_segment["height_m"])
            plane_coord = (float(first_segment["fixed_coord"]) + float(second_segment["fixed_coord"])) / 2.0

            wall_index_by_zone[first_zone_name] += 1
            wall_index_by_zone[second_zone_name] += 1
            first_surface_name = f"{first_zone_name}_WALL_{wall_index_by_zone[first_zone_name]:02d}"
            second_surface_name = f"{second_zone_name}_WALL_{wall_index_by_zone[second_zone_name]:02d}"

            first_start, first_end = oriented_segment_points(
                axis=str(first_segment["axis"]),
                side=str(first_segment["side"]),
                fixed_coord=plane_coord,
                var_min=overlap_min,
                var_max=overlap_max,
            )
            second_start, second_end = oriented_segment_points(
                axis=str(second_segment["axis"]),
                side=str(second_segment["side"]),
                fixed_coord=plane_coord,
                var_min=overlap_min,
                var_max=overlap_max,
            )

            first_row = build_wall_surface_row(
                zone_name=first_zone_name,
                surface_name=first_surface_name,
                start=first_start,
                end=first_end,
                height_m=first_height_m,
                outside_boundary_condition="Surface",
                outside_boundary_condition_object=second_surface_name,
                construction_name="",
                reverse_construction=False,
            )
            second_row = build_wall_surface_row(
                zone_name=second_zone_name,
                surface_name=second_surface_name,
                start=second_start,
                end=second_end,
                height_m=second_height_m,
                outside_boundary_condition="Surface",
                outside_boundary_condition_object=first_surface_name,
                construction_name="",
                reverse_construction=True,
            )
            surface_rows.extend([first_row, second_row])

            first_host = {
                "surface_name": first_surface_name,
                "zone_name": first_zone_name,
                "start": [first_start[0], first_start[1]],
                "end": [first_end[0], first_end[1]],
                "length_m": math.hypot(first_end[0] - first_start[0], first_end[1] - first_start[1]),
                "height_m": first_height_m,
                "boundary_condition": "Surface",
                "adjacent_zone_name": second_zone_name,
                "paired_surface_name": second_surface_name,
                "axis": str(first_segment["axis"]),
                "side": str(first_segment["side"]),
                "construction_reverse": False,
            }
            second_host = {
                "surface_name": second_surface_name,
                "zone_name": second_zone_name,
                "start": [second_start[0], second_start[1]],
                "end": [second_end[0], second_end[1]],
                "length_m": math.hypot(second_end[0] - second_start[0], second_end[1] - second_start[1]),
                "height_m": second_height_m,
                "boundary_condition": "Surface",
                "adjacent_zone_name": first_zone_name,
                "paired_surface_name": first_surface_name,
                "axis": str(second_segment["axis"]),
                "side": str(second_segment["side"]),
                "construction_reverse": True,
            }
            wall_hosts_by_zone[first_zone_name].append(first_host)
            wall_hosts_by_zone[second_zone_name].append(second_host)
            zone_internal_wall_count.update([first_zone_name, second_zone_name])
            adjacency_pair_rows.append(
                {
                    "first_zone_name": first_zone_name,
                    "second_zone_name": second_zone_name,
                    "first_surface_name": first_surface_name,
                    "second_surface_name": second_surface_name,
                    "axis": str(first_segment["axis"]),
                    "plane_coord_m": round(plane_coord, 3),
                    "overlap_min_m": round(overlap_min, 3),
                    "overlap_max_m": round(overlap_max, 3),
                    "overlap_length_m": round(overlap_max - overlap_min, 3),
                }
            )

    wall_hosts_by_surface_name: dict[str, dict[str, object]] = {
        str(host["surface_name"]): host
        for hosts in wall_hosts_by_zone.values()
        for host in hosts
    }

    for segment in boundary_segments:
        segment_id = str(segment["segment_id"])
        remaining_intervals = subtract_intervals(
            (float(segment["var_min"]), float(segment["var_max"])),
            used_intervals_by_segment[segment_id],
        )
        zone_name = str(segment["zone_name"])
        height_m = float(segment["height_m"])
        for interval_min, interval_max in remaining_intervals:
            if interval_max - interval_min <= 1e-6:
                continue
            wall_index_by_zone[zone_name] += 1
            surface_name = f"{zone_name}_WALL_{wall_index_by_zone[zone_name]:02d}"
            start, end = oriented_segment_points(
                axis=str(segment["axis"]),
                side=str(segment["side"]),
                fixed_coord=float(segment["fixed_coord"]),
                var_min=interval_min,
                var_max=interval_max,
            )
            row = build_wall_surface_row(
                zone_name=zone_name,
                surface_name=surface_name,
                start=start,
                end=end,
                height_m=height_m,
                outside_boundary_condition=(
                    "Outdoors"
                    if segment_is_exterior_relative_to_outer_rect(
                        segment,
                        outer_boundary_rect,
                        exterior_gap_tolerance_m=exterior_gap_tolerance_m,
                    )
                    else "Adiabatic"
                ),
                outside_boundary_condition_object="",
                construction_name="",
                reverse_construction=False,
            )
            surface_rows.append(row)
            boundary_condition = str(row["outside_boundary_condition"])
            host = {
                "surface_name": surface_name,
                "zone_name": zone_name,
                "start": [start[0], start[1]],
                "end": [end[0], end[1]],
                "length_m": math.hypot(end[0] - start[0], end[1] - start[1]),
                "height_m": height_m,
                "boundary_condition": boundary_condition,
                "adjacent_zone_name": "",
                "paired_surface_name": "",
                "axis": str(segment["axis"]),
                "side": str(segment["side"]),
                "construction_reverse": False,
            }
            wall_hosts_by_zone[zone_name].append(host)
            wall_hosts_by_surface_name[surface_name] = host
            if boundary_condition == "Outdoors":
                zone_external_wall_count.update([zone_name])
            else:
                zone_adiabatic_wall_count.update([zone_name])

    adjacency_summary = {
        "adjacency_pair_count": len(adjacency_pair_rows),
        "adjacency_pairs": adjacency_pair_rows,
        "zone_internal_wall_count": dict(zone_internal_wall_count),
        "zone_external_wall_count": dict(zone_external_wall_count),
        "zone_adiabatic_wall_count": dict(zone_adiabatic_wall_count),
        "wall_hosts_by_surface_name": wall_hosts_by_surface_name,
    }
    return surface_rows, wall_hosts_by_zone, adjacency_summary


def default_surface_zone_name(zone_key: str) -> str:
    return f"APARTMENT_A_{str(zone_key).strip()}"


def normalize_zone_rectangles_payload(
    raw_payload: object,
) -> dict[str, list[tuple[float, float, float, float]]]:
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
            normalized[zone_key] = normalized_rectangles
    return normalized


def normalize_zone_polygons_payload(
    raw_payload: object,
) -> dict[str, list[list[tuple[float, float]]]]:
    normalized: dict[str, list[list[tuple[float, float]]]] = {}
    if not isinstance(raw_payload, dict):
        return normalized
    for zone_key, polygons in raw_payload.items():
        if not isinstance(zone_key, str) or not isinstance(polygons, list):
            continue
        normalized_polygons: list[list[tuple[float, float]]] = []
        for polygon in polygons:
            if not isinstance(polygon, list):
                continue
            points: list[tuple[float, float]] = []
            for point in polygon:
                if isinstance(point, list) and len(point) >= 2:
                    points.append((float(point[0]), float(point[1])))
            cleaned_polygon = ensure_counterclockwise_polygon(points)
            if cleaned_polygon:
                normalized_polygons.append(cleaned_polygon)
        if normalized_polygons:
            normalized[zone_key] = normalized_polygons
    return normalized


def surface_summary(surface_rows: list[dict[str, object]]) -> dict[str, object]:
    type_counts = Counter(str(row.get("surface_type", "") or "unknown") for row in surface_rows)
    return {
        "surface_row_count": len(surface_rows),
        "surface_type_counts": dict(sorted(type_counts.items())),
    }


def build_surface_artifacts(
    *,
    geometry_payload: dict[str, object],
) -> dict[str, object]:
    zone_rectangles_m_by_key = normalize_zone_rectangles_payload(
        geometry_payload.get("zone_rectangles_m_by_key", {})
    )
    zone_polygons_m_by_key = normalize_zone_polygons_payload(
        geometry_payload.get("zone_polygons_m_by_key", {})
    )
    if not zone_rectangles_m_by_key:
        raise WorkspaceRuleError("Geometry payload is missing zone_rectangles_m_by_key for surface building.")

    zone_output_name_by_key = {
        str(zone_key): str(zone_name)
        for zone_key, zone_name in dict(geometry_payload.get("zone_output_name_by_key", {})).items()
        if str(zone_key).strip() and str(zone_name).strip()
    }
    zone_heights_by_key = {
        str(zone_key): float(height_m)
        for zone_key, height_m in dict(geometry_payload.get("zone_height_m_by_key", {})).items()
        if str(zone_key).strip()
    }
    outer_block_rect_raw = list(geometry_payload.get("outer_block_rect_m", []))
    outer_block_rect = None
    if len(outer_block_rect_raw) >= 4:
        outer_block_rect = normalize_rect(tuple(float(value) for value in outer_block_rect_raw[:4]))

    zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]] = {}
    zone_heights_by_name: dict[str, float] = {}
    zone_key_by_name: dict[str, str] = {}
    for zone_key, rectangles in sorted(zone_rectangles_m_by_key.items()):
        zone_name = zone_output_name_by_key.get(zone_key, default_surface_zone_name(zone_key))
        zone_rectangles_by_name[zone_name] = rectangles
        if zone_key not in zone_heights_by_key:
            raise WorkspaceRuleError(f"Geometry payload is missing human-provided height for zone: {zone_key}")
        zone_height_m = float(zone_heights_by_key[zone_key])
        if zone_height_m <= 0.0:
            raise WorkspaceRuleError(f"Zone height must be greater than 0 for zone: {zone_key}")
        zone_heights_by_name[zone_name] = zone_height_m
        zone_key_by_name[zone_name] = zone_key

    zone_polygons_by_name: dict[str, list[list[tuple[float, float]]]] = {}
    if zone_polygons_m_by_key:
        for zone_key, rectangles in sorted(zone_rectangles_m_by_key.items()):
            zone_name = zone_output_name_by_key.get(zone_key, default_surface_zone_name(zone_key))
            polygons = zone_polygons_m_by_key.get(zone_key)
            if polygons:
                zone_polygons_by_name[zone_name] = polygons
            else:
                zone_polygons_by_name[zone_name] = [rectangle_to_polygon(rectangle) for rectangle in rectangles]

    if zone_polygons_by_name:
        surface_rows, wall_hosts_by_zone, adjacency_summary = build_zone_surfaces_from_polygons_with_adjacency(
            zone_polygons_by_name=zone_polygons_by_name,
            zone_heights_by_name=zone_heights_by_name,
            outer_boundary_rect=outer_block_rect,
        )
    else:
        surface_rows, wall_hosts_by_zone, adjacency_summary = build_zone_surfaces_with_adjacency(
            zone_rectangles_by_name=zone_rectangles_by_name,
            zone_heights_by_name=zone_heights_by_name,
            outer_boundary_rect=outer_block_rect,
        )

    adjacency_summary = {
        **adjacency_summary,
        "geometry_mode": geometry_payload.get("geometry_mode"),
        "geometry_source": geometry_payload.get("geometry_source"),
        "outer_block_rect_m": list(outer_block_rect) if outer_block_rect is not None else None,
        "zone_key_by_name": zone_key_by_name,
        "zone_heights_by_name": zone_heights_by_name,
        "surface_summary": surface_summary(surface_rows),
    }
    return {
        "surface_rows": surface_rows,
        "wall_hosts_by_zone": wall_hosts_by_zone,
        "zone_rectangles_by_name": zone_rectangles_by_name,
        "zone_polygons_by_name": zone_polygons_by_name,
        "zone_heights_by_name": zone_heights_by_name,
        "adjacency_summary": adjacency_summary,
    }


def write_surface_outputs(
    surface_artifacts: dict[str, object],
    *,
    output_dir: Path | str | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    resolved_output_dir = GUARD.resolve(output_dir or _resolve_default_output_dir(path_resolver.resolve_project_id(project_id)))
    if project_id is not None:
        path_resolver.assert_output_in_project_scope(path_resolver.resolve_project_id(project_id), resolved_output_dir)
    output_paths = {
        "surface_rows": resolved_output_dir / "surface_rows.json",
        "adjacency_summary": resolved_output_dir / "adjacency_summary.json",
    }

    GUARD.write_json(
        output_paths["surface_rows"],
        list(surface_artifacts.get("surface_rows", [])),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    GUARD.write_json(
        output_paths["adjacency_summary"],
        dict(surface_artifacts.get("adjacency_summary", {})),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    return {key: workspace_path(path) for key, path in output_paths.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build intermediate surface rows from geometry payload.")
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--geometry-payload",
        default=None,
        help="Path to geometry_payload.json. If omitted, resolves from 5_output/<project_id>/intermediate/geometry.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for surface artifacts. If omitted, defaults to 5_output/<project_id>/intermediate/surfaces.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)
    geometry_payload_path = args.geometry_payload or _resolve_default_geometry_payload(project_id)
    output_dir = args.output_dir or _resolve_default_output_dir(project_id)

    geometry_payload = load_json_object(geometry_payload_path)
    surface_artifacts = build_surface_artifacts(geometry_payload=geometry_payload)
    written_paths = write_surface_outputs(surface_artifacts, output_dir=output_dir, project_id=project_id)
    summary = dict(surface_artifacts.get("adjacency_summary", {}))
    surface_counts = dict(summary.get("surface_summary", {}))

    print("SURFACE_BUILDER_OK")
    print(f"Geometry payload: {geometry_payload_path}")
    print(f"Surface rows: {written_paths['surface_rows']}")
    print(f"Adjacency summary: {written_paths['adjacency_summary']}")
    print(f"Surface row count: {surface_counts.get('surface_row_count')}")
    print(f"Surface type counts: {surface_counts.get('surface_type_counts')}")
    print(f"Adjacency pair count: {summary.get('adjacency_pair_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
