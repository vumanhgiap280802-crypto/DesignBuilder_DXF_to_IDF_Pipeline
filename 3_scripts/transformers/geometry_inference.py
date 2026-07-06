#!/usr/bin/env python3
"""
Infer Apartment A geometry from mapping context and geometry policy.

This transformer only works from parser/context outputs. It does not parse raw DXF,
and does not build final CSV/IDF outputs.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import deque
from itertools import combinations
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.dxf_raw_parser import Record, numeric_group_code_value  # noqa: E402
from schema_tools.schema_workbench import parse_dxf_extract_file  # noqa: E402
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils import path_resolver  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_MAPPING_PAYLOAD = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "mapping_payload.json"
DEFAULT_POLICY_PATH = ROOT / "2_config" / "apartment_a_geometry_policy.json"
DEFAULT_OUTPUT_DIR = Path("5_output") / "<project_id>" / "intermediate" / "geometry"
DEFAULT_ZONE_OUTPUT_PREFIX = "APARTMENT_A_"


def workspace_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def normalize_relative_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/")


def _resolve_default_mapping_payload(project_id: str) -> Path:
    resolved = path_resolver.resolve_output_file_for_read(project_id, "intermediate/mapping", "mapping_payload.json")
    if resolved is None:
        raise WorkspaceRuleError(f"No mapping payload found for project '{project_id}'.")
    return resolved


def _resolve_default_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/geometry")


def load_json_object(path: Path | str) -> dict[str, object]:
    resolved_path = GUARD.assert_read_path(path)
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkspaceRuleError(f"Invalid JSON object: {workspace_path(resolved_path)}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"JSON root must be an object: {workspace_path(resolved_path)}")
    return payload


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
        "LGIA": "LOGIA",
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


def aliased_source_zone_name(source_zone_name: str, geometry_policy: dict[str, object]) -> str:
    aliases = {
        canonical_zone_key(str(source)): str(target).strip()
        for source, target in dict(geometry_policy.get("zone_name_aliases", {})).items()
        if str(source).strip() and str(target).strip()
    }
    alias = aliases.get(canonical_zone_key(source_zone_name), "")
    return alias or source_zone_name


def zone_merge_source_to_target_map(geometry_policy: dict[str, object]) -> dict[str, str]:
    source_to_target: dict[str, str] = {}
    for group in list(geometry_policy.get("zone_merge_groups", [])):
        if not isinstance(group, dict):
            continue
        target_zone_key = canonical_zone_key(str(group.get("target_zone_key", "") or ""))
        if not target_zone_key:
            continue
        for source_zone_key in list(group.get("source_zone_keys", [])):
            normalized_source = canonical_zone_key(str(source_zone_key))
            if normalized_source:
                source_to_target[normalized_source] = target_zone_key
    return source_to_target


def zone_merge_target_names(geometry_policy: dict[str, object]) -> dict[str, str]:
    target_names: dict[str, str] = {}
    for group in list(geometry_policy.get("zone_merge_groups", [])):
        if not isinstance(group, dict):
            continue
        target_zone_key = canonical_zone_key(str(group.get("target_zone_key", "") or ""))
        if not target_zone_key:
            continue
        target_zone_name = str(group.get("target_zone_name", "") or "").strip()
        target_names[target_zone_key] = target_zone_name or default_source_zone_name(target_zone_key)
    return target_names


def normalize_rect(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = rect
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def interval_overlap_length(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(max(a_start, a_end), max(b_start, b_end)) - max(min(a_start, a_end), min(b_start, b_end)))


def rectangles_area_m2(rectangles: list[tuple[float, float, float, float]]) -> float:
    return sum(max(0.0, (x2 - x1) * (y2 - y1)) for x1, y1, x2, y2 in rectangles)


def rectangle_grid_cells(
    outer_rectangle: tuple[float, float, float, float],
    filled_rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    outer_min_x, outer_min_y, outer_max_x, outer_max_y = normalize_rect(outer_rectangle)
    xs = sorted(
        {
            round(outer_min_x, 6),
            round(outer_max_x, 6),
            *[round(value, 6) for rect in filled_rectangles for value in (rect[0], rect[2])],
        }
    )
    ys = sorted(
        {
            round(outer_min_y, 6),
            round(outer_max_y, 6),
            *[round(value, 6) for rect in filled_rectangles for value in (rect[1], rect[3])],
        }
    )

    cells: list[tuple[float, float, float, float]] = []
    for x_index in range(len(xs) - 1):
        x1 = xs[x_index]
        x2 = xs[x_index + 1]
        center_x = (x1 + x2) / 2.0
        for y_index in range(len(ys) - 1):
            y1 = ys[y_index]
            y2 = ys[y_index + 1]
            center_y = (y1 + y2) / 2.0
            if any(
                rect[0] < center_x < rect[2] and rect[1] < center_y < rect[3]
                for rect in filled_rectangles
            ):
                continue
            if outer_min_x < center_x < outer_max_x and outer_min_y < center_y < outer_max_y:
                cells.append((x1, y1, x2, y2))
    return cells


def merge_axis_aligned_rectangles(
    rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    if not rectangles:
        return []

    normalized = [normalize_rect(rectangle) for rectangle in rectangles]
    horizontal_groups: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for x1, y1, x2, y2 in normalized:
        horizontal_groups.setdefault((round(y1, 6), round(y2, 6)), []).append((x1, x2))

    horizontally_merged: list[tuple[float, float, float, float]] = []
    for (y1, y2), spans in horizontal_groups.items():
        spans = sorted(spans, key=lambda item: item[0])
        current_start, current_end = spans[0]
        for start, end in spans[1:]:
            if abs(current_end - start) <= 1e-6:
                current_end = end
            else:
                horizontally_merged.append((current_start, y1, current_end, y2))
                current_start, current_end = start, end
        horizontally_merged.append((current_start, y1, current_end, y2))

    vertical_groups: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for x1, y1, x2, y2 in horizontally_merged:
        vertical_groups.setdefault((round(x1, 6), round(x2, 6)), []).append((y1, y2))

    vertically_merged: list[tuple[float, float, float, float]] = []
    for (x1, x2), spans in vertical_groups.items():
        spans = sorted(spans, key=lambda item: item[0])
        current_start, current_end = spans[0]
        for start, end in spans[1:]:
            if abs(current_end - start) <= 1e-6:
                current_end = end
            else:
                vertically_merged.append((x1, current_start, x2, current_end))
                current_start, current_end = start, end
        vertically_merged.append((x1, current_start, x2, current_end))

    return vertically_merged


def rectangle_area_m2(rectangle: tuple[float, float, float, float]) -> float:
    min_x, min_y, max_x, max_y = normalize_rect(rectangle)
    return max(0.0, (max_x - min_x) * (max_y - min_y))


def rectangle_center_xy(rectangle: tuple[float, float, float, float]) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = normalize_rect(rectangle)
    return (min_x + max_x) / 2.0, (min_y + max_y) / 2.0


def point_in_rectangle(
    point_xy: tuple[float, float],
    rectangle: tuple[float, float, float, float],
    *,
    tolerance_m: float = 1e-9,
) -> bool:
    point_x, point_y = point_xy
    min_x, min_y, max_x, max_y = normalize_rect(rectangle)
    return (
        min_x - tolerance_m <= point_x <= max_x + tolerance_m
        and min_y - tolerance_m <= point_y <= max_y + tolerance_m
    )


def shared_boundary_length_m(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    tolerance_m: float = 1e-5,
) -> float:
    first_min_x, first_min_y, first_max_x, first_max_y = normalize_rect(first)
    second_min_x, second_min_y, second_max_x, second_max_y = normalize_rect(second)

    if abs(first_max_x - second_min_x) <= tolerance_m or abs(second_max_x - first_min_x) <= tolerance_m:
        return interval_overlap_length(first_min_y, first_max_y, second_min_y, second_max_y)
    if abs(first_max_y - second_min_y) <= tolerance_m or abs(second_max_y - first_min_y) <= tolerance_m:
        return interval_overlap_length(first_min_x, first_max_x, second_min_x, second_max_x)
    return 0.0


def build_rectangle_adjacency_map(
    rectangles: list[tuple[float, float, float, float]],
) -> dict[int, list[int]]:
    adjacency: dict[int, list[int]] = {index: [] for index in range(len(rectangles))}
    for first_index, first in enumerate(rectangles):
        for second_index in range(first_index + 1, len(rectangles)):
            if shared_boundary_length_m(first, rectangles[second_index]) <= 1e-5:
                continue
            adjacency[first_index].append(second_index)
            adjacency[second_index].append(first_index)
    return adjacency


def find_seed_rectangle_index(
    rectangles: list[tuple[float, float, float, float]],
    anchor_xy_m: tuple[float, float],
) -> int | None:
    for index, rectangle in enumerate(rectangles):
        if point_in_rectangle(anchor_xy_m, rectangle):
            return index
    if not rectangles:
        return None
    anchor_x, anchor_y = anchor_xy_m
    return min(
        range(len(rectangles)),
        key=lambda index: math.hypot(
            rectangle_center_xy(rectangles[index])[0] - anchor_x,
            rectangle_center_xy(rectangles[index])[1] - anchor_y,
        ),
    )


def grow_zone_cells_to_target_area(
    *,
    free_cells_local_m: list[tuple[float, float, float, float]],
    anchor_xy_m: tuple[float, float] | None,
    target_area_m2: float,
    max_iterations: int = 256,
) -> list[tuple[float, float, float, float]]:
    normalized_cells = [normalize_rect(cell) for cell in free_cells_local_m]
    if not normalized_cells or anchor_xy_m is None or target_area_m2 <= 1e-6:
        return []

    adjacency = build_rectangle_adjacency_map(normalized_cells)
    start_index = find_seed_rectangle_index(normalized_cells, anchor_xy_m)
    if start_index is None:
        return []

    selected_indices: set[int] = {start_index}

    def current_area(indices: set[int]) -> float:
        return sum(rectangle_area_m2(normalized_cells[index]) for index in indices)

    def current_frontier(indices: set[int]) -> set[int]:
        frontier: set[int] = set()
        for index in indices:
            for neighbor_index in adjacency[index]:
                if neighbor_index not in indices:
                    frontier.add(neighbor_index)
        return frontier

    def removable_indices(indices: set[int]) -> list[int]:
        removable: list[int] = []
        for index in indices:
            if index == start_index:
                continue
            remaining = [item for item in indices if item != index]
            if not remaining:
                continue
            pending = deque([remaining[0]])
            seen = {remaining[0]}
            while pending:
                current_index = pending.popleft()
                for neighbor_index in adjacency[current_index]:
                    if neighbor_index in indices and neighbor_index != index and neighbor_index not in seen:
                        seen.add(neighbor_index)
                        pending.append(neighbor_index)
            if len(seen) == len(remaining):
                removable.append(index)
        return removable

    def candidate_score(new_area_m2: float, rectangle_index: int, shared_length_m: float) -> tuple[float, int, float, float, float]:
        center_x, center_y = rectangle_center_xy(normalized_cells[rectangle_index])
        anchor_x, anchor_y = anchor_xy_m
        return (
            abs(new_area_m2 - target_area_m2),
            0 if new_area_m2 <= target_area_m2 else 1,
            math.hypot(center_x - anchor_x, center_y - anchor_y),
            -shared_length_m,
            -rectangle_area_m2(normalized_cells[rectangle_index]),
        )

    for _iteration in range(max_iterations):
        selected_area_m2 = current_area(selected_indices)
        selected_error_m2 = abs(selected_area_m2 - target_area_m2)
        best_move: tuple[tuple[float, int, float, float, float], str, int] | None = None

        for rectangle_index in current_frontier(selected_indices):
            shared_length_m = max(
                (
                    shared_boundary_length_m(normalized_cells[rectangle_index], normalized_cells[selected_index])
                    for selected_index in selected_indices
                ),
                default=0.0,
            )
            score = candidate_score(
                selected_area_m2 + rectangle_area_m2(normalized_cells[rectangle_index]),
                rectangle_index,
                shared_length_m,
            )
            if best_move is None or score < best_move[0]:
                best_move = (score, "add", rectangle_index)

        for rectangle_index in removable_indices(selected_indices):
            shared_length_m = max(
                (
                    shared_boundary_length_m(normalized_cells[rectangle_index], normalized_cells[selected_index])
                    for selected_index in selected_indices
                    if selected_index != rectangle_index
                ),
                default=0.0,
            )
            score = candidate_score(
                selected_area_m2 - rectangle_area_m2(normalized_cells[rectangle_index]),
                rectangle_index,
                shared_length_m,
            )
            if best_move is None or score < best_move[0]:
                best_move = (score, "remove", rectangle_index)

        if best_move is None or best_move[0][0] >= selected_error_m2 - 1e-9:
            break

        _score, action, rectangle_index = best_move
        if action == "add":
            selected_indices.add(rectangle_index)
        else:
            selected_indices.remove(rectangle_index)

    return [normalized_cells[index] for index in sorted(selected_indices)]


def rectangles_overlap_area_m2(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    first_min_x, first_min_y, first_max_x, first_max_y = normalize_rect(first)
    second_min_x, second_min_y, second_max_x, second_max_y = normalize_rect(second)
    overlap_width_m = max(0.0, min(first_max_x, second_max_x) - max(first_min_x, second_min_x))
    overlap_height_m = max(0.0, min(first_max_y, second_max_y) - max(first_min_y, second_min_y))
    return overlap_width_m * overlap_height_m


def touches_outer_boundary(
    rectangle: tuple[float, float, float, float],
    outer_rectangle: tuple[float, float, float, float],
    *,
    tolerance_m: float = 1e-9,
) -> bool:
    rect_min_x, rect_min_y, rect_max_x, rect_max_y = normalize_rect(rectangle)
    outer_min_x, outer_min_y, outer_max_x, outer_max_y = normalize_rect(outer_rectangle)
    return (
        abs(rect_min_x - outer_min_x) <= tolerance_m
        or abs(rect_min_y - outer_min_y) <= tolerance_m
        or abs(rect_max_x - outer_max_x) <= tolerance_m
        or abs(rect_max_y - outer_max_y) <= tolerance_m
    )


def rounded_rectangle(
    rectangle: tuple[float, float, float, float],
    *,
    digits: int = 3,
) -> tuple[float, float, float, float]:
    return normalize_rect(tuple(round(value, digits) for value in normalize_rect(rectangle)))


def filter_growth_cells_for_air_zone(
    free_cells_local_m: list[tuple[float, float, float, float]],
    outer_rectangle_local_m: tuple[float, float, float, float],
    *,
    min_outer_sliver_short_edge_m: float = 0.25,
) -> list[tuple[float, float, float, float]]:
    filtered_cells: list[tuple[float, float, float, float]] = []
    for rectangle in free_cells_local_m:
        normalized_rectangle = normalize_rect(rectangle)
        width_m = normalized_rectangle[2] - normalized_rectangle[0]
        height_m = normalized_rectangle[3] - normalized_rectangle[1]
        if (
            touches_outer_boundary(normalized_rectangle, outer_rectangle_local_m)
            and min(width_m, height_m) < min_outer_sliver_short_edge_m
        ):
            continue
        filtered_cells.append(normalized_rectangle)
    return filtered_cells


def adjust_rectangle_with_fixed_edge(
    rectangle: tuple[float, float, float, float],
    *,
    target_area_m2: float,
    movable_side: str,
    minimum_dimension_m: float = 0.30,
) -> tuple[float, float, float, float] | None:
    x1, y1, x2, y2 = normalize_rect(rectangle)
    width_m = x2 - x1
    height_m = y2 - y1
    if target_area_m2 <= 1e-6 or width_m <= 1e-9 or height_m <= 1e-9:
        return None

    candidate: tuple[float, float, float, float] | None = None
    if movable_side == "left":
        target_width_m = target_area_m2 / height_m
        candidate = (x2 - target_width_m, y1, x2, y2)
    elif movable_side == "right":
        target_width_m = target_area_m2 / height_m
        candidate = (x1, y1, x1 + target_width_m, y2)
    elif movable_side == "bottom":
        target_height_m = target_area_m2 / width_m
        candidate = (x1, y2 - target_height_m, x2, y2)
    elif movable_side == "top":
        target_height_m = target_area_m2 / width_m
        candidate = (x1, y1, x2, y1 + target_height_m)

    if candidate is None:
        return None
    candidate = normalize_rect(candidate)
    candidate_width_m = candidate[2] - candidate[0]
    candidate_height_m = candidate[3] - candidate[1]
    if candidate_width_m < minimum_dimension_m or candidate_height_m < minimum_dimension_m:
        return None
    return candidate


def adjust_rectangle_to_target_area(
    *,
    rectangle: tuple[float, float, float, float],
    target_area_m2: float,
    anchor_xy_m: tuple[float, float] | None,
    outer_rectangle_local_m: tuple[float, float, float, float],
    blocked_rectangles_local_m: list[tuple[float, float, float, float]],
    minimum_dimension_m: float = 0.30,
    overlap_tolerance_m2: float = 1e-6,
) -> tuple[float, float, float, float]:
    normalized_rectangle = normalize_rect(rectangle)
    if target_area_m2 <= 1e-6:
        return normalized_rectangle

    x1, y1, x2, y2 = normalized_rectangle
    width_m = x2 - x1
    height_m = y2 - y1
    outer_min_x, outer_min_y, outer_max_x, outer_max_y = normalize_rect(outer_rectangle_local_m)

    candidates: list[tuple[float, float, float, float, str]] = []
    if width_m > minimum_dimension_m:
        target_height_m = target_area_m2 / width_m
        if target_height_m >= minimum_dimension_m:
            candidates.append((x1, y1, x2, y1 + target_height_m, "keep_bottom"))
            candidates.append((x1, y2 - target_height_m, x2, y2, "keep_top"))
    if height_m > minimum_dimension_m:
        target_width_m = target_area_m2 / height_m
        if target_width_m >= minimum_dimension_m:
            candidates.append((x1, y1, x1 + target_width_m, y2, "keep_left"))
            candidates.append((x2 - target_width_m, y1, x2, y2, "keep_right"))

    best_candidate: tuple[tuple[float, float, float, float, float], tuple[float, float, float, float]] | None = None
    for candidate_x1, candidate_y1, candidate_x2, candidate_y2, _label in candidates:
        candidate = normalize_rect((candidate_x1, candidate_y1, candidate_x2, candidate_y2))
        candidate_width_m = candidate[2] - candidate[0]
        candidate_height_m = candidate[3] - candidate[1]
        if candidate_width_m < minimum_dimension_m or candidate_height_m < minimum_dimension_m:
            continue
        if (
            candidate[0] < outer_min_x - 1e-9
            or candidate[1] < outer_min_y - 1e-9
            or candidate[2] > outer_max_x + 1e-9
            or candidate[3] > outer_max_y + 1e-9
        ):
            continue
        if anchor_xy_m is not None and not point_in_rectangle(anchor_xy_m, candidate, tolerance_m=1e-9):
            continue
        if any(
            rectangles_overlap_area_m2(candidate, blocked_rectangle) > overlap_tolerance_m2
            for blocked_rectangle in blocked_rectangles_local_m
        ):
            continue
        candidate_area_m2 = rectangle_area_m2(candidate)
        score = (
            round(
                abs(candidate[0] - x1)
                + abs(candidate[1] - y1)
                + abs(candidate[2] - x2)
                + abs(candidate[3] - y2),
                6,
            ),
            round(abs(candidate_area_m2 - target_area_m2), 6),
            round(abs(candidate_width_m - width_m) + abs(candidate_height_m - height_m), 6),
            round(-min(candidate_width_m, candidate_height_m), 6),
            round(-max(candidate_width_m, candidate_height_m), 6),
        )
        if best_candidate is None or score < best_candidate[0]:
            best_candidate = (score, candidate)

    return best_candidate[1] if best_candidate is not None else normalized_rectangle


def correct_named_zone_rectangles_to_target_area(
    *,
    room_rectangles_local: dict[str, tuple[float, float, float, float]],
    zone_target_area_by_key: dict[str, float],
    zone_anchor_local_by_key: dict[str, tuple[float, float]],
    outer_rectangle_local_m: tuple[float, float, float, float],
) -> dict[str, tuple[float, float, float, float]]:
    corrected_rectangles = {
        zone_key: rounded_rectangle(rectangle)
        for zone_key, rectangle in room_rectangles_local.items()
    }
    preferred_movable_sides_by_zone = {
        "PN_02": ["right"],
        "WC_01": ["right"],
        "PN_01": ["right"],
        "WC_02": ["left"],
        "LOGIA": ["left"],
    }

    for zone_key in sorted(corrected_rectangles):
        target_area_m2 = zone_target_area_by_key.get(zone_key, 0.0)
        if target_area_m2 <= 1e-6:
            continue
        preferred_movable_sides = preferred_movable_sides_by_zone.get(zone_key, [])
        preferred_candidate: tuple[float, float, float, float] | None = None
        for movable_side in preferred_movable_sides:
            candidate = adjust_rectangle_with_fixed_edge(
                corrected_rectangles[zone_key],
                target_area_m2=target_area_m2,
                movable_side=movable_side,
            )
            if candidate is None:
                continue
            if zone_anchor_local_by_key.get(zone_key) is not None and not point_in_rectangle(
                zone_anchor_local_by_key[zone_key],
                candidate,
                tolerance_m=1e-9,
            ):
                continue
            preferred_candidate = candidate
            break
        if preferred_candidate is not None:
            corrected_rectangles[zone_key] = rounded_rectangle(preferred_candidate)
            continue
        blocked_rectangles = [
            rectangle
            for other_zone_key, rectangle in corrected_rectangles.items()
            if other_zone_key != zone_key
        ]
        corrected_rectangles[zone_key] = rounded_rectangle(
            adjust_rectangle_to_target_area(
                rectangle=corrected_rectangles[zone_key],
                target_area_m2=target_area_m2,
                anchor_xy_m=zone_anchor_local_by_key.get(zone_key),
                outer_rectangle_local_m=outer_rectangle_local_m,
                blocked_rectangles_local_m=blocked_rectangles,
            )
        )

    return corrected_rectangles


def connected_indices(
    *,
    index_set: set[int],
    adjacency: dict[int, list[int]],
) -> set[int]:
    if not index_set:
        return set()
    start_index = next(iter(index_set))
    seen = {start_index}
    pending = deque([start_index])
    while pending:
        current_index = pending.popleft()
        for neighbor_index in adjacency[current_index]:
            if neighbor_index in index_set and neighbor_index not in seen:
                seen.add(neighbor_index)
                pending.append(neighbor_index)
    return seen


def every_component_touches_boundary(
    *,
    index_set: set[int],
    adjacency: dict[int, list[int]],
    boundary_indices: set[int],
) -> bool:
    remaining_indices = set(index_set)
    while remaining_indices:
        start_index = next(iter(remaining_indices))
        pending = deque([start_index])
        component_indices = {start_index}
        while pending:
            current_index = pending.popleft()
            for neighbor_index in adjacency[current_index]:
                if neighbor_index in remaining_indices and neighbor_index not in component_indices:
                    component_indices.add(neighbor_index)
                    pending.append(neighbor_index)
        if not (component_indices & boundary_indices):
            return False
        remaining_indices -= component_indices
    return True


def zone_contact_graph(
    zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]],
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {zone_name: set() for zone_name in zone_rectangles_by_name}
    zone_names = sorted(zone_rectangles_by_name)
    for first_index, first_zone_name in enumerate(zone_names):
        for second_zone_name in zone_names[first_index + 1 :]:
            shared_length_m = sum(
                shared_boundary_length_m(first_rectangle, second_rectangle)
                for first_rectangle in zone_rectangles_by_name[first_zone_name]
                for second_rectangle in zone_rectangles_by_name[second_zone_name]
            )
            if shared_length_m <= 1e-6:
                continue
            graph[first_zone_name].add(second_zone_name)
            graph[second_zone_name].add(first_zone_name)
    return graph


def graph_is_connected(graph: dict[str, set[str]]) -> bool:
    if not graph:
        return True
    start_node = next(iter(graph))
    seen = {start_node}
    pending = deque([start_node])
    while pending:
        current_node = pending.popleft()
        for neighbor_node in graph[current_node]:
            if neighbor_node not in seen:
                seen.add(neighbor_node)
                pending.append(neighbor_node)
    return len(seen) == len(graph)


def summarize_single_block_partition(
    *,
    outer_rectangle_local_m: tuple[float, float, float, float],
    zone_rectangles_local_m_by_key: dict[str, list[tuple[float, float, float, float]]],
) -> dict[str, object]:
    normalized_zone_rectangles = {
        zone_key: [normalize_rect(rectangle) for rectangle in rectangles]
        for zone_key, rectangles in zone_rectangles_local_m_by_key.items()
        if rectangles
    }
    all_zone_rectangles = [
        rectangle
        for rectangles in normalized_zone_rectangles.values()
        for rectangle in rectangles
    ]
    unallocated_cells_local_m = rectangle_grid_cells(
        outer_rectangle_local_m,
        all_zone_rectangles,
    )
    zone_graph = zone_contact_graph(normalized_zone_rectangles)
    return {
        "outer_block_area_m2": round(rectangles_area_m2([outer_rectangle_local_m]), 3),
        "allocated_area_m2": round(rectangles_area_m2(all_zone_rectangles), 3),
        "unallocated_area_m2": round(rectangles_area_m2(unallocated_cells_local_m), 6),
        "unallocated_cell_count": len(unallocated_cells_local_m),
        "zone_graph_connected": graph_is_connected(zone_graph),
        "zone_contact_graph": {
            zone_key: sorted(neighbor_zone_keys)
            for zone_key, neighbor_zone_keys in sorted(zone_graph.items())
        },
    }


def select_partition_cells_without_holes(
    *,
    free_cells_local_m: list[tuple[float, float, float, float]],
    target_area_m2: float,
    anchor_xy_m: tuple[float, float] | None,
    outer_rectangle_local_m: tuple[float, float, float, float],
    fixed_zone_rectangles_by_name: dict[str, list[tuple[float, float, float, float]]],
    max_cell_count: int = 20,
    area_tolerance_m2: float = 0.05,
) -> list[tuple[float, float, float, float]]:
    normalized_cells = [normalize_rect(cell) for cell in free_cells_local_m]
    if not normalized_cells or anchor_xy_m is None or len(normalized_cells) > max_cell_count:
        return []

    adjacency = build_rectangle_adjacency_map(normalized_cells)
    seed_index = find_seed_rectangle_index(normalized_cells, anchor_xy_m)
    if seed_index is None:
        return []

    boundary_indices = {
        index
        for index, rectangle in enumerate(normalized_cells)
        if touches_outer_boundary(rectangle, outer_rectangle_local_m)
    }
    cell_areas_m2 = [rectangle_area_m2(rectangle) for rectangle in normalized_cells]
    all_indices = set(range(len(normalized_cells)))

    best_selection: tuple[
        tuple[float, int, float, float],
        list[tuple[float, float, float, float]],
    ] | None = None

    for mask in range(1 << len(normalized_cells)):
        if not ((mask >> seed_index) & 1):
            continue
        selected_indices = {index for index in range(len(normalized_cells)) if (mask >> index) & 1}
        selected_area_m2 = sum(cell_areas_m2[index] for index in selected_indices)
        if abs(selected_area_m2 - target_area_m2) > area_tolerance_m2:
            continue
        if connected_indices(index_set=selected_indices, adjacency=adjacency) != selected_indices:
            continue

        remaining_indices = all_indices - selected_indices
        if not every_component_touches_boundary(
            index_set=set(remaining_indices),
            adjacency=adjacency,
            boundary_indices=boundary_indices,
        ):
            continue

        selected_rectangles = [normalized_cells[index] for index in sorted(selected_indices)]
        merged_rectangles = merge_axis_aligned_rectangles(selected_rectangles)
        zone_graph = zone_contact_graph(
            {
                **fixed_zone_rectangles_by_name,
                "PK_PB": merged_rectangles,
            }
        )
        if not graph_is_connected(zone_graph):
            continue

        shared_contact_length_m = sum(
            shared_boundary_length_m(candidate_rectangle, fixed_rectangle)
            for candidate_rectangle in merged_rectangles
            for fixed_rectangles in fixed_zone_rectangles_by_name.values()
            for fixed_rectangle in fixed_rectangles
        )
        score = (
            round(abs(selected_area_m2 - target_area_m2), 6),
            len(merged_rectangles),
            round(-shared_contact_length_m, 6),
            round(sum(rectangle_area_m2(rectangle) for rectangle in merged_rectangles), 6),
        )
        if best_selection is None or score < best_selection[0]:
            best_selection = (score, merged_rectangles)

    return best_selection[1] if best_selection is not None else []


def aligned_dimension_span(record: Record) -> dict[str, float | str] | None:
    x1 = numeric_group_code_value(record, "13")
    y1 = numeric_group_code_value(record, "23")
    x2 = numeric_group_code_value(record, "14")
    y2 = numeric_group_code_value(record, "24")
    measurement = numeric_group_code_value(record, "42")
    if x1 is None or y1 is None or x2 is None or y2 is None:
        return None

    if abs(y1 - y2) <= 1e-6:
        orientation = "horizontal"
    elif abs(x1 - x2) <= 1e-6:
        orientation = "vertical"
    else:
        return None

    return {
        "orientation": orientation,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "measurement": measurement if measurement is not None else 0.0,
    }


def cluster_numeric_values(values: list[float], tolerance: float = 40.0) -> list[dict[str, float]]:
    if not values:
        return []

    sorted_values = sorted(values)
    clusters: list[list[float]] = [[sorted_values[0]]]
    for value in sorted_values[1:]:
        if abs(value - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])

    return [
        {"center": sum(cluster) / len(cluster), "count": float(len(cluster))}
        for cluster in clusters
    ]


def close_polygon_points(points_xy: list[tuple[float, float]]) -> list[tuple[float, float]]:
    closed = [(float(x_value), float(y_value)) for x_value, y_value in points_xy]
    if not closed:
        return []
    first_x, first_y = closed[0]
    last_x, last_y = closed[-1]
    if abs(first_x - last_x) > 1e-6 or abs(first_y - last_y) > 1e-6:
        closed.append(closed[0])
    return closed


def cluster_axis_centers(values: list[float], tolerance: float = 25.0) -> list[float]:
    if not values:
        return []
    sorted_values = sorted(values)
    clusters: list[list[float]] = [[sorted_values[0]]]
    for value in sorted_values[1:]:
        if abs(value - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def snap_polygon_points(points_xy: list[tuple[float, float]], tolerance: float = 25.0) -> list[tuple[float, float]]:
    if not points_xy:
        return []
    x_centers = cluster_axis_centers([float(point[0]) for point in points_xy], tolerance=tolerance)
    y_centers = cluster_axis_centers([float(point[1]) for point in points_xy], tolerance=tolerance)

    def snap_value(value: float, centers: list[float]) -> float:
        return min(centers, key=lambda center: abs(center - value))

    return [
        (snap_value(float(point[0]), x_centers), snap_value(float(point[1]), y_centers))
        for point in points_xy
    ]


def polygon_is_axis_aligned(points_xy: list[tuple[float, float]]) -> bool:
    closed = close_polygon_points(snap_polygon_points(points_xy))
    if len(closed) < 4:
        return False
    for index in range(len(closed) - 1):
        x1, y1 = closed[index]
        x2, y2 = closed[index + 1]
        if abs(x1 - x2) <= 1e-6 or abs(y1 - y2) <= 1e-6:
            continue
        return False
    return True


def point_in_polygon_xy(point_xy: tuple[float, float], polygon_points: list[tuple[float, float]]) -> bool:
    closed = close_polygon_points(snap_polygon_points(polygon_points))
    if len(closed) < 4:
        return False
    point_x, point_y = point_xy
    inside = False
    for index in range(len(closed) - 1):
        x1, y1 = closed[index]
        x2, y2 = closed[index + 1]
        intersects_ray = ((y1 > point_y) != (y2 > point_y)) and (
            point_x < ((x2 - x1) * (point_y - y1) / ((y2 - y1) or 1e-12)) + x1
        )
        if intersects_ray:
            inside = not inside
    return inside


def polygon_area_xy(points_xy: list[tuple[float, float]]) -> float:
    if len(points_xy) < 3:
        return 0.0
    area = 0.0
    for index, (x1, y1) in enumerate(points_xy):
        x2, y2 = points_xy[(index + 1) % len(points_xy)]
        area += (x1 * y2) - (x2 * y1)
    return area / 2.0


def orthogonal_polygon_to_rectangles_mm(
    polygon_points: list[tuple[float, float]],
) -> list[tuple[float, float, float, float]]:
    closed = close_polygon_points(snap_polygon_points(polygon_points))
    if len(closed) < 4 or not polygon_is_axis_aligned(closed):
        return []

    xs = sorted({round(point[0], 6) for point in closed[:-1]})
    ys = sorted({round(point[1], 6) for point in closed[:-1]})
    cells: list[tuple[float, float, float, float]] = []
    for x_index in range(len(xs) - 1):
        x1 = xs[x_index]
        x2 = xs[x_index + 1]
        center_x = (x1 + x2) / 2.0
        for y_index in range(len(ys) - 1):
            y1 = ys[y_index]
            y2 = ys[y_index + 1]
            center_y = (y1 + y2) / 2.0
            if point_in_polygon_xy((center_x, center_y), closed):
                cells.append((x1, y1, x2, y2))
    return cells


def boundary_candidate_sort_key(candidate: dict[str, object]) -> tuple[int, int, int, float, str]:
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    scope_rank = {"apartment": 0, "room": 1, "supporting": 2}
    return (
        -int(candidate.get("priority", 0) or 0),
        confidence_rank.get(str(candidate.get("candidate_confidence", "")), 99),
        scope_rank.get(str(candidate.get("candidate_scope", "")), 99),
        float(candidate.get("bbox_area_mm2", 0.0) or 0.0),
        str(candidate.get("handle", "")),
    )


def boundary_candidate_contains_anchor(candidate: dict[str, object], anchor_xy_mm: tuple[float, float]) -> bool:
    points_xy = [
        (float(point[0]), float(point[1]))
        for point in list(candidate.get("points_xy", []))
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if points_xy and bool(candidate.get("closed_polyline")):
        if point_in_polygon_xy(anchor_xy_mm, points_xy):
            return True
    bbox_xy = candidate.get("bbox_xy")
    if isinstance(bbox_xy, list) and len(bbox_xy) == 4:
        return point_in_rectangle(anchor_xy_mm, tuple(float(value) for value in bbox_xy))
    return False


def boundary_candidate_rectangles_local_m(
    candidate: dict[str, object],
    outer_bbox_mm: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    outer_min_x, outer_min_y, _outer_max_x, _outer_max_y = outer_bbox_mm
    points_xy = [
        (float(point[0]), float(point[1]))
        for point in list(candidate.get("points_xy", []))
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    world_rectangles_mm = orthogonal_polygon_to_rectangles_mm(points_xy)
    if not world_rectangles_mm:
        bbox_xy = candidate.get("bbox_xy")
        if isinstance(bbox_xy, list) and len(bbox_xy) == 4:
            world_rectangles_mm = [tuple(float(value) for value in bbox_xy)]
    local_rectangles = [
        normalize_rect(
            (
                (rectangle[0] - outer_min_x) / 1000.0,
                (rectangle[1] - outer_min_y) / 1000.0,
                (rectangle[2] - outer_min_x) / 1000.0,
                (rectangle[3] - outer_min_y) / 1000.0,
            )
        )
        for rectangle in world_rectangles_mm
    ]
    return merge_axis_aligned_rectangles(local_rectangles)


def boundary_candidate_polygon_local_m(
    candidate: dict[str, object],
    outer_bbox_mm: tuple[float, float, float, float],
) -> list[tuple[float, float]]:
    outer_min_x, outer_min_y, _outer_max_x, _outer_max_y = outer_bbox_mm
    points_xy = [
        (float(point[0]), float(point[1]))
        for point in list(candidate.get("points_xy", []))
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    closed_points = close_polygon_points(snap_polygon_points(points_xy))
    if len(closed_points) < 4:
        return []

    polygon_points: list[tuple[float, float]] = []
    for point_x, point_y in closed_points[:-1]:
        local_point = ((point_x - outer_min_x) / 1000.0, (point_y - outer_min_y) / 1000.0)
        if polygon_points and math.hypot(local_point[0] - polygon_points[-1][0], local_point[1] - polygon_points[-1][1]) <= 1e-9:
            continue
        polygon_points.append(local_point)
    if len(polygon_points) >= 2 and math.hypot(
        polygon_points[0][0] - polygon_points[-1][0],
        polygon_points[0][1] - polygon_points[-1][1],
    ) <= 1e-9:
        polygon_points.pop()
    if len(polygon_points) < 3 or abs(polygon_area_xy(polygon_points)) <= 1e-9:
        return []
    return polygon_points


def bbox_contains_all_points(
    bbox_xy: tuple[float, float, float, float],
    points_xy: list[tuple[float, float]],
    *,
    tolerance_mm: float = 1e-6,
) -> bool:
    if not points_xy:
        return False
    min_x, min_y, max_x, max_y = normalize_rect(bbox_xy)
    return all(
        min_x - tolerance_mm <= point_x <= max_x + tolerance_mm
        and min_y - tolerance_mm <= point_y <= max_y + tolerance_mm
        for point_x, point_y in points_xy
    )


def room_boundary_union_bbox(
    boundary_candidates: list[dict[str, object]],
) -> tuple[float, float, float, float] | None:
    room_bboxes: list[tuple[float, float, float, float]] = []
    for candidate in boundary_candidates:
        if str(candidate.get("candidate_scope", "")) != "room":
            continue
        bbox_xy = candidate.get("bbox_xy")
        if not isinstance(bbox_xy, list) or len(bbox_xy) != 4:
            continue
        room_bboxes.append(normalize_rect(tuple(float(value) for value in bbox_xy)))
    if not room_bboxes:
        return None
    return (
        min(rectangle[0] for rectangle in room_bboxes),
        min(rectangle[1] for rectangle in room_bboxes),
        max(rectangle[2] for rectangle in room_bboxes),
        max(rectangle[3] for rectangle in room_bboxes),
    )


def assign_layer_room_boundary_rectangles_local(
    *,
    boundary_candidates: list[dict[str, object]],
    zone_anchor_world_by_key: dict[str, tuple[float, float]],
    zone_boundary_handles_by_key: dict[str, list[str]] | None = None,
    outer_bbox_mm: tuple[float, float, float, float],
) -> tuple[
    dict[str, list[tuple[float, float, float, float]]],
    dict[str, list[list[tuple[float, float]]]],
    dict[str, dict[str, object]],
]:
    room_boundary_candidates = sorted(
        [
            dict(candidate)
            for candidate in boundary_candidates
            if str(candidate.get("candidate_scope", "")) == "room"
        ],
        key=boundary_candidate_sort_key,
    )
    assigned_rectangles: dict[str, list[tuple[float, float, float, float]]] = {}
    assigned_polygons: dict[str, list[list[tuple[float, float]]]] = {}
    assignment_debug: dict[str, dict[str, object]] = {}
    used_handles: set[str] = set()
    candidates_by_handle = {
        str(candidate.get("handle", "")).strip(): candidate
        for candidate in room_boundary_candidates
        if str(candidate.get("handle", "")).strip()
    }

    for zone_key, boundary_handles in sorted(dict(zone_boundary_handles_by_key or {}).items()):
        selected_candidates = [
            candidates_by_handle[boundary_handle]
            for boundary_handle in boundary_handles
            if boundary_handle in candidates_by_handle and boundary_handle not in used_handles
        ]
        if not selected_candidates:
            continue
        rectangles_local_m: list[tuple[float, float, float, float]] = []
        polygons_local_m: list[list[tuple[float, float]]] = []
        for selected_candidate in selected_candidates:
            rectangles_local_m.extend(boundary_candidate_rectangles_local_m(selected_candidate, outer_bbox_mm))
            polygon_local_m = boundary_candidate_polygon_local_m(selected_candidate, outer_bbox_mm)
            if polygon_local_m:
                polygons_local_m.append(polygon_local_m)
        if not rectangles_local_m:
            continue
        selected_handles = [
            str(candidate.get("handle", "")).strip()
            for candidate in selected_candidates
            if str(candidate.get("handle", "")).strip()
        ]
        used_handles.update(selected_handles)
        assigned_rectangles[zone_key] = merge_axis_aligned_rectangles(rectangles_local_m)
        if polygons_local_m:
            assigned_polygons[zone_key] = polygons_local_m
        assignment_debug[zone_key] = {
            "boundary_handle": selected_handles[0] if len(selected_handles) == 1 else None,
            "boundary_handles": selected_handles,
            "boundary_layer": "+".join(
                sorted({str(candidate.get("source_layer", "")).strip() for candidate in selected_candidates if str(candidate.get("source_layer", "")).strip()})
            ),
            "boundary_role": "+".join(
                sorted({str(candidate.get("layer_role", "")).strip() for candidate in selected_candidates if str(candidate.get("layer_role", "")).strip()})
            ),
            "boundary_confidence": "high"
            if all(str(candidate.get("candidate_confidence", "")) == "high" for candidate in selected_candidates)
            else "medium",
            "boundary_closed_polyline": all(bool(candidate.get("closed_polyline")) for candidate in selected_candidates),
            "boundary_rectangles_local_m": [
                [round(value, 3) for value in rectangle]
                for rectangle in assigned_rectangles[zone_key]
            ],
            "boundary_polygons_local_m": [
                [[round(point[0], 3), round(point[1], 3)] for point in polygon]
                for polygon in polygons_local_m
            ],
            "assignment_mode": "mapping_boundary_handles",
        }

    for zone_key, anchor_xy_mm in sorted(zone_anchor_world_by_key.items()):
        if zone_key in assigned_rectangles:
            continue
        matched_candidates = [
            candidate
            for candidate in room_boundary_candidates
            if candidate.get("handle") not in used_handles
            and boundary_candidate_contains_anchor(candidate, anchor_xy_mm)
        ]
        if not matched_candidates:
            continue
        selected_candidate = sorted(matched_candidates, key=boundary_candidate_sort_key)[0]
        candidate_handle = str(selected_candidate.get("handle", ""))
        if candidate_handle:
            used_handles.add(candidate_handle)
        rectangles_local_m = boundary_candidate_rectangles_local_m(selected_candidate, outer_bbox_mm)
        if not rectangles_local_m:
            continue
        polygon_local_m = boundary_candidate_polygon_local_m(selected_candidate, outer_bbox_mm)
        assigned_rectangles[zone_key] = rectangles_local_m
        if polygon_local_m:
            assigned_polygons[zone_key] = [polygon_local_m]
        assignment_debug[zone_key] = {
            "boundary_handle": selected_candidate.get("handle"),
            "boundary_layer": selected_candidate.get("source_layer"),
            "boundary_role": selected_candidate.get("layer_role"),
            "boundary_confidence": selected_candidate.get("candidate_confidence"),
            "boundary_closed_polyline": bool(selected_candidate.get("closed_polyline")),
            "boundary_rectangles_local_m": [
                [round(value, 3) for value in rectangle]
                for rectangle in rectangles_local_m
            ],
            "boundary_polygons_local_m": (
                [[[round(point[0], 3), round(point[1], 3)] for point in polygon_local_m]]
                if polygon_local_m
                else []
            ),
        }

    return assigned_rectangles, assigned_polygons, assignment_debug


def rectangles_centroid_xy_m(
    rectangles: list[tuple[float, float, float, float]],
) -> tuple[float, float] | None:
    if not rectangles:
        return None
    total_area_m2 = rectangles_area_m2(rectangles)
    if total_area_m2 <= 1e-9:
        min_x = min(rectangle[0] for rectangle in rectangles)
        min_y = min(rectangle[1] for rectangle in rectangles)
        max_x = max(rectangle[2] for rectangle in rectangles)
        max_y = max(rectangle[3] for rectangle in rectangles)
        return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)

    weighted_x = 0.0
    weighted_y = 0.0
    for rectangle in rectangles:
        min_x, min_y, max_x, max_y = normalize_rect(rectangle)
        area_m2 = max(0.0, max_x - min_x) * max(0.0, max_y - min_y)
        if area_m2 <= 0.0:
            continue
        weighted_x += ((min_x + max_x) / 2.0) * area_m2
        weighted_y += ((min_y + max_y) / 2.0) * area_m2
    if weighted_x == 0.0 and weighted_y == 0.0:
        return None
    return (weighted_x / total_area_m2, weighted_y / total_area_m2)


def total_rectangles_overlap_area_m2(
    first_rectangles: list[tuple[float, float, float, float]],
    second_rectangles: list[tuple[float, float, float, float]],
) -> float:
    return sum(
        rectangles_overlap_area_m2(first_rectangle, second_rectangle)
        for first_rectangle in first_rectangles
        for second_rectangle in second_rectangles
    )


def assigned_boundary_handles(assignment_debug_by_key: dict[str, dict[str, object]]) -> set[str]:
    used_handles: set[str] = set()
    for payload in assignment_debug_by_key.values():
        if not isinstance(payload, dict):
            continue
        boundary_handle = str(payload.get("boundary_handle", "") or "").strip()
        if boundary_handle:
            used_handles.add(boundary_handle)
        for item in list(payload.get("boundary_handles", [])):
            handle = str(item or "").strip()
            if handle:
                used_handles.add(handle)
    return used_handles


def backfill_missing_layer_boundary_zones(
    *,
    boundary_candidates: list[dict[str, object]],
    outer_bbox_mm: tuple[float, float, float, float],
    assigned_rectangles_by_key: dict[str, list[tuple[float, float, float, float]]],
    assignment_debug_by_key: dict[str, dict[str, object]],
    source_zone_name_by_key: dict[str, str],
    zone_target_area_by_key: dict[str, float],
    reference_rectangles_local_by_key: dict[str, tuple[float, float, float, float]],
    remembered_zone_targets_by_key: dict[str, dict[str, object]],
    minimum_overlap_area_m2: float = 0.05,
) -> None:
    if not reference_rectangles_local_by_key:
        return

    used_handles = assigned_boundary_handles(assignment_debug_by_key)
    room_boundary_candidates = sorted(
        [
            dict(candidate)
            for candidate in boundary_candidates
            if str(candidate.get("candidate_scope", "")) == "room"
            and bool(candidate.get("closed_polyline"))
            and str(candidate.get("handle", "")).strip() not in used_handles
        ],
        key=boundary_candidate_sort_key,
    )
    if not room_boundary_candidates:
        return

    missing_zone_keys = [
        zone_key
        for zone_key in sorted(reference_rectangles_local_by_key)
        if zone_key not in assigned_rectangles_by_key
    ]
    if not missing_zone_keys:
        return

    assignment_edges: list[tuple[float, float, float, tuple[int, int, int, float, str], str, dict[str, object], list[tuple[float, float, float, float]]]] = []
    for zone_key in missing_zone_keys:
        reference_rectangles = [normalize_rect(reference_rectangles_local_by_key[zone_key])]
        reference_area_m2 = rectangles_area_m2(reference_rectangles)
        reference_centroid = rectangles_centroid_xy_m(reference_rectangles)
        for candidate in room_boundary_candidates:
            candidate_rectangles = boundary_candidate_rectangles_local_m(candidate, outer_bbox_mm)
            if not candidate_rectangles:
                continue
            overlap_area_m2 = total_rectangles_overlap_area_m2(reference_rectangles, candidate_rectangles)
            if overlap_area_m2 < minimum_overlap_area_m2:
                continue
            candidate_area_m2 = rectangles_area_m2(candidate_rectangles)
            candidate_centroid = rectangles_centroid_xy_m(candidate_rectangles)
            centroid_distance_m = math.inf
            if reference_centroid is not None and candidate_centroid is not None:
                centroid_distance_m = math.hypot(
                    candidate_centroid[0] - reference_centroid[0],
                    candidate_centroid[1] - reference_centroid[1],
                )
            assignment_edges.append(
                (
                    -overlap_area_m2,
                    abs(candidate_area_m2 - reference_area_m2),
                    centroid_distance_m,
                    boundary_candidate_sort_key(candidate),
                    zone_key,
                    candidate,
                    candidate_rectangles,
                )
            )

    if not assignment_edges:
        return

    claimed_zone_keys: set[str] = set()
    claimed_handles: set[str] = set()
    for (
        neg_overlap_area_m2,
        area_delta_m2,
        centroid_distance_m,
        _sort_key,
        zone_key,
        candidate,
        candidate_rectangles,
    ) in sorted(assignment_edges):
        candidate_handle = str(candidate.get("handle", "")).strip()
        if zone_key in claimed_zone_keys or candidate_handle in claimed_handles:
            continue
        claimed_zone_keys.add(zone_key)
        claimed_handles.add(candidate_handle)

        assigned_rectangles_by_key[zone_key] = candidate_rectangles
        remembered_target = dict(remembered_zone_targets_by_key.get(zone_key, {}))
        remembered_area_m2 = float(remembered_target.get("area_m2", 0.0) or 0.0)
        source_zone_name_by_key.setdefault(zone_key, default_source_zone_name(zone_key))
        if zone_key not in zone_target_area_by_key:
            zone_target_area_by_key[zone_key] = remembered_area_m2 or rectangles_area_m2(candidate_rectangles)
        assignment_debug_by_key[zone_key] = {
            "boundary_handle": candidate.get("handle"),
            "boundary_layer": candidate.get("source_layer"),
            "boundary_role": candidate.get("layer_role"),
            "boundary_confidence": candidate.get("candidate_confidence"),
            "boundary_closed_polyline": bool(candidate.get("closed_polyline")),
            "boundary_rectangles_local_m": [
                [round(value, 3) for value in rectangle]
                for rectangle in candidate_rectangles
            ],
            "assignment_mode": "seed_overlap_backfill",
            "seed_overlap_area_m2": round(-neg_overlap_area_m2, 3),
            "seed_area_delta_m2": round(area_delta_m2, 3),
            "seed_centroid_distance_m": (
                None if math.isinf(centroid_distance_m) else round(centroid_distance_m, 3)
            ),
        }


def backfill_missing_layer_boundary_zones_by_area(
    *,
    boundary_candidates: list[dict[str, object]],
    outer_bbox_mm: tuple[float, float, float, float],
    assigned_rectangles_by_key: dict[str, list[tuple[float, float, float, float]]],
    assignment_debug_by_key: dict[str, dict[str, object]],
    source_zone_name_by_key: dict[str, str],
    zone_target_area_by_key: dict[str, float],
    remembered_zone_targets_by_key: dict[str, dict[str, object]],
    relative_tolerance: float = 0.12,
    absolute_tolerance_m2: float = 0.25,
) -> None:
    used_handles = assigned_boundary_handles(assignment_debug_by_key)
    room_boundary_candidates = [
        dict(candidate)
        for candidate in boundary_candidates
        if str(candidate.get("candidate_scope", "")) == "room"
        and str(candidate.get("handle", "")).strip() not in used_handles
    ]
    missing_zone_keys = [
        zone_key
        for zone_key in sorted(source_zone_name_by_key)
        if zone_key not in assigned_rectangles_by_key
    ]
    if not room_boundary_candidates or not missing_zone_keys:
        return

    candidate_rows: list[tuple[str, dict[str, object], list[tuple[float, float, float, float]], float]] = []
    for candidate in room_boundary_candidates:
        candidate_handle = str(candidate.get("handle", "")).strip()
        if not candidate_handle:
            continue
        candidate_rectangles = boundary_candidate_rectangles_local_m(candidate, outer_bbox_mm)
        candidate_area_m2 = rectangles_area_m2(candidate_rectangles)
        if candidate_area_m2 <= 1e-6:
            continue
        candidate_rows.append((candidate_handle, candidate, candidate_rectangles, candidate_area_m2))

    assignment_edges: list[tuple[float, float, tuple[int, int, int, float, str], str, str, dict[str, object], list[tuple[float, float, float, float]], float]] = []
    for zone_key in missing_zone_keys:
        target_area_m2 = float(zone_target_area_by_key.get(zone_key, 0.0) or 0.0)
        if target_area_m2 <= 1e-6:
            remembered_target = dict(remembered_zone_targets_by_key.get(zone_key, {}))
            target_area_m2 = float(remembered_target.get("area_m2", 0.0) or 0.0)
        if target_area_m2 <= 1e-6:
            continue
        tolerance_m2 = max(absolute_tolerance_m2, target_area_m2 * relative_tolerance)
        for candidate_handle, candidate, candidate_rectangles, candidate_area_m2 in candidate_rows:
            area_delta_m2 = abs(candidate_area_m2 - target_area_m2)
            if area_delta_m2 > tolerance_m2:
                continue
            assignment_edges.append(
                (
                    area_delta_m2,
                    -candidate_area_m2,
                    boundary_candidate_sort_key(candidate),
                    zone_key,
                    candidate_handle,
                    candidate,
                    candidate_rectangles,
                    candidate_area_m2,
                )
            )

    claimed_zone_keys: set[str] = set()
    claimed_handles: set[str] = set()
    for (
        area_delta_m2,
        _neg_candidate_area_m2,
        _sort_key,
        zone_key,
        candidate_handle,
        candidate,
        candidate_rectangles,
        candidate_area_m2,
    ) in sorted(assignment_edges):
        if zone_key in claimed_zone_keys or candidate_handle in claimed_handles:
            continue
        claimed_zone_keys.add(zone_key)
        claimed_handles.add(candidate_handle)

        assigned_rectangles_by_key[zone_key] = candidate_rectangles
        source_zone_name_by_key.setdefault(zone_key, default_source_zone_name(zone_key))
        if zone_key not in zone_target_area_by_key:
            zone_target_area_by_key[zone_key] = candidate_area_m2
        assignment_debug_by_key[zone_key] = {
            "boundary_handle": candidate.get("handle"),
            "boundary_layer": candidate.get("source_layer"),
            "boundary_role": candidate.get("layer_role"),
            "boundary_confidence": candidate.get("candidate_confidence"),
            "boundary_closed_polyline": bool(candidate.get("closed_polyline")),
            "boundary_rectangles_local_m": [
                [round(value, 3) for value in rectangle]
                for rectangle in candidate_rectangles
            ],
            "assignment_mode": "target_area_backfill",
            "target_area_m2": round(float(zone_target_area_by_key.get(zone_key, candidate_area_m2) or candidate_area_m2), 3),
            "candidate_area_m2": round(candidate_area_m2, 3),
            "area_delta_m2": round(area_delta_m2, 3),
        }


def apply_zone_merge_groups(
    *,
    geometry_policy: dict[str, object],
    source_zone_name_by_key: dict[str, str],
    zone_target_area_by_key: dict[str, float],
    zone_anchor_world_by_key: dict[str, tuple[float, float]],
    zone_anchor_local_by_key: dict[str, tuple[float, float]],
    zone_rectangles_local_m_by_key: dict[str, list[tuple[float, float, float, float]]],
    layer_boundary_assignment_by_key: dict[str, dict[str, object]],
) -> dict[str, str]:
    source_to_target = zone_merge_source_to_target_map(geometry_policy)
    target_names = zone_merge_target_names(geometry_policy)
    if not source_to_target:
        return {}

    target_to_sources: dict[str, list[str]] = {}
    for source_zone_key, target_zone_key in source_to_target.items():
        if source_zone_key in source_zone_name_by_key or source_zone_key in zone_rectangles_local_m_by_key:
            target_to_sources.setdefault(target_zone_key, []).append(source_zone_key)

    applied_source_to_target: dict[str, str] = {}
    for target_zone_key, source_zone_keys in sorted(target_to_sources.items()):
        merged_rectangles: list[tuple[float, float, float, float]] = []
        target_area_m2 = 0.0
        world_anchors: list[tuple[float, float]] = []
        local_anchors: list[tuple[float, float]] = []
        merged_assignments: list[dict[str, object]] = []
        merged_source_names: list[str] = []

        for source_zone_key in source_zone_keys:
            rectangles = zone_rectangles_local_m_by_key.get(source_zone_key, [])
            merged_rectangles.extend(rectangles)
            target_area_m2 += float(zone_target_area_by_key.get(source_zone_key, 0.0) or 0.0)
            if source_zone_key in zone_anchor_world_by_key:
                world_anchors.append(zone_anchor_world_by_key[source_zone_key])
            if source_zone_key in zone_anchor_local_by_key:
                local_anchors.append(zone_anchor_local_by_key[source_zone_key])
            if source_zone_key in layer_boundary_assignment_by_key:
                merged_assignments.append(dict(layer_boundary_assignment_by_key[source_zone_key]))
            source_name = str(source_zone_name_by_key.get(source_zone_key, "") or "").strip()
            if source_name:
                merged_source_names.append(source_name)

        if not merged_rectangles:
            continue

        zone_rectangles_local_m_by_key[target_zone_key] = merge_axis_aligned_rectangles(merged_rectangles)
        zone_target_area_by_key[target_zone_key] = target_area_m2 or rectangles_area_m2(merged_rectangles)
        source_zone_name_by_key[target_zone_key] = target_names.get(target_zone_key, default_source_zone_name(target_zone_key))
        if world_anchors:
            zone_anchor_world_by_key[target_zone_key] = (
                sum(anchor[0] for anchor in world_anchors) / len(world_anchors),
                sum(anchor[1] for anchor in world_anchors) / len(world_anchors),
            )
        if local_anchors:
            zone_anchor_local_by_key[target_zone_key] = (
                sum(anchor[0] for anchor in local_anchors) / len(local_anchors),
                sum(anchor[1] for anchor in local_anchors) / len(local_anchors),
            )
        layer_boundary_assignment_by_key[target_zone_key] = {
            "assignment_mode": "zone_merge_group",
            "merged_source_zone_keys": source_zone_keys,
            "merged_source_zone_names": merged_source_names,
            "merged_boundary_assignments": merged_assignments,
        }

        for source_zone_key in source_zone_keys:
            applied_source_to_target[source_zone_key] = target_zone_key
            if source_zone_key == target_zone_key:
                continue
            source_zone_name_by_key.pop(source_zone_key, None)
            zone_target_area_by_key.pop(source_zone_key, None)
            zone_anchor_world_by_key.pop(source_zone_key, None)
            zone_anchor_local_by_key.pop(source_zone_key, None)
            zone_rectangles_local_m_by_key.pop(source_zone_key, None)
            layer_boundary_assignment_by_key.pop(source_zone_key, None)

    return applied_source_to_target


def detect_outer_apartment_record(
    records: list[Record],
    room_anchor_points: list[tuple[float, float]],
) -> Record | None:
    if not room_anchor_points:
        return None

    candidate_records: list[tuple[float, float, Record]] = []
    for record in records:
        if record.section != "FILTERED_RECORDS" or record.record_type not in {"LWPOLYLINE", "POLYLINE"}:
            continue
        bbox = record.bbox
        if bbox is None:
            continue
        if not any(
            raw_line.strip() == "70" and raw_value.strip() == "1"
            for raw_line, raw_value in zip(record.raw_lines[::2], record.raw_lines[1::2])
        ):
            continue

        min_x, min_y, max_x, max_y = bbox
        if not all(
            min_x - 1e-6 <= point_x <= max_x + 1e-6 and min_y - 1e-6 <= point_y <= max_y + 1e-6
            for point_x, point_y in room_anchor_points
        ):
            continue

        area = (max_x - min_x) * (max_y - min_y)
        perimeter = (max_x - min_x) + (max_y - min_y)
        candidate_records.append((area, perimeter, record))

    if not candidate_records:
        return None

    candidate_records.sort(key=lambda item: (item[0], item[1], item[2].handle))
    return candidate_records[0][2]


def infer_inner_dimension_bounds_local(
    records: list[Record],
    outer_bbox: tuple[float, float, float, float],
    *,
    band_mm: float = 900.0,
    cluster_tolerance_mm: float = 40.0,
    edge_search_window_mm: float = 450.0,
    min_occurrences: int = 2,
) -> dict[str, float]:
    outer_min_x, outer_min_y, outer_max_x, outer_max_y = outer_bbox
    horizontal_x_values: list[float] = []
    vertical_y_values: list[float] = []

    for record in records:
        if record.section != "FILTERED_RECORDS" or record.record_type != "DIMENSION" or record.layer != "TAC - Dim":
            continue
        span = aligned_dimension_span(record)
        if span is None:
            continue

        if span["orientation"] == "horizontal":
            y_value = float(span["y1"])
            if min(abs(y_value - outer_min_y), abs(y_value - outer_max_y)) > band_mm:
                continue
            for x_value in (float(span["x1"]), float(span["x2"])):
                if outer_min_x - band_mm <= x_value <= outer_max_x + band_mm:
                    horizontal_x_values.append(x_value)
        else:
            x_value = float(span["x1"])
            if min(abs(x_value - outer_min_x), abs(x_value - outer_max_x)) > band_mm:
                continue
            for y_value in (float(span["y1"]), float(span["y2"])):
                if outer_min_y - band_mm <= y_value <= outer_max_y + band_mm:
                    vertical_y_values.append(y_value)

    def clustered_centers(values: list[float]) -> list[float]:
        return [
            cluster["center"]
            for cluster in cluster_numeric_values(values, tolerance=cluster_tolerance_mm)
            if cluster["count"] >= min_occurrences
        ]

    def nearest_in_band(candidates: list[float], low: float, high: float, prefer: str) -> float | None:
        in_band = [value for value in candidates if low <= value <= high]
        if not in_band:
            return None
        return min(in_band) if prefer == "min" else max(in_band)

    x_centers = clustered_centers(horizontal_x_values)
    y_centers = clustered_centers(vertical_y_values)

    bounds: dict[str, float] = {}
    left_inner = nearest_in_band(x_centers, outer_min_x + 40.0, outer_min_x + edge_search_window_mm, "min")
    right_inner = nearest_in_band(x_centers, outer_max_x - edge_search_window_mm, outer_max_x - 40.0, "max")
    bottom_inner = nearest_in_band(y_centers, outer_min_y + 40.0, outer_min_y + edge_search_window_mm, "min")
    top_inner = nearest_in_band(y_centers, outer_max_y - edge_search_window_mm, outer_max_y - 40.0, "max")
    if left_inner is not None:
        bounds["inner_left_m"] = (left_inner - outer_min_x) / 1000.0
    if right_inner is not None:
        bounds["inner_right_m"] = (right_inner - outer_min_x) / 1000.0
    if bottom_inner is not None:
        bounds["inner_bottom_m"] = (bottom_inner - outer_min_y) / 1000.0
    if top_inner is not None:
        bounds["inner_top_m"] = (top_inner - outer_min_y) / 1000.0
    return bounds


def build_dimension_coordinate_grid_local(
    records: list[Record],
    outer_bbox: tuple[float, float, float, float],
    *,
    cluster_tolerance_mm: float = 35.0,
    seed_rectangles_local: dict[str, tuple[float, float, float, float]] | None = None,
) -> dict[str, list[float]]:
    outer_min_x, outer_min_y, outer_max_x, outer_max_y = outer_bbox
    x_values_mm = [outer_min_x, outer_max_x]
    y_values_mm = [outer_min_y, outer_max_y]

    for record in records:
        if record.section != "FILTERED_RECORDS" or record.record_type != "DIMENSION" or record.layer != "TAC - Dim":
            continue
        span = aligned_dimension_span(record)
        if span is None:
            continue
        for x_value in (float(span["x1"]), float(span["x2"])):
            if outer_min_x - 200.0 <= x_value <= outer_max_x + 200.0:
                x_values_mm.append(x_value)
        for y_value in (float(span["y1"]), float(span["y2"])):
            if outer_min_y - 200.0 <= y_value <= outer_max_y + 200.0:
                y_values_mm.append(y_value)

    if seed_rectangles_local is not None:
        for rectangle in seed_rectangles_local.values():
            x1, y1, x2, y2 = normalize_rect(rectangle)
            x_values_mm.extend([outer_min_x + (x1 * 1000.0), outer_min_x + (x2 * 1000.0)])
            y_values_mm.extend([outer_min_y + (y1 * 1000.0), outer_min_y + (y2 * 1000.0)])

    def cluster_to_local(values_mm: list[float], origin_mm: float) -> list[float]:
        centers = [cluster["center"] for cluster in cluster_numeric_values(values_mm, tolerance=cluster_tolerance_mm)]
        return sorted({round((value - origin_mm) / 1000.0, 3) for value in centers})

    return {
        "x": cluster_to_local(x_values_mm, outer_min_x),
        "y": cluster_to_local(y_values_mm, outer_min_y),
    }


def infer_named_zone_clear_rectangles_from_dimension_grid_local(
    *,
    zone_anchor_local_by_key: dict[str, tuple[float, float]],
    zone_target_area_by_key: dict[str, float],
    outer_width_m: float,
    outer_height_m: float,
    dimension_grid_local: dict[str, list[float]],
    seed_rectangles_local: dict[str, tuple[float, float, float, float]],
) -> dict[str, tuple[float, float, float, float]]:
    target_dimensions_m: dict[str, tuple[float, float]] = {
        "PN_02": (3.55, 3.02),
        "WC_01": (2.19, 1.42),
        "PN_01": (3.47, 3.13),
        "WC_02": (1.67, 1.70),
        "LOGIA": (3.00, 0.86),
    }

    x_candidates = sorted({0.0, round(outer_width_m, 3), *dimension_grid_local.get("x", [])})
    y_candidates = sorted({0.0, round(outer_height_m, 3), *dimension_grid_local.get("y", [])})

    resolved: dict[str, tuple[float, float, float, float]] = {}
    for zone_key, seed_rectangle in seed_rectangles_local.items():
        target_width_m, target_height_m = target_dimensions_m.get(
            zone_key,
            (seed_rectangle[2] - seed_rectangle[0], seed_rectangle[3] - seed_rectangle[1]),
        )
        target_area_m2 = zone_target_area_by_key.get(zone_key, target_width_m * target_height_m)
        anchor_xy_m = zone_anchor_local_by_key.get(zone_key)
        seed_min_x, seed_min_y, seed_max_x, seed_max_y = normalize_rect(seed_rectangle)
        best_score: tuple[float, float, float, float, float, float] | None = None
        best_rectangle: tuple[float, float, float, float] | None = None

        for x1 in x_candidates:
            for x2 in x_candidates:
                if x2 <= x1 + 0.30:
                    continue
                if anchor_xy_m is not None and not (x1 - 1e-9 <= anchor_xy_m[0] <= x2 + 1e-9):
                    continue
                width_m = x2 - x1
                width_diff_m = abs(width_m - target_width_m)
                if width_diff_m > 0.45:
                    continue
                for y1 in y_candidates:
                    for y2 in y_candidates:
                        if y2 <= y1 + 0.30:
                            continue
                        if anchor_xy_m is not None and not (y1 - 1e-9 <= anchor_xy_m[1] <= y2 + 1e-9):
                            continue
                        height_m = y2 - y1
                        height_diff_m = abs(height_m - target_height_m)
                        if height_diff_m > 0.45:
                            continue
                        area_m2 = width_m * height_m
                        area_diff_m2 = abs(area_m2 - target_area_m2)
                        seed_delta_m = abs(x1 - seed_min_x) + abs(x2 - seed_max_x) + abs(y1 - seed_min_y) + abs(y2 - seed_max_y)
                        perimeter_prior = 0.0
                        if zone_key in {"PN_01", "PN_02", "WC_01"}:
                            perimeter_prior += abs(x1 - 0.0)
                        if zone_key in {"WC_02", "LOGIA"}:
                            perimeter_prior += abs(x2 - outer_width_m)
                        if zone_key in {"PN_02", "WC_02"}:
                            perimeter_prior += abs(y1 - 0.0)
                        if zone_key in {"PN_01", "LOGIA"}:
                            perimeter_prior += abs(y2 - outer_height_m)
                        anchor_center_distance_m = 0.0
                        if anchor_xy_m is not None:
                            anchor_center_distance_m = math.hypot(((x1 + x2) / 2.0) - anchor_xy_m[0], ((y1 + y2) / 2.0) - anchor_xy_m[1])
                        score = (
                            round(width_diff_m + height_diff_m, 6),
                            round(area_diff_m2, 6),
                            round(seed_delta_m, 6),
                            round(perimeter_prior, 6),
                            round(anchor_center_distance_m, 6),
                            round((x2 - x1) + (y2 - y1), 6),
                        )
                        if best_score is None or score < best_score:
                            best_score = score
                            best_rectangle = (x1, y1, x2, y2)
        resolved[zone_key] = normalize_rect(best_rectangle or seed_rectangle)
    return resolved


def infer_dimension_guided_zone_rectangles_local(
    *,
    outer_width_m: float,
    outer_height_m: float,
) -> dict[str, tuple[float, float, float, float]]:
    local_rectangles = {
        "PN_02": (0.125, 0.205, 3.675, 3.225),
        "WC_01": (0.125, 3.365, 2.315, 4.785),
        "PN_01": (0.205, 4.925, 3.675, 8.055),
        "WC_02": (5.380, 0.205, 7.050, 1.900),
        "LOGIA": (3.925, 7.305, 6.925, 8.180),
    }
    normalized: dict[str, tuple[float, float, float, float]] = {}
    for zone_key, rectangle in local_rectangles.items():
        x1, y1, x2, y2 = normalize_rect(rectangle)
        if x2 > outer_width_m + 1e-9 or y2 > outer_height_m + 1e-9:
            continue
        normalized[zone_key] = (x1, y1, x2, y2)
    return normalized


def expand_perimeter_room_rectangles_local(
    room_rectangles_local: dict[str, tuple[float, float, float, float]],
    outer_rectangle_local: tuple[float, float, float, float],
    *,
    exterior_gap_max_m: float = 0.60,
    shared_overlap_min_m: float = 0.60,
) -> dict[str, tuple[float, float, float, float]]:
    expanded = {zone_key: list(normalize_rect(rectangle)) for zone_key, rectangle in room_rectangles_local.items()}
    outer_min_x, outer_min_y, outer_max_x, outer_max_y = normalize_rect(outer_rectangle_local)

    def blocked(zone_key: str, side: str, rectangle: list[float]) -> bool:
        x1, y1, x2, y2 = rectangle
        for other_key, other_rectangle in expanded.items():
            if other_key == zone_key:
                continue
            ox1, oy1, ox2, oy2 = other_rectangle
            if side == "left":
                if ox2 <= x1 + 1e-9 and interval_overlap_length(y1, y2, oy1, oy2) >= shared_overlap_min_m:
                    return True
            elif side == "right":
                if ox1 >= x2 - 1e-9 and interval_overlap_length(y1, y2, oy1, oy2) >= shared_overlap_min_m:
                    return True
            elif side == "bottom":
                if oy2 <= y1 + 1e-9 and interval_overlap_length(x1, x2, ox1, ox2) >= shared_overlap_min_m:
                    return True
            elif side == "top":
                if oy1 >= y2 - 1e-9 and interval_overlap_length(x1, x2, ox1, ox2) >= shared_overlap_min_m:
                    return True
        return False

    for zone_key in sorted(expanded):
        rectangle = expanded[zone_key]
        if rectangle[0] - outer_min_x <= exterior_gap_max_m and not blocked(zone_key, "left", rectangle):
            rectangle[0] = outer_min_x
        if rectangle[1] - outer_min_y <= exterior_gap_max_m and not blocked(zone_key, "bottom", rectangle):
            rectangle[1] = outer_min_y
        if outer_max_x - rectangle[2] <= exterior_gap_max_m and not blocked(zone_key, "right", rectangle):
            rectangle[2] = outer_max_x
        if outer_max_y - rectangle[3] <= exterior_gap_max_m and not blocked(zone_key, "top", rectangle):
            rectangle[3] = outer_max_y

    return {
        zone_key: normalize_rect(tuple(values))
        for zone_key, values in expanded.items()
    }


def reconcile_named_room_shared_walls_local(
    room_rectangles_local: dict[str, tuple[float, float, float, float]],
    *,
    shared_wall_gap_max_m: float = 0.35,
    shared_overlap_min_m: float = 0.60,
    max_passes: int = 4,
) -> dict[str, tuple[float, float, float, float]]:
    adjusted = {zone_key: list(normalize_rect(rectangle)) for zone_key, rectangle in room_rectangles_local.items()}
    zone_keys = sorted(adjusted)

    for _pass in range(max_passes):
        changed = False
        for left_key, right_key in combinations(zone_keys, 2):
            left_rectangle = adjusted[left_key]
            right_rectangle = adjusted[right_key]

            x_overlap_m = interval_overlap_length(
                left_rectangle[0],
                left_rectangle[2],
                right_rectangle[0],
                right_rectangle[2],
            )
            if x_overlap_m >= shared_overlap_min_m:
                if 0.0 < right_rectangle[1] - left_rectangle[3] <= shared_wall_gap_max_m:
                    midpoint_y = (left_rectangle[3] + right_rectangle[1]) / 2.0
                    if abs(left_rectangle[3] - midpoint_y) > 1e-9 or abs(right_rectangle[1] - midpoint_y) > 1e-9:
                        left_rectangle[3] = midpoint_y
                        right_rectangle[1] = midpoint_y
                        changed = True
                elif 0.0 < left_rectangle[1] - right_rectangle[3] <= shared_wall_gap_max_m:
                    midpoint_y = (right_rectangle[3] + left_rectangle[1]) / 2.0
                    if abs(right_rectangle[3] - midpoint_y) > 1e-9 or abs(left_rectangle[1] - midpoint_y) > 1e-9:
                        right_rectangle[3] = midpoint_y
                        left_rectangle[1] = midpoint_y
                        changed = True

            y_overlap_m = interval_overlap_length(
                left_rectangle[1],
                left_rectangle[3],
                right_rectangle[1],
                right_rectangle[3],
            )
            if y_overlap_m >= shared_overlap_min_m:
                if 0.0 < right_rectangle[0] - left_rectangle[2] <= shared_wall_gap_max_m:
                    midpoint_x = (left_rectangle[2] + right_rectangle[0]) / 2.0
                    if abs(left_rectangle[2] - midpoint_x) > 1e-9 or abs(right_rectangle[0] - midpoint_x) > 1e-9:
                        left_rectangle[2] = midpoint_x
                        right_rectangle[0] = midpoint_x
                        changed = True
                elif 0.0 < left_rectangle[0] - right_rectangle[2] <= shared_wall_gap_max_m:
                    midpoint_x = (right_rectangle[2] + left_rectangle[0]) / 2.0
                    if abs(right_rectangle[2] - midpoint_x) > 1e-9 or abs(left_rectangle[0] - midpoint_x) > 1e-9:
                        right_rectangle[2] = midpoint_x
                        left_rectangle[0] = midpoint_x
                        changed = True

        if not changed:
            break

    return {
        zone_key: normalize_rect(tuple(values))
        for zone_key, values in adjusted.items()
    }


def load_apartment_a_geometry_policy(policy_path: Path | str | None = None) -> tuple[dict[str, object], Path]:
    resolved_policy_path = GUARD.assert_read_path(policy_path or DEFAULT_POLICY_PATH)
    payload = load_json_object(resolved_policy_path)
    return payload, resolved_policy_path


def _rounded_point_list(values: tuple[float, float] | None) -> list[float] | None:
    if values is None:
        return None
    return [round(values[0], 3), round(values[1], 3)]


def _rounded_rect_list(values: tuple[float, float, float, float] | None) -> list[float] | None:
    if values is None:
        return None
    return [round(value, 3) for value in normalize_rect(values)]


def _normalize_zone_rectangles(
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


def build_zone_rectangles_artifact(geometry_payload: dict[str, object]) -> dict[str, object]:
    zone_geometry_by_key = dict(geometry_payload.get("zone_geometry_by_key", {}))
    zones: list[dict[str, object]] = []
    for zone_key in sorted(zone_geometry_by_key):
        payload = dict(zone_geometry_by_key.get(zone_key, {}))
        zones.append(
            {
                "zone_key": zone_key,
                "source_zone_name": payload.get("source_zone_name"),
                "target_area_m2": payload.get("target_area_m2"),
                "footprint_area_m2": payload.get("footprint_area_m2"),
                "footprint_area_delta_m2": payload.get("footprint_area_delta_m2"),
                "footprint_rectangles_m": payload.get("footprint_rectangles_m", []),
                "selected_rectangles_local_m": payload.get("selected_rectangles_local_m", []),
                "clear_dimensions_m": payload.get("clear_dimensions_m"),
                "growth_anchor_local_m": payload.get("growth_anchor_local_m"),
            }
        )
    return {
        "geometry_mode": geometry_payload.get("geometry_mode"),
        "geometry_source": geometry_payload.get("geometry_source"),
        "geometry_policy_source": geometry_payload.get("geometry_policy_source"),
        "outer_block_rect_m": geometry_payload.get("outer_block_rect_m"),
        "export_origin_mode": geometry_payload.get("export_origin_mode"),
        "export_origin_offset_m": geometry_payload.get("export_origin_offset_m"),
        "zones": zones,
    }


def build_partition_summary(geometry_payload: dict[str, object]) -> dict[str, object]:
    zone_geometry_by_key = dict(geometry_payload.get("zone_geometry_by_key", {}))
    zone_targets = {
        zone_key: payload.get("target_area_m2")
        for zone_key, payload in sorted(zone_geometry_by_key.items())
        if isinstance(payload, dict)
    }
    zone_actuals = {
        zone_key: payload.get("footprint_area_m2")
        for zone_key, payload in sorted(zone_geometry_by_key.items())
        if isinstance(payload, dict)
    }
    return {
        "geometry_mode": geometry_payload.get("geometry_mode"),
        "geometry_source": geometry_payload.get("geometry_source"),
        "geometry_policy_source": geometry_payload.get("geometry_policy_source"),
        "outer_block_rect_m": geometry_payload.get("outer_block_rect_m"),
        "outer_record_handle": geometry_payload.get("outer_record_handle"),
        "outer_record_layer": geometry_payload.get("outer_record_layer"),
        "zone_count": len(zone_geometry_by_key),
        "zone_targets_m2": zone_targets,
        "zone_footprint_area_m2": zone_actuals,
        "single_block_validation": geometry_payload.get("single_block_validation", {}),
    }


def compact_zone_output_name(zone_key: str) -> str:
    normalized_key = str(zone_key).strip()
    compact_match = re.fullmatch(r"(PN|WC)_0?(\d{1,2})", normalized_key)
    if compact_match:
        return f"{compact_match.group(1)}{int(compact_match.group(2)):02d}"
    return normalized_key.replace("_", "")


def default_surface_zone_name(zone_key: str, zone_output_prefix: str = DEFAULT_ZONE_OUTPUT_PREFIX) -> str:
    normalized_key = str(zone_key).strip()
    if zone_output_prefix == "":
        return compact_zone_output_name(normalized_key)
    return f"{zone_output_prefix}{normalized_key}"


def resolve_human_ceiling_height_m(value: float | str | None) -> float:
    if value in {None, ""}:
        raise WorkspaceRuleError("Geometry build requires human-provided --ceiling-height-m.")
    ceiling_height_m = float(value)
    if ceiling_height_m <= 0.0:
        raise WorkspaceRuleError("--ceiling-height-m must be greater than 0.")
    return ceiling_height_m


def infer_apartment_a_geometry(
    *,
    mapping_payload: dict[str, object],
    policy_path: Path | str | None = None,
    ceiling_height_m: float | str | None = None,
    zone_output_prefix: str = DEFAULT_ZONE_OUTPUT_PREFIX,
) -> dict[str, object] | None:
    source_extract = normalize_relative_path(str(mapping_payload.get("source_extract", "") or ""))
    if not source_extract:
        return None

    source_extract_path = GUARD.assert_read_path(source_extract)
    parsed_extract = parse_dxf_extract_file(source_extract_path)
    filtered_records = list(parsed_extract.get("filtered_records", []))
    boundary_candidates = [
        dict(candidate)
        for candidate in list(mapping_payload.get("boundary_candidates", []))
        if isinstance(candidate, dict)
    ]
    apartment_extent_candidates = [
        dict(candidate)
        for candidate in list(mapping_payload.get("apartment_extent_candidates", []))
        if isinstance(candidate, dict)
    ]
    if not filtered_records:
        raise WorkspaceRuleError(
            f"DXF normalized extract does not contain filtered records: {workspace_path(source_extract_path)}"
        )

    geometry_policy, resolved_policy_path = load_apartment_a_geometry_policy(policy_path)
    ceiling_height_m = resolve_human_ceiling_height_m(ceiling_height_m)
    geometry_policy = dict(geometry_policy)
    geometry_policy["ceiling_height_m"] = round(ceiling_height_m, 3)
    geometry_policy["ceiling_height_source"] = "human_input"
    policy_geometry_mode = str(geometry_policy.get("geometry_mode", "") or "").strip()
    require_single_block = bool(geometry_policy.get("require_single_block", False))
    max_unallocated_area_m2 = float(geometry_policy.get("max_unallocated_area_m2", 0.0) or 0.0)
    area_priority_mode = str(geometry_policy.get("area_priority_mode", "") or "").strip()
    remembered_zone_targets_by_key = {
        canonical_zone_key(str(zone_key)): dict(payload)
        for zone_key, payload in dict(geometry_policy.get("remembered_zone_targets", {})).items()
        if isinstance(payload, dict)
    }

    room_anchor_points: list[tuple[float, float]] = []
    source_zone_name_by_key: dict[str, str] = {}
    zone_target_area_by_key: dict[str, float] = {}
    zone_anchor_world_by_key: dict[str, tuple[float, float]] = {}
    zone_boundary_handles_by_key: dict[str, list[str]] = {}
    zone_fragment_bboxes_world_by_key: dict[str, list[tuple[float, float, float, float]]] = {}
    duplicate_source_zone_names_by_key: dict[str, set[str]] = {}
    for zone in mapping_payload.get("candidate_zones", []):
        if not isinstance(zone, dict):
            continue
        source_zone_name = str(zone.get("zone_name", "")).strip()
        if not source_zone_name:
            continue
        source_zone_name = aliased_source_zone_name(source_zone_name, geometry_policy)
        zone_key = canonical_zone_key(source_zone_name)
        existing_source_zone_name = source_zone_name_by_key.get(zone_key)
        if existing_source_zone_name and existing_source_zone_name != source_zone_name:
            duplicate_source_zone_names_by_key.setdefault(zone_key, {existing_source_zone_name}).add(source_zone_name)
            continue
        source_zone_name_by_key[zone_key] = source_zone_name
        zone_target_area_by_key[zone_key] = float(zone.get("area_m2", 0.0) or 0.0)
        anchor_xy = zone.get("anchor_xy")
        if isinstance(anchor_xy, list) and len(anchor_xy) >= 2:
            anchor_xy_world = (float(anchor_xy[0]), float(anchor_xy[1]))
            room_anchor_points.append(anchor_xy_world)
            zone_anchor_world_by_key[zone_key] = anchor_xy_world
        boundary_handles = [
            str(handle or "").strip()
            for handle in list(zone.get("boundary_handles", []))
            if str(handle or "").strip()
        ]
        boundary_handle = str(zone.get("boundary_handle", "") or "").strip()
        if boundary_handle and boundary_handle not in boundary_handles:
            boundary_handles.append(boundary_handle)
        if boundary_handles:
            zone_boundary_handles_by_key[zone_key] = boundary_handles
        fragment_bboxes: list[tuple[float, float, float, float]] = []
        for bbox_xy in list(zone.get("boundary_fragment_bboxes_xy", [])):
            if isinstance(bbox_xy, list) and len(bbox_xy) == 4:
                fragment_bboxes.append(normalize_rect(tuple(float(value) for value in bbox_xy)))
        if fragment_bboxes:
            zone_fragment_bboxes_world_by_key[zone_key] = fragment_bboxes

    if duplicate_source_zone_names_by_key:
        collision_text = "; ".join(
            f"{zone_key}: {', '.join(sorted(source_zone_names))}"
            for zone_key, source_zone_names in sorted(duplicate_source_zone_names_by_key.items())
        )
        raise WorkspaceRuleError(
            "Duplicate canonical zone keys detected in mapping payload. "
            "Each room must resolve to a single canonical room code: "
            + collision_text
        )

    if not source_zone_name_by_key:
        return None

    records_by_handle = {record.handle: record for record in filtered_records if record.handle}

    def bbox_for_handle(*handles: str) -> tuple[float, float, float, float] | None:
        for handle in handles:
            record = records_by_handle.get(handle)
            if record is not None and record.bbox is not None:
                return tuple(float(value) for value in record.bbox)
        return None

    outer_boundary_candidate = sorted(apartment_extent_candidates, key=boundary_candidate_sort_key)[0] if apartment_extent_candidates else None
    outer_record = None if outer_boundary_candidate is not None else detect_outer_apartment_record(filtered_records, room_anchor_points)
    outer_bbox_values = list(outer_boundary_candidate.get("bbox_xy", [])) if outer_boundary_candidate is not None else []
    outer_bbox = tuple(float(value) for value in outer_bbox_values) if len(outer_bbox_values) == 4 else None
    if outer_bbox is None and outer_record is not None:
        outer_bbox = outer_record.bbox
    if outer_bbox is None:
        outer_bbox = room_boundary_union_bbox(boundary_candidates)
    if outer_bbox is None:
        fallback_bbox = bbox_for_handle("2528")
        if fallback_bbox is not None and bbox_contains_all_points(fallback_bbox, room_anchor_points):
            outer_bbox = fallback_bbox
    if outer_bbox is None:
        raise WorkspaceRuleError(
            "Could not determine the Apartment A outer block from the normalized DXF extract."
        )

    zone_anchor_local_by_key: dict[str, tuple[float, float]] = {}
    for zone in mapping_payload.get("candidate_zones", []):
        if not isinstance(zone, dict):
            continue
        source_zone_name = str(zone.get("zone_name", "")).strip()
        if not source_zone_name:
            continue
        source_zone_name = aliased_source_zone_name(source_zone_name, geometry_policy)
        zone_key = canonical_zone_key(source_zone_name)
        anchor_xy = zone.get("anchor_xy")
        if isinstance(anchor_xy, list) and len(anchor_xy) >= 2:
            zone_anchor_local_by_key[zone_key] = (
                (float(anchor_xy[0]) / 1000.0) - (float(outer_bbox[0]) / 1000.0),
                (float(anchor_xy[1]) / 1000.0) - (float(outer_bbox[1]) / 1000.0),
            )

    measured_outer_block_rect_m = normalize_rect(
        (
            float(outer_bbox[0]) / 1000.0,
            float(outer_bbox[1]) / 1000.0,
            float(outer_bbox[2]) / 1000.0,
            float(outer_bbox[3]) / 1000.0,
        )
    )
    outer_min_x_m, outer_min_y_m, outer_max_x_m, outer_max_y_m = measured_outer_block_rect_m
    export_origin_offset_xy_m = (outer_min_x_m, outer_min_y_m)
    outer_width_m = outer_max_x_m - outer_min_x_m
    outer_height_m = outer_max_y_m - outer_min_y_m
    outer_rectangle_local_m = (0.0, 0.0, outer_width_m, outer_height_m)
    (
        layer_boundary_rectangles_local_m_by_key,
        layer_boundary_polygons_local_m_by_key,
        layer_boundary_assignment_by_key,
    ) = assign_layer_room_boundary_rectangles_local(
        boundary_candidates=boundary_candidates,
        zone_anchor_world_by_key=zone_anchor_world_by_key,
        zone_boundary_handles_by_key=zone_boundary_handles_by_key,
        outer_bbox_mm=tuple(float(value) for value in outer_bbox),
    )
    for zone_key, fragment_bboxes_world_mm in zone_fragment_bboxes_world_by_key.items():
        if zone_key in layer_boundary_rectangles_local_m_by_key:
            continue
        fragment_rectangles_local_m = [
            normalize_rect(
                (
                    (bbox_xy[0] - float(outer_bbox[0])) / 1000.0,
                    (bbox_xy[1] - float(outer_bbox[1])) / 1000.0,
                    (bbox_xy[2] - float(outer_bbox[0])) / 1000.0,
                    (bbox_xy[3] - float(outer_bbox[1])) / 1000.0,
                )
            )
            for bbox_xy in fragment_bboxes_world_mm
        ]
        if not fragment_rectangles_local_m:
            continue
        layer_boundary_rectangles_local_m_by_key[zone_key] = merge_axis_aligned_rectangles(fragment_rectangles_local_m)
        layer_boundary_assignment_by_key[zone_key] = {
            "boundary_handle": (
                zone_boundary_handles_by_key.get(zone_key, [None])[0]
                if len(zone_boundary_handles_by_key.get(zone_key, [])) == 1
                else None
            ),
            "boundary_handles": zone_boundary_handles_by_key.get(zone_key, []),
            "boundary_rectangles_local_m": [
                [round(value, 3) for value in rectangle]
                for rectangle in layer_boundary_rectangles_local_m_by_key[zone_key]
            ],
            "assignment_mode": "mapping_boundary_fragment_bboxes",
        }

    geometry_mode = "dxf_inferred_dimension_grid_topology"
    raw_core_rectangles_local_m = infer_dimension_guided_zone_rectangles_local(
        outer_width_m=outer_width_m,
        outer_height_m=outer_height_m,
    )
    inferred_inner_bounds_local_m = infer_inner_dimension_bounds_local(filtered_records, outer_bbox)
    dimension_grid_local_m: dict[str, list[float]] = {"x": [], "y": []}
    refined_clear_rectangles_local_m: dict[str, tuple[float, float, float, float]] = {}
    corrected_named_room_rectangles_local_m: dict[str, tuple[float, float, float, float]] = {}

    if policy_geometry_mode == "backup_v2_single_block":
        geometry_mode = "dxf_inferred_backup_v2_single_block"
        reconciled_core_rectangles_local_m = reconcile_named_room_shared_walls_local(
            raw_core_rectangles_local_m,
            shared_wall_gap_max_m=0.40,
        )
        expanded_named_room_rectangles_local_m = expand_perimeter_room_rectangles_local(
            reconciled_core_rectangles_local_m,
            outer_rectangle_local_m,
            exterior_gap_max_m=0.50,
        )
        seed_named_room_rectangles_local_m = expanded_named_room_rectangles_local_m
    else:
        dimension_grid_local_m = build_dimension_coordinate_grid_local(
            filtered_records,
            outer_bbox,
            seed_rectangles_local=raw_core_rectangles_local_m,
        )
        refined_clear_rectangles_local_m = infer_named_zone_clear_rectangles_from_dimension_grid_local(
            zone_anchor_local_by_key=zone_anchor_local_by_key,
            zone_target_area_by_key=zone_target_area_by_key,
            outer_width_m=outer_width_m,
            outer_height_m=outer_height_m,
            dimension_grid_local=dimension_grid_local_m,
            seed_rectangles_local=raw_core_rectangles_local_m,
        )
        reconciled_core_rectangles_local_m = reconcile_named_room_shared_walls_local(
            refined_clear_rectangles_local_m,
            shared_wall_gap_max_m=0.25,
        )
        expanded_named_room_rectangles_local_m = expand_perimeter_room_rectangles_local(
            reconciled_core_rectangles_local_m,
            outer_rectangle_local_m,
            exterior_gap_max_m=0.40,
        )
        corrected_named_room_rectangles_local_m = correct_named_zone_rectangles_to_target_area(
            room_rectangles_local=expanded_named_room_rectangles_local_m,
            zone_target_area_by_key=zone_target_area_by_key,
            zone_anchor_local_by_key=zone_anchor_local_by_key,
            outer_rectangle_local_m=outer_rectangle_local_m,
        )
        seed_named_room_rectangles_local_m = corrected_named_room_rectangles_local_m

    seed_named_room_rectangles_local_m = {
        zone_key: rectangle
        for zone_key, rectangle in seed_named_room_rectangles_local_m.items()
        if zone_key in source_zone_name_by_key
    }

    backfill_missing_layer_boundary_zones(
        boundary_candidates=boundary_candidates,
        outer_bbox_mm=tuple(float(value) for value in outer_bbox),
        assigned_rectangles_by_key=layer_boundary_rectangles_local_m_by_key,
        assignment_debug_by_key=layer_boundary_assignment_by_key,
        source_zone_name_by_key=source_zone_name_by_key,
        zone_target_area_by_key=zone_target_area_by_key,
        reference_rectangles_local_by_key=seed_named_room_rectangles_local_m,
        remembered_zone_targets_by_key=remembered_zone_targets_by_key,
    )
    backfill_missing_layer_boundary_zones_by_area(
        boundary_candidates=boundary_candidates,
        outer_bbox_mm=tuple(float(value) for value in outer_bbox),
        assigned_rectangles_by_key=layer_boundary_rectangles_local_m_by_key,
        assignment_debug_by_key=layer_boundary_assignment_by_key,
        source_zone_name_by_key=source_zone_name_by_key,
        zone_target_area_by_key=zone_target_area_by_key,
        remembered_zone_targets_by_key=remembered_zone_targets_by_key,
    )

    if layer_boundary_rectangles_local_m_by_key:
        if len(layer_boundary_rectangles_local_m_by_key) == len(source_zone_name_by_key):
            geometry_mode = "dxf_inferred_s04_layer_boundary"
        else:
            geometry_mode = "dxf_inferred_s04_layer_boundary_fallback"

    zone_rectangles_local_m_by_key: dict[str, list[tuple[float, float, float, float]]] = {
        zone_key: [normalize_rect(rectangle) for rectangle in rectangles]
        for zone_key, rectangles in layer_boundary_rectangles_local_m_by_key.items()
    }
    zone_polygons_local_m_by_key: dict[str, list[list[tuple[float, float]]]] = {
        zone_key: [
            [(float(point[0]), float(point[1])) for point in polygon]
            for polygon in polygons
            if len(polygon) >= 3
        ]
        for zone_key, polygons in layer_boundary_polygons_local_m_by_key.items()
    }
    for zone_key, local_rectangle in seed_named_room_rectangles_local_m.items():
        zone_rectangles_local_m_by_key.setdefault(zone_key, [normalize_rect(local_rectangle)])

    unresolved_zone_keys = [
        zone_key
        for zone_key in sorted(source_zone_name_by_key)
        if zone_key not in zone_rectangles_local_m_by_key
    ]
    for unresolved_index, zone_key in enumerate(unresolved_zone_keys):
        free_cells_local_m = rectangle_grid_cells(
            outer_rectangle_local_m,
            [
                rectangle
                for rectangles in zone_rectangles_local_m_by_key.values()
                for rectangle in rectangles
            ],
        )
        selected_cells_local_m: list[tuple[float, float, float, float]] = []
        if policy_geometry_mode == "backup_v2_single_block":
            if zone_key == "PK_PB" or unresolved_index == len(unresolved_zone_keys) - 1:
                selected_cells_local_m = merge_axis_aligned_rectangles(free_cells_local_m)
            else:
                selected_cells_local_m = grow_zone_cells_to_target_area(
                    free_cells_local_m=free_cells_local_m,
                    anchor_xy_m=zone_anchor_local_by_key.get(zone_key),
                    target_area_m2=zone_target_area_by_key.get(zone_key, 0.0),
                )
        else:
            if zone_key == "PK_PB":
                selected_cells_local_m = select_partition_cells_without_holes(
                    free_cells_local_m=free_cells_local_m,
                    target_area_m2=zone_target_area_by_key.get(zone_key, 0.0),
                    anchor_xy_m=zone_anchor_local_by_key.get(zone_key),
                    outer_rectangle_local_m=outer_rectangle_local_m,
                    fixed_zone_rectangles_by_name={
                        other_zone_key: list(rectangles)
                        for other_zone_key, rectangles in zone_rectangles_local_m_by_key.items()
                        if other_zone_key != zone_key
                    },
                )
            candidate_growth_cells_local_m = free_cells_local_m
            if not selected_cells_local_m and zone_key == "PK_PB":
                candidate_growth_cells_local_m = filter_growth_cells_for_air_zone(
                    free_cells_local_m,
                    outer_rectangle_local_m,
                )
            if not selected_cells_local_m:
                selected_cells_local_m = grow_zone_cells_to_target_area(
                    free_cells_local_m=candidate_growth_cells_local_m,
                    anchor_xy_m=zone_anchor_local_by_key.get(zone_key),
                    target_area_m2=zone_target_area_by_key.get(zone_key, 0.0),
                )
            if not selected_cells_local_m and candidate_growth_cells_local_m != free_cells_local_m:
                selected_cells_local_m = grow_zone_cells_to_target_area(
                    free_cells_local_m=free_cells_local_m,
                    anchor_xy_m=zone_anchor_local_by_key.get(zone_key),
                    target_area_m2=zone_target_area_by_key.get(zone_key, 0.0),
                )
        if not selected_cells_local_m:
            continue
        zone_rectangles_local_m_by_key[zone_key] = merge_axis_aligned_rectangles(selected_cells_local_m)

    zone_merge_source_to_target_key = apply_zone_merge_groups(
        geometry_policy=geometry_policy,
        source_zone_name_by_key=source_zone_name_by_key,
        zone_target_area_by_key=zone_target_area_by_key,
        zone_anchor_world_by_key=zone_anchor_world_by_key,
        zone_anchor_local_by_key=zone_anchor_local_by_key,
        zone_rectangles_local_m_by_key=zone_rectangles_local_m_by_key,
        layer_boundary_assignment_by_key=layer_boundary_assignment_by_key,
    )

    missing_zone_keys = [
        zone_key
        for zone_key in sorted(source_zone_name_by_key)
        if zone_key not in zone_rectangles_local_m_by_key
    ]
    if missing_zone_keys:
        raise WorkspaceRuleError(
            "Could not infer local geometry for Apartment A zones: " + ", ".join(missing_zone_keys)
        )

    single_block_validation = summarize_single_block_partition(
        outer_rectangle_local_m=outer_rectangle_local_m,
        zone_rectangles_local_m_by_key=zone_rectangles_local_m_by_key,
    )
    if require_single_block:
        if not bool(single_block_validation.get("zone_graph_connected")):
            raise WorkspaceRuleError("Apartment A geometry policy violation: zone partition is not a single connected block.")
        if float(single_block_validation.get("unallocated_area_m2", 0.0) or 0.0) > max_unallocated_area_m2 + 1e-9:
            raise WorkspaceRuleError(
                "Apartment A geometry policy violation: unallocated area inside outer block exceeds tolerance "
                f"({single_block_validation.get('unallocated_area_m2')} m2 > {max_unallocated_area_m2} m2)."
            )

    zone_rectangles_m_by_key: dict[str, list[tuple[float, float, float, float]]] = {}
    zone_polygons_m_by_key: dict[str, list[list[tuple[float, float]]]] = {}
    zone_geometry_by_key: dict[str, dict[str, object]] = {}
    zone_area_actual_m2_by_key: dict[str, float] = {}

    for zone_key, local_rectangles in zone_rectangles_local_m_by_key.items():
        source_zone_name = source_zone_name_by_key.get(zone_key, "")
        world_rectangles: list[tuple[float, float, float, float]] = []
        for local_rectangle in local_rectangles:
            x1_local, y1_local, x2_local, y2_local = normalize_rect(local_rectangle)
            world_rectangles.append(
                normalize_rect(
                    (
                        outer_min_x_m + x1_local,
                        outer_min_y_m + y1_local,
                        outer_min_x_m + x2_local,
                        outer_min_y_m + y2_local,
                    )
                )
            )
        zone_rectangles_m_by_key[zone_key] = world_rectangles
        world_polygons: list[list[tuple[float, float]]] = []
        for local_polygon in zone_polygons_local_m_by_key.get(zone_key, []):
            world_polygon = [
                (outer_min_x_m + float(point[0]), outer_min_y_m + float(point[1]))
                for point in local_polygon
            ]
            if len(world_polygon) >= 3 and abs(polygon_area_xy(world_polygon)) > 1e-9:
                world_polygons.append(world_polygon)
        if world_polygons:
            zone_polygons_m_by_key[zone_key] = world_polygons
            zone_area_actual_m2_by_key[zone_key] = sum(abs(polygon_area_xy(polygon)) for polygon in world_polygons)
        else:
            zone_area_actual_m2_by_key[zone_key] = rectangles_area_m2(world_rectangles)

        raw_core_local_rectangle = raw_core_rectangles_local_m.get(zone_key)
        refined_clear_local_rectangle = refined_clear_rectangles_local_m.get(zone_key)
        reconciled_core_local_rectangle = reconciled_core_rectangles_local_m.get(zone_key)
        expanded_room_local_rectangle = expanded_named_room_rectangles_local_m.get(zone_key)
        corrected_room_local_rectangle = corrected_named_room_rectangles_local_m.get(zone_key)
        selected_local_rectangles = zone_rectangles_local_m_by_key.get(zone_key, [])
        target_area_m2 = zone_target_area_by_key.get(zone_key, 0.0)
        footprint_area_delta_m2 = zone_area_actual_m2_by_key[zone_key] - target_area_m2

        zone_payload: dict[str, object] = {
            "zone_key": zone_key,
            "source_zone_name": source_zone_name,
            "geometry_mode": geometry_mode,
            "geometry_source": workspace_path(source_extract_path),
            "geometry_upstream_source": normalize_relative_path(str(mapping_payload.get("upstream_source", "") or "")) or None,
            "geometry_source_kind": "normalized_dxf_extract",
            "geometry_policy_mode": policy_geometry_mode or None,
            "geometry_policy_source": workspace_path(resolved_policy_path),
            "area_priority_mode": area_priority_mode or None,
            "single_block_required": require_single_block,
            "outer_block_rect_m": [round(value, 3) for value in measured_outer_block_rect_m],
            "outer_record_handle": (
                outer_record.handle if outer_record is not None else str((outer_boundary_candidate or {}).get("handle", "2528"))
            ),
            "outer_record_layer": (
                outer_record.layer if outer_record is not None else str((outer_boundary_candidate or {}).get("source_layer", "0"))
            ),
            "inferred_inner_bounds_local_m": {key: round(value, 3) for key, value in inferred_inner_bounds_local_m.items()},
            "target_area_m2": round(target_area_m2, 3),
            "footprint_area_m2": round(zone_area_actual_m2_by_key[zone_key], 3),
            "footprint_area_delta_m2": round(footprint_area_delta_m2, 3),
            "area_crosscheck_pass": abs(footprint_area_delta_m2) <= 0.05,
            "area_crosscheck_required": area_priority_mode != "remember_only",
            "footprint_rectangles_m": [
                [round(value, 3) for value in rect]
                for rect in world_rectangles
            ],
        }
        if world_polygons:
            zone_payload["footprint_polygons_m"] = [
                [[round(point[0], 3), round(point[1], 3)] for point in polygon]
                for polygon in world_polygons
            ]
        if zone_key in layer_boundary_assignment_by_key:
            zone_payload["layer_boundary_assignment"] = layer_boundary_assignment_by_key[zone_key]
        if dimension_grid_local_m.get("x") or dimension_grid_local_m.get("y"):
            zone_payload["dimension_grid_local_m"] = {
                "x": [round(value, 3) for value in dimension_grid_local_m.get("x", [])],
                "y": [round(value, 3) for value in dimension_grid_local_m.get("y", [])],
            }
        if raw_core_local_rectangle is not None:
            zone_payload["raw_core_dimension_rectangle_local_m"] = _rounded_rect_list(raw_core_local_rectangle)
        if refined_clear_local_rectangle is not None:
            zone_payload["dimension_refined_clear_rectangle_local_m"] = _rounded_rect_list(refined_clear_local_rectangle)
        if reconciled_core_local_rectangle is not None:
            zone_payload["reconciled_core_rectangle_local_m"] = _rounded_rect_list(reconciled_core_local_rectangle)
        if expanded_room_local_rectangle is not None:
            zone_payload["expanded_room_rectangle_local_m"] = _rounded_rect_list(expanded_room_local_rectangle)
        if corrected_room_local_rectangle is not None:
            zone_payload["area_corrected_rectangle_local_m"] = _rounded_rect_list(corrected_room_local_rectangle)
        if selected_local_rectangles:
            zone_payload["selected_rectangles_local_m"] = [
                _rounded_rect_list(rectangle)
                for rectangle in selected_local_rectangles
            ]
            if len(selected_local_rectangles) == 1:
                selected_rectangle = normalize_rect(selected_local_rectangles[0])
                zone_payload["clear_dimensions_m"] = {
                    "width_m": round(selected_rectangle[2] - selected_rectangle[0], 3),
                    "depth_m": round(selected_rectangle[3] - selected_rectangle[1], 3),
                }
        selected_local_polygons = zone_polygons_local_m_by_key.get(zone_key, [])
        if selected_local_polygons:
            zone_payload["selected_polygons_local_m"] = [
                [[round(point[0], 3), round(point[1], 3)] for point in polygon]
                for polygon in selected_local_polygons
            ]
        if zone_key == "PK_PB":
            zone_payload["growth_anchor_local_m"] = _rounded_point_list(zone_anchor_local_by_key.get(zone_key))
            zone_payload["allocated_fragment_count"] = len(world_rectangles)
        zone_geometry_by_key[zone_key] = zone_payload

    area_crosscheck_failures = [
        f"{zone_key}: target={payload.get('target_area_m2')} actual={payload.get('footprint_area_m2')} delta={payload.get('footprint_area_delta_m2')}"
        for zone_key, payload in zone_geometry_by_key.items()
        if bool(payload.get("area_crosscheck_required")) and not bool(payload.get("area_crosscheck_pass"))
    ]
    if area_crosscheck_failures:
        raise WorkspaceRuleError(
            "Zone area cross-check failed after geometry inference: " + "; ".join(area_crosscheck_failures)
        )

    geometry_payload: dict[str, object] = {
        "geometry_mode": geometry_mode,
        "geometry_source": workspace_path(source_extract_path),
        "geometry_upstream_source": normalize_relative_path(str(mapping_payload.get("upstream_source", "") or "")) or None,
        "geometry_source_kind": "normalized_dxf_extract",
        "geometry_policy": geometry_policy,
        "geometry_policy_source": workspace_path(resolved_policy_path),
        "single_block_validation": single_block_validation,
        "zone_name_aliases": dict(geometry_policy.get("zone_name_aliases", {})),
        "zone_merge_source_to_target_key": zone_merge_source_to_target_key,
        "zone_merge_target_names": zone_merge_target_names(geometry_policy),
        "source_zone_name_by_key": source_zone_name_by_key,
        "zone_output_name_by_key": {
            zone_key: default_surface_zone_name(zone_key, zone_output_prefix)
            for zone_key in sorted(source_zone_name_by_key)
        },
        "zone_height_m_by_key": {
            zone_key: round(ceiling_height_m, 3)
            for zone_key in sorted(source_zone_name_by_key)
        },
        "zone_target_area_by_key": {
            zone_key: round(value, 3)
            for zone_key, value in sorted(zone_target_area_by_key.items())
        },
        "boundary_candidates": boundary_candidates,
        "apartment_extent_candidates": apartment_extent_candidates,
        "outer_boundary_candidate": outer_boundary_candidate,
        "layer_boundary_assignment_by_key": layer_boundary_assignment_by_key,
        "zone_anchor_local_m_by_key": {
            zone_key: _rounded_point_list(anchor_xy_m)
            for zone_key, anchor_xy_m in sorted(zone_anchor_local_by_key.items())
        },
        "outer_block_bbox_mm": [round(float(value), 3) for value in outer_bbox],
        "outer_block_rect_m": [round(value, 3) for value in measured_outer_block_rect_m],
        "outer_rectangle_local_m": [0.0, 0.0, round(outer_width_m, 3), round(outer_height_m, 3)],
        "outer_record_handle": (
            outer_record.handle if outer_record is not None else str((outer_boundary_candidate or {}).get("handle", "2528"))
        ),
        "outer_record_layer": (
            outer_record.layer if outer_record is not None else str((outer_boundary_candidate or {}).get("source_layer", "0"))
        ),
        "inferred_inner_bounds_local_m": {key: round(value, 3) for key, value in inferred_inner_bounds_local_m.items()},
        "dimension_grid_local_m": {
            "x": [round(value, 3) for value in dimension_grid_local_m.get("x", [])],
            "y": [round(value, 3) for value in dimension_grid_local_m.get("y", [])],
        },
        "zone_rectangles_local_m_by_key": {
            zone_key: [[round(value, 3) for value in rectangle] for rectangle in rectangles]
            for zone_key, rectangles in sorted(zone_rectangles_local_m_by_key.items())
        },
        "zone_polygons_local_m_by_key": {
            zone_key: [
                [[round(point[0], 3), round(point[1], 3)] for point in polygon]
                for polygon in polygons
            ]
            for zone_key, polygons in sorted(zone_polygons_local_m_by_key.items())
        },
        "zone_rectangles_m_by_key": {
            zone_key: [[round(value, 3) for value in rectangle] for rectangle in rectangles]
            for zone_key, rectangles in sorted(zone_rectangles_m_by_key.items())
        },
        "zone_polygons_m_by_key": {
            zone_key: [
                [[round(point[0], 3), round(point[1], 3)] for point in polygon]
                for polygon in polygons
            ]
            for zone_key, polygons in sorted(zone_polygons_m_by_key.items())
        },
        "zone_geometry_by_key": zone_geometry_by_key,
        "zone_area_actual_m2_by_key": {
            zone_key: round(area_m2, 3)
            for zone_key, area_m2 in sorted(zone_area_actual_m2_by_key.items())
        },
        "export_origin_mode": "outer_block_min_corner_to_zero",
        "export_origin_offset_m": [
            round(export_origin_offset_xy_m[0], 3),
            round(export_origin_offset_xy_m[1], 3),
            0.0,
        ],
        "export_origin_reference": (
            "Lower-left corner of the outer apartment block in plan; current Apartment A maps this to the non-adjacent lower-left corner of PN02."
        ),
    }
    geometry_payload["zone_rectangles"] = build_zone_rectangles_artifact(geometry_payload)
    geometry_payload["partition_summary"] = build_partition_summary(geometry_payload)
    return geometry_payload


def write_geometry_outputs(
    geometry_payload: dict[str, object],
    *,
    output_dir: Path | str | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    resolved_output_dir = GUARD.resolve(output_dir or _resolve_default_output_dir(path_resolver.resolve_project_id(project_id)))
    if project_id is not None:
        path_resolver.assert_output_in_project_scope(path_resolver.resolve_project_id(project_id), resolved_output_dir)
    geometry_paths = {
        "geometry_payload": resolved_output_dir / "geometry_payload.json",
        "zone_rectangles": resolved_output_dir / "zone_rectangles.json",
        "partition_summary": resolved_output_dir / "partition_summary.json",
    }

    GUARD.write_json(
        geometry_paths["geometry_payload"],
        geometry_payload,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    GUARD.write_json(
        geometry_paths["zone_rectangles"],
        build_zone_rectangles_artifact(geometry_payload),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    GUARD.write_json(
        geometry_paths["partition_summary"],
        build_partition_summary(geometry_payload),
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )
    return {key: workspace_path(path) for key, path in geometry_paths.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Infer Apartment A geometry from mapping payload and policy.")
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--mapping-payload",
        default=None,
        help="Path to mapping_payload.json. If omitted, resolves from 5_output/<project_id>/intermediate/mapping.",
    )
    parser.add_argument(
        "--policy",
        default=str(DEFAULT_POLICY_PATH.relative_to(ROOT)),
        help="Path to apartment_a_geometry_policy.json.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for geometry artifacts. If omitted, defaults to 5_output/<project_id>/intermediate/geometry.",
    )
    parser.add_argument(
        "--ceiling-height-m",
        type=float,
        required=True,
        help="Human-provided zone/model ceiling height in meters for this geometry build.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)

    mapping_payload_path = args.mapping_payload or _resolve_default_mapping_payload(project_id)
    output_dir = args.output_dir or _resolve_default_output_dir(project_id)
    mapping_payload = load_json_object(mapping_payload_path)
    geometry_payload = infer_apartment_a_geometry(
        mapping_payload=mapping_payload,
        policy_path=args.policy,
        ceiling_height_m=args.ceiling_height_m,
    )
    if geometry_payload is None:
        raise WorkspaceRuleError("Geometry inference requires a mapping payload with source_extract and candidate zones.")
    written_paths = write_geometry_outputs(geometry_payload, output_dir=output_dir, project_id=project_id)
    partition_summary = build_partition_summary(geometry_payload)
    single_block_validation = dict(partition_summary.get("single_block_validation", {}))

    print("GEOMETRY_INFERENCE_OK")
    print(f"Mapping payload: {normalize_relative_path(str(mapping_payload_path))}")
    print(f"Geometry source: {geometry_payload.get('geometry_source')}")
    print(f"Geometry mode: {geometry_payload.get('geometry_mode')}")
    print(f"Output dir: {normalize_relative_path(str(output_dir))}")
    print(f"Geometry payload: {written_paths['geometry_payload']}")
    print(f"Zone rectangles: {written_paths['zone_rectangles']}")
    print(f"Partition summary: {written_paths['partition_summary']}")
    print(f"Zone count: {partition_summary.get('zone_count')}")
    print(f"Allocated area m2: {single_block_validation.get('allocated_area_m2')}")
    print(f"Unallocated area m2: {single_block_validation.get('unallocated_area_m2')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
