#!/usr/bin/env python3
"""
Build semantic mapping artifacts from normalized DXF context.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.dxf_raw_parser import (  # noqa: E402
    DEFAULT_DXF_LAYER_PROFILE,
    BlockDefinition,
    Record,
    classify_record_layer,
    first_group_code_value,
    load_layer_profile,
    numeric_group_code_value,
    record_matches_layer_roles,
    record_anchor_xy,
)
from schema_tools.schema_workbench import parse_dxf_extract_file  # noqa: E402
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError  # noqa: E402
from utils import path_resolver  # noqa: E402


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root

DEFAULT_DXF_EXTRACT = Path("5_output") / "<project_id>" / "normalized" / "dxf" / "<project>_filtered_extract.txt"
DEFAULT_OUTPUT_DIR = Path("5_output") / "<project_id>" / "intermediate" / "mapping"
DEFAULT_MAPPING_PAYLOAD_OUTPUT = DEFAULT_OUTPUT_DIR / "mapping_payload.json"
DEFAULT_LAYER_PROFILE_PATH = DEFAULT_DXF_LAYER_PROFILE

ROOM_PATTERNS = [
    re.compile(r"PK\s*\+\s*PB", re.IGNORECASE),
    re.compile(r"PN\s*0?1", re.IGNORECASE),
    re.compile(r"PN\s*0?2", re.IGNORECASE),
    re.compile(r"WC\s*0?1", re.IGNORECASE),
    re.compile(r"WC\s*0?2", re.IGNORECASE),
    re.compile(r"L(?:[ÔO偞])GIA", re.IGNORECASE),
]

TITLE_PATTERNS = [
    re.compile(r"CH[\s\-]?A", re.IGNORECASE),
    re.compile(r"CÄ‚N\s+Há»˜", re.IGNORECASE),
    re.compile(r"CAN\s+HO", re.IGNORECASE),
]

INSERT_LAYERS = {
    "TAC - Door window",
    "TAC - CUA+LC",
    "TAC - Betong",
}

OPENING_SIZE_PATTERN = re.compile(r"\b(\d{2,4})\s*[Xx×*]\s*(\d{2,4})\b")
OPENING_SIZE_FULL_PATTERN = re.compile(r"^\s*(\d{2,4})\s*[Xx×*]\s*(\d{2,4})\s*$")
OPENING_TYPE_CODE_PATTERN = re.compile(r"^(?:DN\d+|DWN|DG|SW|SN|LC\d+)$", re.IGNORECASE)
OPENING_SILL_PATTERN = re.compile(r"^\+\s*\d+(?:[.,]\d+)?$")

EXCLUDED_INSERT_NAME_PATTERNS = [
    re.compile(r"^_Dot$", re.IGNORECASE),
    re.compile(r"lavabo", re.IGNORECASE),
    re.compile(r"giuong", re.IGNORECASE),
    re.compile(r"DOUBLE-SINK", re.IGNORECASE),
    re.compile(r"maygiat", re.IGNORECASE),
    re.compile(r"hat1", re.IGNORECASE),
    re.compile(r"ref1", re.IGNORECASE),
    re.compile(r"THANG", re.IGNORECASE),
    re.compile(r"TRUC", re.IGNORECASE),
    re.compile(r"Section Callout", re.IGNORECASE),
    re.compile(r"KH_CLC", re.IGNORECASE),
]

GEOMETRY_RECORD_TYPES = {
    "LINE",
    "LWPOLYLINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "SPLINE",
}

OPENING_GEOMETRY_RECORD_TYPES = {
    "LWPOLYLINE",
    "POLYLINE",
    "CIRCLE",
    "ELLIPSE",
}

GEOMETRY_LAYERS = {
    "0",
    "TAC - Tuong",
    "TAC - Lop hoan thien",
    "TAC - Door window",
    "TAC - CUA+LC",
    "TAC - Betong",
    "TAC - Thay",
}

OPENING_PLACEMENT_SEGMENT_LAYERS = {
    "TAC - Thay",
    "TAC - Lop hoan thien",
    "TAC - Tuong",
}

OPENING_GEOMETRY_MATCH_DISTANCE_MM = 1200.0


def workspace_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def normalize_relative_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/")


def _resolve_single_match(directory: Path, patterns: list[str], *, label: str) -> Path:
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise WorkspaceRuleError(
                f"Multiple {label} candidates found in {workspace_path(directory)} for pattern '{pattern}'."
            )
    raise WorkspaceRuleError(f"No {label} found in {workspace_path(directory)}.")


def _resolve_default_dxf_extract(project_id: str) -> Path:
    output_dir = path_resolver.resolve_output_dir_for_read(project_id, "normalized/dxf")
    if output_dir is None:
        raise WorkspaceRuleError(f"No normalized DXF output directory found for project '{project_id}'.")
    return _resolve_single_match(output_dir, ["*_filtered_extract.txt", "*.txt"], label="normalized DXF extract")


def _resolve_default_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/mapping")


def parse_decimal_text(value: str) -> float | None:
    cleaned = str(value or "").strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def squared_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2) + ((a[1] - b[1]) ** 2)


def centroid_xy(items: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not items:
        return None
    return (
        sum(item[0] for item in items) / len(items),
        sum(item[1] for item in items) / len(items),
    )


def strip_mtext_formatting(text: str) -> str:
    cleaned = str(text or "").replace("\\P", "\n")
    cleaned = re.sub(r"\\f[^;]*;", "", cleaned)
    cleaned = re.sub(r"\\H[^;]*;", "", cleaned)
    cleaned = re.sub(r"\\A\d+;", "", cleaned)
    cleaned = re.sub(r"\\[A-Za-z][^;]*;", "", cleaned)
    cleaned = cleaned.replace("{", "").replace("}", "")
    cleaned = cleaned.replace("\\", "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def normalize_opening_text(value: str) -> str:
    normalized = strip_mtext_formatting(value)
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = normalized.replace("×", "X").replace("*", "X")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def extract_opening_size_components(text: str) -> tuple[int, int, str] | None:
    normalized = normalize_opening_text(text)
    match = OPENING_SIZE_PATTERN.search(normalized)
    if match is None:
        return None
    width_mm = int(match.group(1))
    height_mm = int(match.group(2))
    return width_mm, height_mm, f"{width_mm}X{height_mm}"


def matches_patterns(text: str, patterns: list[re.Pattern[str]]) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in patterns)


def counter_to_sorted_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter)}


def role_names_from_profile(layer_profile: dict[str, object], key: str) -> set[str]:
    return {str(value) for value in list(layer_profile.get(key, []))}


def parse_json_metadata_value(value: str, default: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def confidence_sort_key(value: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(value or "").strip().lower(), 99)


def candidate_anchor_for_dedupe(candidate: dict[str, object]) -> tuple[float, float] | None:
    anchor_xy = candidate.get("anchor_xy")
    if isinstance(anchor_xy, list) and len(anchor_xy) >= 2:
        return (float(anchor_xy[0]), float(anchor_xy[1]))
    return None


def candidate_area_for_dedupe(candidate: dict[str, object]) -> float | None:
    area_m2 = candidate.get("area_m2")
    if area_m2 in {None, ""}:
        return None
    return float(area_m2)


def same_physical_room_candidate(first: dict[str, object], second: dict[str, object]) -> bool:
    first_handle = str(first.get("label_handle", "") or "").strip()
    second_handle = str(second.get("label_handle", "") or "").strip()
    if first_handle and first_handle == second_handle:
        return True

    first_anchor = candidate_anchor_for_dedupe(first)
    second_anchor = candidate_anchor_for_dedupe(second)
    if first_anchor is not None and second_anchor is not None:
        return squared_distance(first_anchor, second_anchor) <= 250.0**2

    first_text = str(first.get("source_text", "") or "").strip()
    second_text = str(second.get("source_text", "") or "").strip()
    first_area = candidate_area_for_dedupe(first)
    second_area = candidate_area_for_dedupe(second)
    return bool(first_text and first_text == second_text and first_area == second_area)


def spatial_dedupe_sort_key(candidate: dict[str, object]) -> tuple[float, float, str]:
    anchor = candidate_anchor_for_dedupe(candidate)
    if anchor is None:
        return (0.0, 0.0, str(candidate.get("label_handle", "")))
    return (-anchor[1], anchor[0], str(candidate.get("label_handle", "")))


def ranked_room_candidate(items: list[dict[str, object]]) -> dict[str, object]:
    ranked = sorted(
        items,
        key=lambda item: (
            confidence_sort_key(str(item.get("candidate_confidence", ""))),
            0 if item.get("area_m2") is not None else 1,
            0 if item.get("anchor_xy") is not None else 1,
            str(item.get("label_handle", "")),
        ),
    )
    winner = dict(ranked[0])
    winner["source_candidate_count"] = len(items)
    winner["source_handles"] = [str(item.get("label_handle", "")) for item in items if item.get("label_handle")]
    return winner


def suffix_duplicate_room_candidate(candidate: dict[str, object], *, zone_key: str, index: int) -> dict[str, object]:
    resolved = dict(candidate)
    source_zone_name = str(resolved.get("zone_name", "") or default_source_zone_name(zone_key)).strip()
    resolved.setdefault("original_zone_name", source_zone_name)
    resolved.setdefault("original_zone_key", zone_key)
    resolved["zone_name"] = f"{source_zone_name} {index:02d}"
    resolved["canonical_text"] = resolved["zone_name"]
    resolved["zone_key"] = f"{zone_key}_{index:02d}"
    return resolved


def deduplicate_room_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for candidate in candidates:
        zone_key = str(candidate.get("zone_key", "") or "").strip()
        if not zone_key:
            continue
        grouped.setdefault(zone_key, []).append(candidate)

    resolved: list[dict[str, object]] = []
    for zone_key, items in grouped.items():
        clusters: list[list[dict[str, object]]] = []
        for item in sorted(items, key=spatial_dedupe_sort_key):
            matching_cluster = next(
                (
                    cluster
                    for cluster in clusters
                    if any(same_physical_room_candidate(item, existing_item) for existing_item in cluster)
                ),
                None,
            )
            if matching_cluster is None:
                clusters.append([item])
            else:
                matching_cluster.append(item)
        if len(clusters) == 1:
            resolved.append(ranked_room_candidate(clusters[0]))
            continue
        for index, cluster in enumerate(clusters, start=1):
            resolved.append(
                suffix_duplicate_room_candidate(
                    ranked_room_candidate(cluster),
                    zone_key=zone_key,
                    index=index,
                )
            )
    return sorted(resolved, key=lambda item: str(item.get("zone_key", "")))


def load_csv_dict_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{key: value or "" for key, value in row.items()} for row in reader]


def flatten_block_records(block_defs: list[BlockDefinition]) -> list[Record]:
    records: list[Record] = []
    for block_def in block_defs:
        records.extend(block_def.records)
    return records


def block_definition_bbox_lookup(
    block_defs: list[BlockDefinition],
) -> dict[str, tuple[float, float, float, float]]:
    lookup: dict[str, tuple[float, float, float, float]] = {}
    for block_def in block_defs:
        block_points: list[tuple[float, float, float]] = []
        for record in block_def.records:
            block_points.extend(record.points)
        if not block_points:
            continue
        xs = [point[0] for point in block_points]
        ys = [point[1] for point in block_points]
        lookup[str(block_def.name)] = (min(xs), min(ys), max(xs), max(ys))
    return lookup


def extract_opening_placement_segments(
    records: list[Record],
    layer_profile: dict[str, object],
) -> list[dict[str, object]]:
    opening_segment_roles = {
        "external_wall",
        "internal_wall",
        "partition",
        "wall_boundary_fallback",
        "room_boundary",
        "apartment_boundary",
    }
    segments: list[dict[str, object]] = []
    axis_tolerance_mm = 10.0
    for record in records:
        if record.record_type not in {"LINE", "LWPOLYLINE"}:
            continue
        if not record_matches_layer_roles(record, layer_profile, opening_segment_roles):
            continue
        if len(record.points) < 2:
            continue
        points = list(record.points)
        closed_flag = first_group_code_value(record, "70")
        if record.record_type == "LWPOLYLINE" and closed_flag not in {None, ""}:
            try:
                if int(float(closed_flag)) & 1 and points[0] != points[-1]:
                    points.append(points[0])
            except (TypeError, ValueError):
                pass
        for segment_index in range(len(points) - 1):
            x1, y1, _ = points[segment_index]
            x2, y2, _ = points[segment_index + 1]
            if abs(y1 - y2) <= axis_tolerance_mm:
                axis = "horizontal"
                fixed_coord_mm = (y1 + y2) / 2.0
                interval_min_mm = min(x1, x2)
                interval_max_mm = max(x1, x2)
            elif abs(x1 - x2) <= axis_tolerance_mm:
                axis = "vertical"
                fixed_coord_mm = (x1 + x2) / 2.0
                interval_min_mm = min(y1, y2)
                interval_max_mm = max(y1, y2)
            else:
                continue
            segments.append(
                {
                    "record_handle": record.handle,
                    "record_type": record.record_type,
                    "layer": record.layer,
                    "segment_index": segment_index,
                    "axis": axis,
                    "fixed_coord_mm": round(float(fixed_coord_mm), 3),
                    "interval_min_mm": round(float(interval_min_mm), 3),
                    "interval_max_mm": round(float(interval_max_mm), 3),
                }
            )
    segments.sort(
        key=lambda item: (
            str(item.get("layer", "")),
            str(item.get("record_handle", "")),
            str(item.get("axis", "")),
            float(item.get("fixed_coord_mm", 0.0) or 0.0),
            float(item.get("interval_min_mm", 0.0) or 0.0),
            float(item.get("interval_max_mm", 0.0) or 0.0),
            int(item.get("segment_index", 0) or 0),
        )
    )
    return segments


def parse_extract_metadata_map(metadata_rows: list[dict[str, str]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in metadata_rows:
        key = str(row.get("key", "")).strip()
        if not key:
            continue
        lookup[key] = str(row.get("value", "")).strip()
    return lookup


def parse_counter_text(value: str) -> dict[str, int]:
    text = str(value or "").strip()
    if not text:
        return {}
    counter: Counter[str] = Counter()
    for item in text.split(","):
        key, _separator, count_text = item.partition("=")
        key = key.strip()
        if not key:
            continue
        try:
            counter[key] = int(count_text.strip() or "0")
        except ValueError:
            continue
    return counter_to_sorted_dict(counter)


def parse_selection_bbox_text(value: str) -> list[float] | None:
    parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if len(parts) != 4:
        return None
    try:
        return [float(part) for part in parts]
    except ValueError:
        return None


def confidence_for_zone_candidate(anchor_xy: list[float] | None, area_m2: float | None) -> str:
    if anchor_xy is not None and area_m2 is not None:
        return "high"
    if anchor_xy is not None:
        return "medium"
    return "low"


def confidence_for_dimension_annotation(value_mm: int | None, anchor_xy: list[float] | None) -> str:
    if value_mm is not None and anchor_xy is not None:
        return "high"
    if value_mm is not None:
        return "medium"
    return "low"


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
        "LOGIA": "LOGIA",
        "LGIA": "LOGIA",
    }.get(token, token)

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


def normalized_zone_name_aliases(zone_name_aliases: dict[str, object] | None) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source_name, target_name in dict(zone_name_aliases or {}).items():
        normalized_source_key = canonical_zone_key(str(source_name))
        normalized_target_name = str(target_name).strip()
        if normalized_source_key and normalized_target_name:
            aliases[normalized_source_key] = normalized_target_name
    return aliases


def aliased_zone_name(source_zone_name: str, zone_name_aliases: dict[str, object] | None) -> str:
    aliases = normalized_zone_name_aliases(zone_name_aliases)
    source_text = str(source_zone_name or "").strip()
    if not source_text:
        return source_text
    source_parts = [part.strip() for part in re.split(r"\s*\+\s*", source_text) if part.strip()]
    if len(source_parts) > 1:
        return " + ".join(aliases.get(canonical_zone_key(part), part) for part in source_parts)
    source_zone_key = canonical_zone_key(source_text)
    return aliases.get(source_zone_key, source_text)


def apply_zone_name_aliases_to_zone_candidate(
    zone: dict[str, object],
    zone_name_aliases: dict[str, object] | None,
) -> dict[str, object]:
    zone_payload = dict(zone)
    source_zone_name = str(zone_payload.get("zone_name", "")).strip()
    source_zone_key = canonical_zone_key(source_zone_name)
    aliased_name = aliased_zone_name(source_zone_name, zone_name_aliases)
    if aliased_name and aliased_name != source_zone_name:
        zone_payload.setdefault("original_zone_name", source_zone_name)
        zone_payload.setdefault("original_zone_key", source_zone_key)
        zone_payload["zone_name"] = aliased_name
        zone_payload["canonical_text"] = aliased_name
        zone_payload["zone_key"] = canonical_zone_key(aliased_name)
        contained_zone_texts = list(zone_payload.get("contained_zone_texts", []))
        if contained_zone_texts:
            zone_payload["contained_zone_texts"] = [
                aliased_zone_name(str(text), zone_name_aliases)
                for text in contained_zone_texts
            ]
    return zone_payload


def apply_zone_name_aliases_to_mapping_payload(
    mapping_payload: dict[str, object],
    zone_name_aliases: dict[str, object] | None,
) -> dict[str, object]:
    aliases = normalized_zone_name_aliases(zone_name_aliases)
    if not aliases:
        return mapping_payload

    resolved_payload = dict(mapping_payload)
    candidate_zones: list[dict[str, object]] = []
    for zone in list(mapping_payload.get("candidate_zones", [])):
        if not isinstance(zone, dict):
            continue
        candidate_zones.append(apply_zone_name_aliases_to_zone_candidate(zone, zone_name_aliases))

    candidate_openings: list[dict[str, object]] = []
    for opening in list(mapping_payload.get("candidate_openings", [])):
        if not isinstance(opening, dict):
            continue
        opening_payload = dict(opening)
        nearest_zone_name = str(opening_payload.get("nearest_zone_name", "") or "").strip()
        nearest_zone_key = canonical_zone_key(nearest_zone_name)
        aliased_name = aliases.get(nearest_zone_key, "")
        if aliased_name:
            opening_payload.setdefault("original_nearest_zone_name", nearest_zone_name)
            opening_payload.setdefault("original_nearest_zone_key", nearest_zone_key)
            opening_payload["nearest_zone_name"] = aliased_name
            opening_payload["nearest_zone_key"] = canonical_zone_key(aliased_name)
        candidate_openings.append(opening_payload)

    resolved_payload["candidate_zones"] = candidate_zones
    resolved_payload["candidate_zone_count"] = len(candidate_zones)
    resolved_payload["candidate_openings"] = candidate_openings
    resolved_payload["candidate_opening_count"] = len(candidate_openings)
    resolved_payload["zone_name_aliases"] = dict(zone_name_aliases or {})
    return resolved_payload


def point_within_bbox(point_xy: tuple[float, float], bbox_xy: list[object]) -> bool:
    if len(bbox_xy) != 4:
        return False
    min_x, min_y, max_x, max_y = (float(value) for value in bbox_xy)
    point_x, point_y = point_xy
    return min_x - 1e-6 <= point_x <= max_x + 1e-6 and min_y - 1e-6 <= point_y <= max_y + 1e-6


def expanded_bbox_contains_point(
    point_xy: tuple[float, float],
    bbox_xy: list[object],
    *,
    tolerance_mm: float = 1e-6,
) -> bool:
    if len(bbox_xy) != 4:
        return False
    min_x, min_y, max_x, max_y = (float(value) for value in bbox_xy)
    point_x, point_y = point_xy
    return (
        min_x - tolerance_mm <= point_x <= max_x + tolerance_mm
        and min_y - tolerance_mm <= point_y <= max_y + tolerance_mm
    )


def bbox_intersects(
    first_bbox_xy: list[object],
    second_bbox_xy: list[object],
    *,
    tolerance_mm: float = 0.0,
) -> bool:
    if len(first_bbox_xy) != 4 or len(second_bbox_xy) != 4:
        return False
    first_min_x, first_min_y, first_max_x, first_max_y = (float(value) for value in first_bbox_xy)
    second_min_x, second_min_y, second_max_x, second_max_y = (float(value) for value in second_bbox_xy)
    return not (
        first_max_x < second_min_x - tolerance_mm
        or second_max_x < first_min_x - tolerance_mm
        or first_max_y < second_min_y - tolerance_mm
        or second_max_y < first_min_y - tolerance_mm
    )


def point_in_polygon(point_xy: tuple[float, float], points_xy: list[object]) -> bool:
    vertices: list[tuple[float, float]] = []
    for point in points_xy:
        if isinstance(point, list) and len(point) >= 2:
            vertices.append((float(point[0]), float(point[1])))
    if len(vertices) < 3:
        return False

    point_x, point_y = point_xy
    inside = False
    previous_x, previous_y = vertices[-1]
    for current_x, current_y in vertices:
        if ((current_y > point_y) != (previous_y > point_y)) and (
            point_x
            < (previous_x - current_x) * (point_y - current_y) / (previous_y - current_y + 1e-12)
            + current_x
        ):
            inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def boundary_contains_anchor(boundary_candidate: dict[str, object], anchor_xy: tuple[float, float]) -> bool:
    bbox_xy = list(boundary_candidate.get("bbox_xy", []))
    if not expanded_bbox_contains_point(anchor_xy, bbox_xy, tolerance_mm=5.0):
        return False
    points_xy = list(boundary_candidate.get("points_xy", []))
    if bool(boundary_candidate.get("closed_polyline")) and len(points_xy) >= 3:
        return point_in_polygon(anchor_xy, points_xy) or expanded_bbox_contains_point(anchor_xy, bbox_xy, tolerance_mm=5.0)
    return True


def candidate_anchor_tuple(candidate: dict[str, object]) -> tuple[float, float] | None:
    anchor_xy = candidate.get("anchor_xy")
    if isinstance(anchor_xy, list) and len(anchor_xy) >= 2:
        return (float(anchor_xy[0]), float(anchor_xy[1]))
    return None


def candidate_bbox_tuple(candidate: dict[str, object]) -> tuple[float, float, float, float] | None:
    bbox_xy = candidate.get("bbox_xy")
    if isinstance(bbox_xy, list) and len(bbox_xy) == 4:
        min_x, min_y, max_x, max_y = (float(value) for value in bbox_xy)
        return (min(min_x, max_x), min(min_y, max_y), max(min_x, max_x), max(min_y, max_y))
    return None


def room_boundary_area_mm2(candidate: dict[str, object]) -> float:
    bbox = candidate_bbox_tuple(candidate)
    if bbox is None:
        return 0.0
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def is_em_layer_candidate(candidate: dict[str, object]) -> bool:
    source_layer = str(candidate.get("source_layer", "") or "").strip().upper()
    canonical_layer = str(candidate.get("layer_canonical", "") or "").strip().upper()
    return source_layer.startswith("EM_") or canonical_layer.startswith("EM_")


def union_bbox(bboxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not bboxes:
        return None
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def room_boundary_indices_for_opening(
    opening_geometry_candidate: dict[str, object],
    room_boundaries: list[dict[str, object]],
    *,
    tolerance_mm: float = 50.0,
) -> list[int]:
    opening_bbox_tuple = candidate_bbox_tuple(opening_geometry_candidate)
    if opening_bbox_tuple is None:
        return []
    opening_min_x, opening_min_y, opening_max_x, opening_max_y = opening_bbox_tuple
    opening_width = opening_max_x - opening_min_x
    opening_height = opening_max_y - opening_min_y
    is_vertical = opening_height >= opening_width
    fixed_coord = (opening_min_x + opening_max_x) / 2.0 if is_vertical else (opening_min_y + opening_max_y) / 2.0
    opening_span_start = opening_min_y if is_vertical else opening_min_x
    opening_span_end = opening_max_y if is_vertical else opening_max_x
    opening_span_length = max(0.0, opening_span_end - opening_span_start)
    min_overlap_mm = min(200.0, max(25.0, opening_span_length * 0.20))

    matched_indices: list[int] = []
    for index, boundary_candidate in enumerate(room_boundaries):
        boundary_bbox = candidate_bbox_tuple(boundary_candidate)
        if boundary_bbox is None:
            continue
        boundary_min_x, boundary_min_y, boundary_max_x, boundary_max_y = boundary_bbox
        if is_vertical:
            fixed_match = boundary_min_x - tolerance_mm <= fixed_coord <= boundary_max_x + tolerance_mm
            boundary_span_start = boundary_min_y
            boundary_span_end = boundary_max_y
        else:
            fixed_match = boundary_min_y - tolerance_mm <= fixed_coord <= boundary_max_y + tolerance_mm
            boundary_span_start = boundary_min_x
            boundary_span_end = boundary_max_x
        overlap_length = max(0.0, min(opening_span_end, boundary_span_end) - max(opening_span_start, boundary_span_start))
        if fixed_match and overlap_length >= min_overlap_mm:
            matched_indices.append(index)
    return matched_indices


def split_boundary_label_fragments(
    *,
    boundary_index: int,
    boundary_candidate: dict[str, object],
    label_candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    boundary_bbox = candidate_bbox_tuple(boundary_candidate)
    if boundary_bbox is None or not label_candidates:
        return []

    min_x, min_y, max_x, max_y = boundary_bbox
    label_rows: list[dict[str, object]] = []
    for label_candidate in label_candidates:
        anchor = candidate_anchor_tuple(label_candidate)
        if anchor is None:
            anchor = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        zone_key = canonical_zone_key(str(label_candidate.get("zone_name", "") or label_candidate.get("zone_key", "")))
        if not zone_key:
            continue
        label_rows.append(
            {
                "label": dict(label_candidate),
                "zone_key": zone_key,
                "anchor": anchor,
                "area_m2": float(label_candidate.get("area_m2", 0.0) or 0.0),
            }
        )
    if not label_rows:
        return []

    boundary_handle = str(boundary_candidate.get("handle", "") or "").strip()
    if len(label_rows) == 1:
        row = label_rows[0]
        return [
            {
                "fragment_id": f"{boundary_index}:{row['zone_key']}",
                "boundary_index": boundary_index,
                "boundary_handle": boundary_handle,
                "label": row["label"],
                "zone_key": row["zone_key"],
                "bbox_xy": boundary_bbox,
                "anchor": row["anchor"],
                "area_m2": row["area_m2"],
            }
        ]

    anchor_x_values = [float(row["anchor"][0]) for row in label_rows]
    anchor_y_values = [float(row["anchor"][1]) for row in label_rows]
    split_axis = "y" if (max(anchor_y_values) - min(anchor_y_values)) >= (max(anchor_x_values) - min(anchor_x_values)) else "x"
    sorted_rows = sorted(
        label_rows,
        key=lambda row: (
            float(row["anchor"][1]) if split_axis == "y" else float(row["anchor"][0]),
            str(row["zone_key"]),
        ),
    )
    area_sum = sum(max(0.0, float(row["area_m2"])) for row in sorted_rows)
    if area_sum <= 1e-9:
        weights = [1.0 for _row in sorted_rows]
        area_sum = float(len(sorted_rows))
    else:
        weights = [max(0.0, float(row["area_m2"])) for row in sorted_rows]

    total_length = (max_y - min_y) if split_axis == "y" else (max_x - min_x)
    cursor = min_y if split_axis == "y" else min_x
    fragments: list[dict[str, object]] = []
    for index, row in enumerate(sorted_rows):
        if index == len(sorted_rows) - 1:
            next_cursor = max_y if split_axis == "y" else max_x
        else:
            next_cursor = cursor + total_length * (weights[index] / area_sum)
        if split_axis == "y":
            fragment_bbox = (min_x, cursor, max_x, next_cursor)
        else:
            fragment_bbox = (cursor, min_y, next_cursor, max_y)
        fragments.append(
            {
                "fragment_id": f"{boundary_index}:{row['zone_key']}",
                "boundary_index": boundary_index,
                "boundary_handle": boundary_handle,
                "label": row["label"],
                "zone_key": row["zone_key"],
                "bbox_xy": fragment_bbox,
                "anchor": row["anchor"],
                "area_m2": row["area_m2"],
            }
        )
        cursor = next_cursor
    return fragments


def opening_fragment_overlap_score(
    opening_geometry_candidate: dict[str, object],
    fragment: dict[str, object],
    *,
    tolerance_mm: float = 50.0,
) -> tuple[float, float]:
    opening_bbox = candidate_bbox_tuple(opening_geometry_candidate)
    fragment_bbox = fragment.get("bbox_xy")
    if opening_bbox is None or not isinstance(fragment_bbox, tuple) or len(fragment_bbox) != 4:
        return (0.0, math.inf)

    opening_min_x, opening_min_y, opening_max_x, opening_max_y = opening_bbox
    fragment_min_x, fragment_min_y, fragment_max_x, fragment_max_y = fragment_bbox
    opening_width = opening_max_x - opening_min_x
    opening_height = opening_max_y - opening_min_y
    is_vertical = opening_height >= opening_width
    if is_vertical:
        fixed_coord = (opening_min_x + opening_max_x) / 2.0
        fixed_match = fragment_min_x - tolerance_mm <= fixed_coord <= fragment_max_x + tolerance_mm
        overlap = max(0.0, min(opening_max_y, fragment_max_y) - max(opening_min_y, fragment_min_y))
    else:
        fixed_coord = (opening_min_y + opening_max_y) / 2.0
        fixed_match = fragment_min_y - tolerance_mm <= fixed_coord <= fragment_max_y + tolerance_mm
        overlap = max(0.0, min(opening_max_x, fragment_max_x) - max(opening_min_x, fragment_min_x))
    anchor = candidate_anchor_tuple(opening_geometry_candidate)
    fragment_center = ((fragment_min_x + fragment_max_x) / 2.0, (fragment_min_y + fragment_max_y) / 2.0)
    distance = math.inf if anchor is None else squared_distance(anchor, fragment_center) ** 0.5
    return (overlap if fixed_match else 0.0, distance)


def best_boundary_fragment_for_opening(
    opening_geometry_candidate: dict[str, object],
    fragments: list[dict[str, object]],
) -> dict[str, object] | None:
    if not fragments:
        return None
    ranked = sorted(
        fragments,
        key=lambda fragment: (
            -opening_fragment_overlap_score(opening_geometry_candidate, fragment)[0],
            opening_fragment_overlap_score(opening_geometry_candidate, fragment)[1],
            str(fragment.get("fragment_id", "")),
        ),
    )
    best = ranked[0]
    overlap, _distance = opening_fragment_overlap_score(opening_geometry_candidate, best)
    return best if overlap > 0.0 else ranked[0]


def build_room_candidates_from_boundaries(
    *,
    room_label_candidates: list[dict[str, object]],
    boundary_candidates: list[dict[str, object]],
    opening_geometry_candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    room_boundaries = [
        dict(candidate)
        for candidate in boundary_candidates
        if isinstance(candidate, dict)
        and str(candidate.get("candidate_scope", "")).strip() == "room"
        and isinstance(candidate.get("bbox_xy"), list)
        and len(candidate.get("bbox_xy", [])) == 4
    ]
    if not room_boundaries:
        return []
    em_room_boundaries = [candidate for candidate in room_boundaries if is_em_layer_candidate(candidate)]
    if em_room_boundaries:
        room_boundaries = em_room_boundaries

    labels_by_boundary_index: dict[int, list[dict[str, object]]] = {index: [] for index in range(len(room_boundaries))}
    for label_candidate in room_label_candidates:
        anchor = candidate_anchor_tuple(label_candidate)
        if anchor is None:
            continue
        containing_indices = [
            index
            for index, boundary_candidate in enumerate(room_boundaries)
            if boundary_contains_anchor(boundary_candidate, anchor)
        ]
        if not containing_indices:
            continue
        containing_indices.sort(
            key=lambda index: (
                room_boundary_area_mm2(room_boundaries[index]),
                str(room_boundaries[index].get("handle", "")),
            )
        )
        labels_by_boundary_index[containing_indices[0]].append(dict(label_candidate))

    zone_candidates: list[dict[str, object]] = []
    for boundary_index, boundary_candidate in enumerate(room_boundaries):
        boundary_labels = list(labels_by_boundary_index.get(boundary_index, []))
        if not boundary_labels:
            continue
        labels_by_key: dict[str, dict[str, object]] = {}
        for label_candidate in boundary_labels:
            zone_key = canonical_zone_key(str(label_candidate.get("zone_name", "") or label_candidate.get("zone_key", "")))
            if zone_key and zone_key not in labels_by_key:
                labels_by_key[zone_key] = dict(label_candidate)
        deduped_labels = sorted(
            labels_by_key.values(),
            key=lambda label: (
                -float(candidate_anchor_tuple(label)[1]) if candidate_anchor_tuple(label) is not None else 0.0,
                float(candidate_anchor_tuple(label)[0]) if candidate_anchor_tuple(label) is not None else 0.0,
                str(label.get("label_handle", "")),
            ),
        )
        if not deduped_labels:
            continue

        zone_name_parts = [str(label.get("zone_name", "")).strip() for label in deduped_labels if str(label.get("zone_name", "")).strip()]
        zone_name = " + ".join(zone_name_parts)
        zone_key = canonical_zone_key(zone_name)
        label_areas = [
            float(label.get("area_m2", 0.0) or 0.0)
            for label in deduped_labels
            if label.get("area_m2") not in {None, ""}
        ]
        boundary_bbox = candidate_bbox_tuple(boundary_candidate)
        if boundary_bbox is None:
            continue
        merged_bbox = boundary_bbox
        anchor_xy = [round((merged_bbox[0] + merged_bbox[2]) / 2.0, 3), round((merged_bbox[1] + merged_bbox[3]) / 2.0, 3)]
        boundary_handle = str(boundary_candidate.get("handle", "") or "").strip()
        boundary_handles = [boundary_handle] if boundary_handle else []
        boundary_source_layer = str(boundary_candidate.get("source_layer", "") or "").strip()
        boundary_layer_role = str(boundary_candidate.get("layer_role", "") or "").strip()
        is_em_boundary = is_em_layer_candidate(boundary_candidate)
        fragment_bboxes_xy = [
            [round(value, 3) for value in merged_bbox]
        ]
        source_handles = [
            str(label.get("label_handle", "")).strip()
            for label in deduped_labels
            if str(label.get("label_handle", "")).strip()
        ]
        zone_candidates.append(
            {
                "zone_name": zone_name,
                "canonical_text": zone_name,
                "zone_key": zone_key,
                "area_m2": round(sum(label_areas), 3)
                if label_areas
                else round(((merged_bbox[2] - merged_bbox[0]) * (merged_bbox[3] - merged_bbox[1])) / 1_000_000.0, 3),
                "label_handle": source_handles[0] if len(source_handles) == 1 else None,
                "source_layer": "+".join(
                    sorted({str(label.get("source_layer", "")).strip() for label in deduped_labels if str(label.get("source_layer", "")).strip()})
                ),
                "anchor_xy": anchor_xy,
                "bbox_xy": [round(value, 3) for value in merged_bbox],
                "source_text": " + ".join(str(label.get("source_text", "")).strip() for label in deduped_labels if str(label.get("source_text", "")).strip()),
                "candidate_confidence": "high" if all(str(label.get("candidate_confidence", "")) == "high" for label in deduped_labels) else "medium",
                "source_candidate_count": len(deduped_labels),
                "source_handles": source_handles,
                "zone_detection_method": (
                    ("em_room_boundary_multi_text_name_merge" if is_em_boundary else "room_boundary_multi_text_name_merge")
                    if len(deduped_labels) > 1
                    else ("em_room_boundary_contained_text" if is_em_boundary else "room_boundary_contained_text")
                ),
                "boundary_fragment_count": len(fragment_bboxes_xy),
                "boundary_fragment_bboxes_xy": fragment_bboxes_xy,
                "boundary_handle": boundary_handles[0] if len(boundary_handles) == 1 else None,
                "boundary_handles": boundary_handles,
                "boundary_source_layer": boundary_source_layer,
                "boundary_layer_role": boundary_layer_role,
                "boundary_layer_canonical": boundary_candidate.get("layer_canonical"),
                "merge_opening_geometry_handles": [],
                "contained_zone_texts": zone_name_parts,
            }
        )

    assigned_label_handles = {
        handle
        for zone_candidate in zone_candidates
        for handle in list(zone_candidate.get("source_handles", []))
    }
    for label_candidate in room_label_candidates:
        label_handle = str(label_candidate.get("label_handle", "") or "").strip()
        if label_handle and label_handle in assigned_label_handles:
            continue
        if not label_handle and str(label_candidate.get("zone_key", "") or "").strip() in {
            str(zone_candidate.get("zone_key", "") or "").strip() for zone_candidate in zone_candidates
        }:
            continue
        zone_candidates.append(dict(label_candidate))

    return sorted(zone_candidates, key=lambda item: (str(item.get("zone_key", "")), str(item.get("label_handle", ""))))


def augment_room_candidates_with_room_boundaries(
    candidates: list[dict[str, object]],
    boundary_candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [dict(candidate) for candidate in candidates if isinstance(candidate, dict)]


def normalize_opening_type(value: str) -> str:
    normalized = str(value or "").strip()
    lookup = {
        "WINDOWORDOORNEEDSREVIEW": "WindowOrDoorNeedsReview",
        "NEEDSREVIEW": "NeedsReview",
        "WINDOW": "Window",
        "DOOR": "Door",
        "GLASSDOOR": "GlassDoor",
        "HOLE": "Hole",
    }
    return lookup.get(ascii_token(normalized), normalized)


def opening_import_proxy_payload(
    opening_type: str,
    layer_profile: dict[str, object],
) -> dict[str, object]:
    proxies = dict(layer_profile.get("opening_import_proxies", {}))
    proxy_config = proxies.get(normalize_opening_type(opening_type), {})
    if not isinstance(proxy_config, dict) or not proxy_config:
        return {}
    idf_surface_type = normalize_opening_type(str(proxy_config.get("idf_surface_type", "") or ""))
    if not idf_surface_type:
        return {}
    return {
        "idf_import_proxy": True,
        "idf_import_surface_type": idf_surface_type,
        "idf_import_name_suffix": ascii_token(str(proxy_config.get("name_suffix", "") or idf_surface_type)),
        "idf_import_construction_name": str(proxy_config.get("construction_name", "") or "").strip(),
        "idf_import_frame_and_divider_name": str(proxy_config.get("frame_and_divider_name", "") or "").strip(),
        "idf_import_proxy_review_reason": str(proxy_config.get("review_reason", "") or "idf_import_proxy").strip(),
    }


def is_geometry_record_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    geometry_roles = role_names_from_profile(layer_profile, "boundary_roles") | {"structural_fallback"}
    return record.record_type in GEOMETRY_RECORD_TYPES and record_matches_layer_roles(record, layer_profile, geometry_roles)


def is_useful_opening_geometry_record_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    return (
        record.record_type in OPENING_GEOMETRY_RECORD_TYPES
        and record_matches_layer_roles(record, layer_profile, {"opening_evidence"})
        and record.bbox is not None
    )


def is_excluded_insert_name(block_name: str) -> bool:
    if not block_name:
        return False
    return any(pattern.search(block_name) for pattern in EXCLUDED_INSERT_NAME_PATTERNS)


def is_useful_insert_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    opening_roles = role_names_from_profile(layer_profile, "opening_roles") | {"structural_fallback"}
    return (
        record.record_type == "INSERT"
        and record_matches_layer_roles(record, layer_profile, opening_roles)
        and not is_excluded_insert_name(record.block_name)
    )


def parse_room_candidate(record: Record) -> dict[str, object] | None:
    raw_text = record.text_blob
    name_text = raw_text.split("\\P", 1)[0]
    area_match = re.search(r"\(([0-9]+(?:[.,][0-9]+)?)\s*m2\)", raw_text, flags=re.IGNORECASE)
    area_m2 = parse_decimal_text(area_match.group(1)) if area_match else None
    zone_name = strip_mtext_formatting(name_text)
    if not matches_patterns(record.text_blob, ROOM_PATTERNS):
        if area_m2 is None or not zone_name or matches_patterns(zone_name, TITLE_PATTERNS):
            return None
        if any(pattern.search(zone_name) for pattern in OPENING_TEXT_PATTERNS + DIMENSION_TEXT_PATTERNS):
            return None
        if re.fullmatch(r"[+\-]?\d+(?:[.,]\d+)?", zone_name):
            return None
    anchor = record_anchor_xy(record)
    bbox = record.bbox
    anchor_xy = [anchor[0], anchor[1]] if anchor else None

    return {
        "zone_name": zone_name,
        "zone_key": canonical_zone_key(zone_name),
        "area_m2": area_m2,
        "label_handle": record.handle,
        "source_layer": record.layer,
        "anchor_xy": anchor_xy,
        "bbox_xy": list(bbox) if bbox else None,
        "source_text": strip_mtext_formatting(raw_text),
        "candidate_confidence": confidence_for_zone_candidate(anchor_xy, area_m2),
    }


def parse_apartment_title(record: Record) -> dict[str, object] | None:
    if not matches_patterns(record.text_blob, TITLE_PATTERNS):
        return None

    anchor = record_anchor_xy(record)
    title_text = strip_mtext_formatting(record.text_blob)
    return {
        "title_handle": record.handle,
        "source_layer": record.layer,
        "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
        "title_text": title_text,
        "title_key": ascii_token(title_text),
        "candidate_confidence": "high" if anchor else "medium",
    }


def classify_opening_type(type_code: str, size_text: str) -> dict[str, str]:
    normalized = str(type_code or "").upper()
    if normalized in {"SN", "SW", "LC1"}:
        return {
            "candidate_idf_object": "FenestrationSurface:Detailed",
            "candidate_fenestration_type": "Window",
            "mapping_confidence": "medium",
        }
    if normalized == "DG":
        return {
            "candidate_idf_object": "FenestrationSurface:Detailed",
            "candidate_fenestration_type": "GlassDoor",
            "mapping_confidence": "medium",
        }
    if normalized.startswith("DN") or normalized == "DWN":
        return {
            "candidate_idf_object": "FenestrationSurface:Detailed",
            "candidate_fenestration_type": "Door",
            "mapping_confidence": "medium",
        }
    if size_text:
        return {
            "candidate_idf_object": "FenestrationSurface:Detailed",
            "candidate_fenestration_type": "WindowOrDoorNeedsReview",
            "mapping_confidence": "low",
        }
    return {
        "candidate_idf_object": "FenestrationSurface:Detailed",
        "candidate_fenestration_type": "NeedsReview",
        "mapping_confidence": "low",
    }


def normalize_join_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    return re.sub(r"_+", "_", ascii_text).strip("_").upper()


def is_anonymous_block_name(block_name: str) -> bool:
    value = (block_name or "").strip().upper()
    return not value or value.startswith("*U") or value.startswith("*A")


def cluster_attribute_records(records: list[Record], max_distance: float = 550.0) -> list[list[Record]]:
    remaining = records.copy()
    clusters: list[list[Record]] = []
    max_distance_squared = max_distance * max_distance

    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            next_remaining: list[Record] = []
            for candidate in remaining:
                candidate_anchor = record_anchor_xy(candidate)
                if candidate_anchor is None:
                    next_remaining.append(candidate)
                    continue
                if any(
                    record_anchor_xy(member) is not None
                    and squared_distance(candidate_anchor, record_anchor_xy(member)) <= max_distance_squared
                    for member in cluster
                ):
                    cluster.append(candidate)
                    changed = True
                else:
                    next_remaining.append(candidate)
            remaining = next_remaining

        clusters.append(sorted(cluster, key=lambda record: (record.handle, record.text_blob)))

    return clusters


def summarize_opening_attributes(
    records: list[Record],
    *,
    reference_anchor_xy: tuple[float, float] | None = None,
) -> tuple[str, str, str, list[dict[str, object]]]:
    size_text = ""
    sill_height_text = ""
    type_code = ""
    attr_payload: list[dict[str, object]] = []
    size_candidates: list[dict[str, object]] = []
    sill_candidates: list[dict[str, object]] = []
    type_candidates: list[dict[str, object]] = []

    for attr_record in sorted(records, key=lambda record: (record.handle, record.text_blob)):
        text_value = strip_mtext_formatting(attr_record.text_blob)
        if not text_value:
            continue

        normalized_text = normalize_opening_text(text_value)
        anchor = record_anchor_xy(attr_record)
        owner_handle = first_group_code_value(attr_record, "330")
        payload = {
            "handle": attr_record.handle,
            "owner_handle": owner_handle or None,
            "text_value": text_value,
            "normalized_text": normalized_text,
            "raw_text": attr_record.text_blob,
            "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
            "selected_as_size_text": False,
            "selected_as_sill_height_text": False,
            "selected_as_type_code": False,
        }
        attr_payload.append(payload)

        size_components = extract_opening_size_components(text_value)
        if size_components is not None:
            distance_mm = float("inf")
            if anchor is not None and reference_anchor_xy is not None:
                distance_mm = squared_distance(anchor, reference_anchor_xy) ** 0.5
            size_candidates.append(
                {
                    "handle": attr_record.handle,
                    "anchor": anchor,
                    "distance_mm": distance_mm,
                    "match_rank": 0 if OPENING_SIZE_FULL_PATTERN.fullmatch(normalized_text) else 1,
                    "width_mm": size_components[0],
                    "height_mm": size_components[1],
                    "normalized_size_text": size_components[2],
                }
            )
        if OPENING_SILL_PATTERN.fullmatch(normalized_text):
            distance_mm = float("inf")
            if anchor is not None and reference_anchor_xy is not None:
                distance_mm = squared_distance(anchor, reference_anchor_xy) ** 0.5
            sill_candidates.append(
                {
                    "handle": attr_record.handle,
                    "distance_mm": distance_mm,
                    "normalized_text": normalized_text,
                }
            )
        if OPENING_TYPE_CODE_PATTERN.fullmatch(normalized_text):
            distance_mm = float("inf")
            if anchor is not None and reference_anchor_xy is not None:
                distance_mm = squared_distance(anchor, reference_anchor_xy) ** 0.5
            type_candidates.append(
                {
                    "handle": attr_record.handle,
                    "distance_mm": distance_mm,
                    "normalized_text": normalized_text.upper(),
                }
            )

    payload_by_handle = {
        str(payload.get("handle", "")).strip(): payload
        for payload in attr_payload
        if str(payload.get("handle", "")).strip()
    }
    if size_candidates:
        selected_size = sorted(
            size_candidates,
            key=lambda candidate: (
                int(candidate.get("match_rank", 99) or 99),
                float(candidate.get("distance_mm", float("inf")) or float("inf")),
                str(candidate.get("handle", "")),
            ),
        )[0]
        size_text = str(selected_size.get("normalized_size_text", "")).strip()
        selected_payload = payload_by_handle.get(str(selected_size.get("handle", "")))
        if selected_payload is not None:
            selected_payload["selected_as_size_text"] = True

    if sill_candidates:
        selected_sill = sorted(
            sill_candidates,
            key=lambda candidate: (
                float(candidate.get("distance_mm", float("inf")) or float("inf")),
                str(candidate.get("handle", "")),
            ),
        )[0]
        sill_height_text = str(selected_sill.get("normalized_text", "")).strip()
        selected_payload = payload_by_handle.get(str(selected_sill.get("handle", "")))
        if selected_payload is not None:
            selected_payload["selected_as_sill_height_text"] = True

    if type_candidates:
        selected_type = sorted(
            type_candidates,
            key=lambda candidate: (
                float(candidate.get("distance_mm", float("inf")) or float("inf")),
                str(candidate.get("handle", "")),
            ),
        )[0]
        type_code = str(selected_type.get("normalized_text", "")).strip()
        selected_payload = payload_by_handle.get(str(selected_type.get("handle", "")))
        if selected_payload is not None:
            selected_payload["selected_as_type_code"] = True

    return size_text, sill_height_text, type_code, attr_payload


def nearest_zone(anchor: tuple[float, float] | None, room_candidates: list[dict[str, object]]) -> tuple[str | None, float | None]:
    if anchor is None or not room_candidates:
        return None, None

    nearest = min(
        room_candidates,
        key=lambda item: squared_distance(anchor, tuple(item["anchor_xy"])) if item["anchor_xy"] else float("inf"),
    )
    if nearest.get("anchor_xy") is None:
        return None, None

    return str(nearest["zone_name"]), squared_distance(anchor, tuple(nearest["anchor_xy"])) ** 0.5


def bbox_center_xy(bbox: tuple[float, float, float, float] | list[float] | None) -> tuple[float, float] | None:
    if bbox is None:
        return None
    min_x, min_y, max_x, max_y = (float(value) for value in bbox)
    return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)


def opening_type_from_layer_name(layer_name: str) -> str | None:
    token = normalize_join_entity_name(layer_name)
    if "HOLE_AS_WINDOW" in token:
        return "Hole"
    if "GLASS" in token and "DOOR" in token:
        return "GlassDoor"
    if "WINDOW" in token:
        return "Window"
    if "DOOR" in token:
        return "Door"
    if "HOLE" in token:
        return "Hole"
    return None


def expected_opening_types(opening_type: str) -> set[str]:
    normalized = normalize_opening_type(opening_type)
    if normalized in {"", "NeedsReview", "WindowOrDoorNeedsReview"}:
        return set()
    return {normalized}


def apply_opening_geometry_classification(
    base_classification: dict[str, str],
    geometry_type_hint: str | None,
) -> dict[str, str]:
    if not geometry_type_hint:
        return dict(base_classification)

    merged = dict(base_classification)
    inferred_type = normalize_opening_type(geometry_type_hint)
    if not inferred_type:
        return merged
    merged["candidate_fenestration_type"] = inferred_type
    merged["mapping_confidence"] = "high"
    return merged


def build_opening_geometry_candidates(
    records: list[Record],
    layer_profile: dict[str, object],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for record in records:
        if not is_useful_opening_geometry_record_for_idf(record, layer_profile):
            continue
        if record.bbox is None:
            continue
        anchor = bbox_center_xy(record.bbox)
        if anchor is None:
            continue
        bbox_width_mm = abs(float(record.bbox[2]) - float(record.bbox[0]))
        bbox_height_mm = abs(float(record.bbox[3]) - float(record.bbox[1]))
        candidates.append(
            {
                "handle": record.handle,
                "record_type": record.record_type,
                "layer": record.layer,
                "anchor_xy": [round(anchor[0], 3), round(anchor[1], 3)],
                "bbox_xy": [round(float(value), 3) for value in record.bbox],
                "bbox_width_mm": round(bbox_width_mm, 3),
                "bbox_height_mm": round(bbox_height_mm, 3),
                "layer_type_hint": opening_type_from_layer_name(record.layer),
            }
        )
    return sorted(
        candidates,
        key=lambda item: (
            str(item.get("layer", "")),
            str(item.get("record_type", "")),
            str(item.get("handle", "")),
        ),
    )


def opening_geometry_length_mm(candidate: dict[str, object]) -> int | None:
    bbox_width_mm = float(candidate.get("bbox_width_mm", 0.0) or 0.0)
    bbox_height_mm = float(candidate.get("bbox_height_mm", 0.0) or 0.0)
    length_mm = max(bbox_width_mm, bbox_height_mm)
    if length_mm <= 1e-6:
        return None
    return int(round(length_mm))


def opening_geometry_axis_offset_match(
    opening_anchor: tuple[float, float],
    geometry_candidate: dict[str, object],
    *,
    width_hint_mm: float,
) -> bool:
    bbox = geometry_candidate.get("bbox_xy")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return False

    min_x = min(float(bbox[0]), float(bbox[2]))
    max_x = max(float(bbox[0]), float(bbox[2]))
    min_y = min(float(bbox[1]), float(bbox[3]))
    max_y = max(float(bbox[1]), float(bbox[3]))
    bbox_width_mm = max_x - min_x
    bbox_height_mm = max_y - min_y
    if max(bbox_width_mm, bbox_height_mm) <= 1e-6:
        return False

    if bbox_width_mm >= bbox_height_mm:
        along_delta_mm = max(0.0, min_x - opening_anchor[0], opening_anchor[0] - max_x)
        offset_delta_mm = max(0.0, min_y - opening_anchor[1], opening_anchor[1] - max_y)
    else:
        along_delta_mm = max(0.0, min_y - opening_anchor[1], opening_anchor[1] - max_y)
        offset_delta_mm = max(0.0, min_x - opening_anchor[0], opening_anchor[0] - max_x)

    allowed_along_mm = max(250.0, min(max(bbox_width_mm, bbox_height_mm) * 0.35, 450.0))
    allowed_offset_mm = max(OPENING_GEOMETRY_MATCH_DISTANCE_MM, width_hint_mm * 0.85)
    return along_delta_mm <= allowed_along_mm and offset_delta_mm <= allowed_offset_mm


def assign_nearby_opening_geometry(
    pending_openings: list[dict[str, object]],
    geometry_candidates: list[dict[str, object]],
) -> dict[int, dict[str, object]]:
    assignments: dict[int, dict[str, object]] = {}
    used_geometry_handles: set[str] = set()
    edges: list[tuple[tuple[object, ...], int, dict[str, object], float, bool]] = []

    for opening_index, payload in enumerate(pending_openings):
        anchor_xy = payload.get("cluster_centroid_xy") or payload.get("annotation_anchor_xy") or payload.get("anchor_xy")
        if not isinstance(anchor_xy, list) or len(anchor_xy) < 2:
            continue
        opening_anchor = (float(anchor_xy[0]), float(anchor_xy[1]))
        width_hint_mm = max(float(payload.get("width_mm", 0) or 0), float(payload.get("height_mm", 0) or 0))
        expected_types = expected_opening_types(str(payload.get("candidate_fenestration_type", "")))

        for candidate in geometry_candidates:
            candidate_anchor_xy = candidate.get("anchor_xy")
            if not isinstance(candidate_anchor_xy, list) or len(candidate_anchor_xy) < 2:
                continue
            geometry_anchor = (float(candidate_anchor_xy[0]), float(candidate_anchor_xy[1]))
            distance_mm = squared_distance(opening_anchor, geometry_anchor) ** 0.5
            candidate_long_edge_mm = max(
                float(candidate.get("bbox_width_mm", 0.0) or 0.0),
                float(candidate.get("bbox_height_mm", 0.0) or 0.0),
            )
            max_distance_mm = max(
                OPENING_GEOMETRY_MATCH_DISTANCE_MM,
                candidate_long_edge_mm * 0.75,
                width_hint_mm * 0.55,
            )
            if distance_mm > max_distance_mm and not opening_geometry_axis_offset_match(
                opening_anchor,
                candidate,
                width_hint_mm=width_hint_mm,
            ):
                continue

            geometry_type_hint = normalize_opening_type(str(candidate.get("layer_type_hint", "") or ""))
            type_conflict = bool(expected_types and geometry_type_hint and geometry_type_hint not in expected_types)
            edges.append(
                (
                    (
                        1 if type_conflict else 0,
                        distance_mm,
                        0 if geometry_type_hint else 1,
                        str(candidate.get("handle", "")),
                        opening_index,
                    ),
                    opening_index,
                    candidate,
                    distance_mm,
                    type_conflict,
                )
            )

    edges.sort(key=lambda item: item[0])
    for _sort_key, opening_index, candidate, distance_mm, type_conflict in edges:
        candidate_handle = str(candidate.get("handle", "")).strip()
        if opening_index in assignments or not candidate_handle or candidate_handle in used_geometry_handles:
            continue
        assignments[opening_index] = {
            **candidate,
            "distance_mm": round(distance_mm, 3),
            "type_conflict": type_conflict,
        }
        used_geometry_handles.add(candidate_handle)

    return assignments


def load_dxf_context(extract_path: Path | str) -> dict[str, object]:
    resolved_extract_path = GUARD.assert_read_path(extract_path)
    parsed_extract = parse_dxf_extract_file(resolved_extract_path)
    metadata_rows = list(parsed_extract.get("metadata_rows", []))
    block_defs = [
        BlockDefinition(
            name=str(section.get("block_name", "")),
            records=list(section.get("records", [])),
        )
        for section in parsed_extract.get("block_sections", [])
    ]
    filtered_records = list(parsed_extract.get("filtered_records", []))
    records_by_handle: dict[str, Record] = {}
    for record in [*flatten_block_records(block_defs), *filtered_records]:
        if record.handle:
            records_by_handle[record.handle] = record

    metadata = parse_extract_metadata_map(metadata_rows)
    source_extract = normalize_relative_path(metadata.get("Output", "")) or workspace_path(resolved_extract_path)
    upstream_source = normalize_relative_path(metadata.get("Source", ""))
    room_label_candidates = parse_json_metadata_value(metadata.get("Room label candidates", ""), [])
    title_candidates = parse_json_metadata_value(metadata.get("Title candidates", ""), [])
    boundary_candidates = parse_json_metadata_value(metadata.get("Boundary candidates", ""), [])
    apartment_extent_candidates = parse_json_metadata_value(metadata.get("Apartment extent candidates", ""), [])
    opening_evidence_candidates = parse_json_metadata_value(metadata.get("Opening candidates", ""), [])
    entities_kept_by_layer = parse_json_metadata_value(metadata.get("Entities kept by layer", ""), {})
    entities_rejected = parse_json_metadata_value(metadata.get("Entities rejected", ""), [])
    matched_layer_aliases = parse_json_metadata_value(metadata.get("Matched layer aliases", ""), {})
    rejected_layers = parse_json_metadata_value(metadata.get("Rejected layers", ""), {})
    parser_warnings = parse_json_metadata_value(metadata.get("Parser warnings", ""), [])
    fallback_usage = parse_json_metadata_value(metadata.get("Fallback usage", ""), {})

    filter_summary = {
        "profile": metadata.get("Filter profile", "idf-prep"),
        "kept_category_counts": parse_counter_text(metadata.get("Kept categories", "")),
        "kept_layers": parse_counter_text(metadata.get("Kept layers", "")),
        "excluded_record_type_counts": parse_counter_text(metadata.get("Excluded record types", "")),
        "rejected_layers": rejected_layers,
        "matched_layer_aliases": matched_layer_aliases,
        "entities_kept_by_layer": entities_kept_by_layer,
        "entities_rejected": entities_rejected,
        "room_label_candidates": room_label_candidates,
        "title_candidates": title_candidates,
        "boundary_candidates": boundary_candidates,
        "apartment_extent_candidates": apartment_extent_candidates,
        "opening_candidates": opening_evidence_candidates,
        "selection_source": metadata.get("Selection source", ""),
        "fallback_usage": fallback_usage,
        "parser_warnings": parser_warnings,
    }

    return {
        "extract_path": resolved_extract_path,
        "metadata_rows": metadata_rows,
        "metadata": metadata,
        "source_extract": source_extract,
        "upstream_source": upstream_source,
        "selection_bbox_xy": parse_selection_bbox_text(metadata.get("Selection bbox", "")),
        "filtered_records": filtered_records,
        "block_defs": block_defs,
        "records_by_handle": records_by_handle,
        "filter_summary": filter_summary,
        "room_label_candidates": room_label_candidates,
        "title_candidates": title_candidates,
        "boundary_candidates": boundary_candidates,
        "apartment_extent_candidates": apartment_extent_candidates,
        "opening_evidence_candidates": opening_evidence_candidates,
        "parser_warnings": parser_warnings,
        "fallback_usage": fallback_usage,
    }


def build_mapping_payload(
    *,
    dxf_context: dict[str, object],
    layer_profile: dict[str, object],
    zone_name_aliases: dict[str, object] | None = None,
) -> dict[str, object]:
    filtered_records = list(dxf_context["filtered_records"])
    block_defs = list(dxf_context["block_defs"])
    block_bbox_by_name = block_definition_bbox_lookup(block_defs)
    opening_placement_segments = extract_opening_placement_segments(filtered_records, layer_profile)
    records_by_handle = dict(dxf_context["records_by_handle"])
    filter_summary = dict(dxf_context["filter_summary"])
    parser_room_candidates = [
        dict(candidate)
        for candidate in list(dxf_context.get("room_label_candidates", []))
        if isinstance(candidate, dict)
    ]
    parser_room_candidates = [
        apply_zone_name_aliases_to_zone_candidate(candidate, zone_name_aliases)
        for candidate in parser_room_candidates
    ]
    room_candidates = deduplicate_room_candidates(parser_room_candidates)
    if not room_candidates:
        room_candidates = [
            item for item in (parse_room_candidate(record) for record in filtered_records) if item is not None
        ]
        room_candidates = [
            apply_zone_name_aliases_to_zone_candidate(candidate, zone_name_aliases)
            for candidate in room_candidates
        ]

    apartment_titles = [
        dict(candidate)
        for candidate in list(dxf_context.get("title_candidates", []))
        if isinstance(candidate, dict)
    ]
    if not apartment_titles:
        apartment_titles = [
            item for item in (parse_apartment_title(record) for record in filtered_records) if item is not None
        ]
    apartment_title = apartment_titles[0] if apartment_titles else None

    physical_inserts = [record for record in filtered_records if is_useful_insert_for_idf(record, layer_profile)]
    opening_geometry_candidates = build_opening_geometry_candidates(filtered_records, layer_profile)
    boundary_room_candidates = build_room_candidates_from_boundaries(
        room_label_candidates=room_candidates,
        boundary_candidates=[
            dict(candidate)
            for candidate in list(dxf_context.get("boundary_candidates", []))
            if isinstance(candidate, dict)
        ],
        opening_geometry_candidates=opening_geometry_candidates,
    )
    if boundary_room_candidates:
        room_candidates = boundary_room_candidates

    attr_records = [
        record
        for record in filtered_records
        if record.record_type == "ATTRIB"
        and record_matches_layer_roles(record, layer_profile, role_names_from_profile(layer_profile, "opening_roles"))
    ]
    attrs_by_owner: dict[str, list[Record]] = {}
    orphan_attr_records: list[Record] = []
    for record in attr_records:
        owner_handle = first_group_code_value(record, "330")
        if owner_handle:
            attrs_by_owner.setdefault(owner_handle, []).append(record)
        else:
            orphan_attr_records.append(record)

    opening_groups: list[tuple[str | None, list[Record]]] = [
        (owner_handle, sorted(records, key=lambda record: (record.handle, record.text_blob)))
        for owner_handle, records in sorted(attrs_by_owner.items())
    ]
    if orphan_attr_records:
        for cluster in cluster_attribute_records(orphan_attr_records):
            opening_groups.append((None, cluster))

    symbol_match_threshold = 1500.0
    pending_openings: list[dict[str, object]] = []
    for index, (owner_handle, attr_group) in enumerate(opening_groups, start=1):
        owner_record = records_by_handle.get(owner_handle, None) if owner_handle else None
        owner_anchor = record_anchor_xy(owner_record) if owner_record else None
        cluster_anchor = centroid_xy(
            [anchor for anchor in (record_anchor_xy(record) for record in attr_group) if anchor is not None]
        )
        anchor = owner_anchor or cluster_anchor

        size_text, sill_height_text, type_code, attr_payload = summarize_opening_attributes(
            attr_group,
            reference_anchor_xy=anchor,
        )

        width_mm = None
        height_mm = None
        size_components = extract_opening_size_components(size_text)
        if size_components is not None:
            width_mm = size_components[0]
            height_mm = size_components[1]

        sill_height_m = None
        if sill_height_text:
            numeric = parse_decimal_text(sill_height_text.lstrip("+"))
            if numeric is not None:
                sill_height_m = numeric

        candidate_symbol_matches: list[tuple[float, Record]] = []
        if anchor and physical_inserts:
            for record in physical_inserts:
                symbol_anchor = record_anchor_xy(record)
                if symbol_anchor is None:
                    continue
                distance = squared_distance(anchor, symbol_anchor) ** 0.5
                if distance <= symbol_match_threshold:
                    candidate_symbol_matches.append((distance, record))
            candidate_symbol_matches.sort(key=lambda item: (item[0], item[1].handle))

        nearest_zone_name, nearest_zone_distance = nearest_zone(anchor, room_candidates)
        candidate_classification = classify_opening_type(type_code, size_text)
        pending_openings.append(
            {
                "opening_id": f"OPENING_{index:03d}",
                "source_layer": attr_group[0].layer if attr_group else "",
                "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
                "cluster_centroid_xy": [cluster_anchor[0], cluster_anchor[1]] if cluster_anchor else None,
                "annotation_owner_handle": owner_handle,
                "annotation_owner_block_name": owner_record.block_name if owner_record else None,
                "annotation_owner_layer": owner_record.layer if owner_record else None,
                "annotation_anchor_xy": [owner_anchor[0], owner_anchor[1]] if owner_anchor else None,
                "size_text": size_text,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "sill_height_text": sill_height_text,
                "sill_height_m": sill_height_m,
                "type_code": type_code,
                "attribute_records": attr_payload,
                "nearest_zone_name": nearest_zone_name,
                "nearest_zone_distance": nearest_zone_distance,
                **candidate_classification,
                "_candidate_symbol_matches": candidate_symbol_matches,
            }
        )

    opening_geometry_assignments = assign_nearby_opening_geometry(pending_openings, opening_geometry_candidates)
    explicit_opening_geometry_available = bool(opening_geometry_candidates)

    used_symbol_handles: set[str] = set()
    symbol_assignments: dict[int, tuple[float, Record]] = {}
    assignment_edges: list[tuple[float, int, Record]] = []
    for opening_index, payload in enumerate(pending_openings):
        for distance, record in payload["_candidate_symbol_matches"]:
            assignment_edges.append((distance, opening_index, record))
    assignment_edges.sort(key=lambda item: (item[0], item[1], item[2].handle))

    for distance, opening_index, record in assignment_edges:
        if opening_index in symbol_assignments or record.handle in used_symbol_handles:
            continue
        symbol_assignments[opening_index] = (distance, record)
        used_symbol_handles.add(record.handle)

    opening_candidates: list[dict[str, object]] = []
    for opening_index, payload in enumerate(pending_openings):
        matched_symbol_distance = None
        matched_symbol = None
        if opening_index in symbol_assignments:
            matched_symbol_distance, matched_symbol = symbol_assignments[opening_index]

        matched_geometry = opening_geometry_assignments.get(opening_index)
        if explicit_opening_geometry_available and matched_geometry is None:
            continue

        if matched_symbol is not None and payload["mapping_confidence"] == "medium":
            payload["mapping_confidence"] = "high"
        if matched_geometry is not None and payload["mapping_confidence"] != "low":
            payload["mapping_confidence"] = "high"

        matched_symbol_anchor = record_anchor_xy(matched_symbol) if matched_symbol else None
        raw_block_name = matched_symbol.block_name if matched_symbol else (payload["annotation_owner_block_name"] or "")
        effective_classification = {
            "candidate_idf_object": str(payload["candidate_idf_object"]),
            "candidate_fenestration_type": str(payload["candidate_fenestration_type"]),
            "mapping_confidence": str(payload["mapping_confidence"]),
        }
        effective_classification = apply_opening_geometry_classification(
            effective_classification,
            str((matched_geometry or {}).get("layer_type_hint", "") or ""),
        )
        effective_classification["candidate_fenestration_type"] = normalize_opening_type(
            effective_classification["candidate_fenestration_type"]
        )
        payload_height_mm = payload["height_mm"]
        if effective_classification["candidate_fenestration_type"] == "Hole":
            payload_height_mm = None
        import_proxy_payload = opening_import_proxy_payload(
            effective_classification["candidate_fenestration_type"],
            layer_profile,
        )
        effective_block_name = raw_block_name
        symbol_name_resolution_method = "raw_block_name"
        applied_symbol_hint = None

        resolved_anchor_xy = payload["anchor_xy"]
        if matched_geometry is not None:
            resolved_anchor_xy = list(matched_geometry.get("anchor_xy", [])) if matched_geometry.get("anchor_xy") else payload["anchor_xy"]
        resolved_anchor_tuple = None
        if isinstance(resolved_anchor_xy, list) and len(resolved_anchor_xy) >= 2:
            resolved_anchor_tuple = (float(resolved_anchor_xy[0]), float(resolved_anchor_xy[1]))
        nearest_zone_name, nearest_zone_distance = nearest_zone(resolved_anchor_tuple, room_candidates)
        if nearest_zone_name is None:
            nearest_zone_name = payload["nearest_zone_name"]
            nearest_zone_distance = payload["nearest_zone_distance"]
        detection_method = "opening_layer_nearby_text" if matched_geometry is not None else "annotation_owner_insert"
        final_opening_id = f"OPENING_{len(opening_candidates) + 1:03d}"

        opening_candidates.append(
            {
                "opening_id": final_opening_id,
                "source_opening_group_id": payload["opening_id"],
                "insert_handle": matched_symbol.handle if matched_symbol else payload["annotation_owner_handle"],
                "block_name": effective_block_name,
                "raw_block_name": raw_block_name,
                "source_layer": payload["source_layer"],
                "anchor_xy": resolved_anchor_xy,
                "annotation_group_anchor_xy": payload["anchor_xy"],
                "cluster_centroid_xy": payload["cluster_centroid_xy"],
                "annotation_owner_handle": payload["annotation_owner_handle"],
                "annotation_owner_block_name": payload["annotation_owner_block_name"],
                "annotation_owner_layer": payload["annotation_owner_layer"],
                "annotation_anchor_xy": payload["annotation_anchor_xy"],
                "rotation_degrees": numeric_group_code_value(matched_symbol, "50") if matched_symbol else None,
                "scale_x": numeric_group_code_value(matched_symbol, "41") if matched_symbol else None,
                "scale_y": numeric_group_code_value(matched_symbol, "42") if matched_symbol else None,
                "scale_z": numeric_group_code_value(matched_symbol, "43") if matched_symbol else None,
                "size_text": payload["size_text"],
                "width_mm": payload["width_mm"],
                "height_mm": payload_height_mm,
                "sill_height_text": payload["sill_height_text"],
                "sill_height_m": payload["sill_height_m"],
                "type_code": payload["type_code"],
                "attribute_records": payload["attribute_records"],
                "matched_symbol_insert_handle": matched_symbol.handle if matched_symbol else None,
                "matched_symbol_block_name": matched_symbol.block_name if matched_symbol else None,
                "matched_symbol_layer": matched_symbol.layer if matched_symbol else None,
                "matched_symbol_anchor_xy": [matched_symbol_anchor[0], matched_symbol_anchor[1]] if matched_symbol_anchor else None,
                "matched_symbol_distance": matched_symbol_distance,
                "matched_symbol_block_bbox_mm": (
                    [round(value, 3) for value in block_bbox_by_name[matched_symbol.block_name]]
                    if matched_symbol is not None and matched_symbol.block_name in block_bbox_by_name
                    else None
                ),
                "symbol_name_resolution_method": symbol_name_resolution_method,
                "applied_symbol_hint": applied_symbol_hint,
                "matched_opening_geometry_handle": str((matched_geometry or {}).get("handle", "") or "") or None,
                "matched_opening_geometry_record_type": str((matched_geometry or {}).get("record_type", "") or "") or None,
                "matched_opening_geometry_layer": str((matched_geometry or {}).get("layer", "") or "") or None,
                "matched_opening_geometry_anchor_xy": list((matched_geometry or {}).get("anchor_xy", [])) if matched_geometry is not None else None,
                "matched_opening_geometry_bbox_mm": list((matched_geometry or {}).get("bbox_xy", [])) if matched_geometry is not None else None,
                "matched_opening_geometry_distance": (matched_geometry or {}).get("distance_mm"),
                "matched_opening_geometry_type_hint": str((matched_geometry or {}).get("layer_type_hint", "") or "") or None,
                "matched_opening_geometry_type_conflict": bool((matched_geometry or {}).get("type_conflict", False)),
                "opening_detection_method": detection_method,
                "nearest_zone_name": nearest_zone_name,
                "nearest_zone_distance": nearest_zone_distance,
                "nearest_zone_key": canonical_zone_key(str(nearest_zone_name or "")) if nearest_zone_name else None,
                "candidate_idf_object": effective_classification["candidate_idf_object"],
                "candidate_fenestration_type": effective_classification["candidate_fenestration_type"],
                "opening_type_normalized": effective_classification["candidate_fenestration_type"],
                "mapping_confidence": effective_classification["mapping_confidence"],
                "candidate_confidence": effective_classification["mapping_confidence"],
                **import_proxy_payload,
            }
        )

    used_geometry_handles = {
        str(geometry.get("handle", "") or "")
        for geometry in opening_geometry_assignments.values()
        if str(geometry.get("handle", "") or "")
    }
    merged_zone_opening_handles = {
        str(opening_handle or "").strip()
        for room_candidate in room_candidates
        if isinstance(room_candidate, dict)
        for opening_handle in list(room_candidate.get("merge_opening_geometry_handles", []))
        if str(opening_handle or "").strip()
    }
    for geometry_candidate in opening_geometry_candidates:
        geometry_type_hint = normalize_opening_type(str(geometry_candidate.get("layer_type_hint", "") or ""))
        geometry_handle = str(geometry_candidate.get("handle", "") or "")
        if geometry_type_hint != "Hole" or geometry_handle in used_geometry_handles:
            continue
        if geometry_handle in merged_zone_opening_handles:
            used_geometry_handles.add(geometry_handle)
            continue
        anchor_xy = geometry_candidate.get("anchor_xy")
        if not isinstance(anchor_xy, list) or len(anchor_xy) < 2:
            continue
        anchor_tuple = (float(anchor_xy[0]), float(anchor_xy[1]))
        nearest_zone_name, nearest_zone_distance = nearest_zone(anchor_tuple, room_candidates)
        width_mm = opening_geometry_length_mm(geometry_candidate)
        if width_mm is None:
            continue
        final_opening_id = f"OPENING_{len(opening_candidates) + 1:03d}"
        import_proxy_payload = opening_import_proxy_payload("Hole", layer_profile)
        opening_candidates.append(
            {
                "opening_id": final_opening_id,
                "source_opening_group_id": None,
                "insert_handle": None,
                "block_name": "",
                "raw_block_name": "",
                "source_layer": str(geometry_candidate.get("layer", "") or ""),
                "anchor_xy": list(anchor_xy),
                "annotation_group_anchor_xy": None,
                "cluster_centroid_xy": list(anchor_xy),
                "annotation_owner_handle": None,
                "annotation_owner_block_name": None,
                "annotation_owner_layer": None,
                "annotation_anchor_xy": None,
                "rotation_degrees": None,
                "scale_x": None,
                "scale_y": None,
                "scale_z": None,
                "size_text": "",
                "width_mm": width_mm,
                "height_mm": None,
                "sill_height_text": "",
                "sill_height_m": 0.0,
                "type_code": "HOLE",
                "attribute_records": [],
                "matched_symbol_insert_handle": None,
                "matched_symbol_block_name": None,
                "matched_symbol_layer": None,
                "matched_symbol_anchor_xy": None,
                "matched_symbol_distance": None,
                "matched_symbol_block_bbox_mm": None,
                "symbol_name_resolution_method": "none",
                "applied_symbol_hint": None,
                "matched_opening_geometry_handle": geometry_handle or None,
                "matched_opening_geometry_record_type": str(geometry_candidate.get("record_type", "") or "") or None,
                "matched_opening_geometry_layer": str(geometry_candidate.get("layer", "") or "") or None,
                "matched_opening_geometry_anchor_xy": list(anchor_xy),
                "matched_opening_geometry_bbox_mm": list(geometry_candidate.get("bbox_xy", [])),
                "matched_opening_geometry_distance": 0.0,
                "matched_opening_geometry_type_hint": "Hole",
                "matched_opening_geometry_type_conflict": False,
                "opening_detection_method": "opening_layer_geometry_only",
                "nearest_zone_name": nearest_zone_name,
                "nearest_zone_distance": nearest_zone_distance,
                "nearest_zone_key": canonical_zone_key(str(nearest_zone_name or "")) if nearest_zone_name else None,
                "candidate_idf_object": "FenestrationSurface:Detailed",
                "candidate_fenestration_type": "Hole",
                "opening_type_normalized": "Hole",
                "mapping_confidence": "high",
                "candidate_confidence": "high",
                "size_resolution_method": "opening_layer_geometry_width_ceiling_height",
                **import_proxy_payload,
            }
        )
        used_geometry_handles.add(geometry_handle)

    unmatched_symbol_inserts: list[dict[str, object]] = []
    for record in physical_inserts:
        if record.handle in used_symbol_handles:
            continue
        anchor = record_anchor_xy(record)
        nearest_zone_name, nearest_zone_distance = nearest_zone(anchor, room_candidates)
        unmatched_symbol_inserts.append(
            {
                "insert_handle": record.handle,
                "block_name": record.block_name,
                "raw_block_name": record.block_name,
                "source_layer": record.layer,
                "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
                "rotation_degrees": numeric_group_code_value(record, "50"),
                "scale_x": numeric_group_code_value(record, "41"),
                "scale_y": numeric_group_code_value(record, "42"),
                "scale_z": numeric_group_code_value(record, "43"),
                "symbol_name_resolution_method": "raw_block_name",
                "applied_symbol_hint": None,
                "nearest_zone_name": nearest_zone_name,
                "nearest_zone_distance": nearest_zone_distance,
                "review_reason": "No nearby TAC - CUA+LC annotation cluster was matched within 1500 mm.",
            }
        )

    dimension_candidates: list[dict[str, object]] = []
    for record in filtered_records:
        if record.record_type not in {"MTEXT", "TEXT", "DIMENSION"}:
            continue
        if not record_matches_layer_roles(record, layer_profile, {"dimension_fallback"}):
            continue
        raw_text = record.text_blob
        numeric_match = re.search(r"\\A1;([0-9]+)", raw_text, flags=re.IGNORECASE)
        value_mm = int(numeric_match.group(1)) if numeric_match else None
        anchor = record_anchor_xy(record)
        anchor_xy = [anchor[0], anchor[1]] if anchor else None
        dimension_candidates.append(
            {
                "handle": record.handle,
                "source_layer": record.layer,
                "value_mm": value_mm,
                "raw_text": raw_text,
                "display_text": strip_mtext_formatting(raw_text),
                "anchor_xy": anchor_xy,
                "candidate_confidence": confidence_for_dimension_annotation(value_mm, anchor_xy),
            }
        )

    geometry_layer_summary: list[dict[str, object]] = []
    for layer_name in sorted({record.layer for record in filtered_records if is_geometry_record_for_idf(record, layer_profile)}):
        layer_records = [
            record
            for record in filtered_records
            if is_geometry_record_for_idf(record, layer_profile) and record.layer == layer_name
        ]
        primary_match = None
        if layer_records:
            classification = classify_record_layer(layer_records[0], layer_profile)
            primary_match = classification.get("primary")
        geometry_layer_summary.append(
            {
                "layer": layer_name,
                "record_count": len(layer_records),
                "record_types": counter_to_sorted_dict(Counter(record.record_type for record in layer_records)),
                "candidate_role": str((primary_match or {}).get("role", "") or "needs_review"),
                "layer_match_source": (primary_match or {}).get("match_source"),
            }
        )

    metadata = dict(dxf_context.get("metadata", {}))
    final_room_candidates = room_candidates
    return {
        "mapping_version": "1.2.1",
        "mapping_purpose": "IDF prep mapping for DesignBuilder import workflow",
        "source_extract": str(dxf_context.get("source_extract", "")),
        "upstream_source": str(dxf_context.get("upstream_source", "")),
        "filter_profile": str(filter_summary.get("profile", "idf-prep")),
        "selection_bbox_xy": dxf_context.get("selection_bbox_xy"),
        "padding": None,
        "opening_placement_context": {
            "aligned_segment_count": len(opening_placement_segments),
            "aligned_segment_layer_counts": counter_to_sorted_dict(
                Counter(str(segment.get("layer", "") or "unknown") for segment in opening_placement_segments)
            ),
        },
        "opening_placement_segments": opening_placement_segments,
        "apartment": apartment_title,
        "apartment_titles": apartment_titles,
        "candidate_zones": final_room_candidates,
        "candidate_zone_count": len(final_room_candidates),
        "boundary_candidates": dxf_context.get("boundary_candidates", []),
        "apartment_extent_candidates": dxf_context.get("apartment_extent_candidates", []),
        "parser_opening_evidence_candidates": dxf_context.get("opening_evidence_candidates", []),
        "candidate_openings": opening_candidates,
        "candidate_opening_count": len(opening_candidates),
        "unmatched_symbol_insert_count": len(unmatched_symbol_inserts),
        "unmatched_symbol_inserts": unmatched_symbol_inserts,
        "dimension_annotations": dimension_candidates,
        "geometry_layer_summary": geometry_layer_summary,
        "retained_block_definitions": [block_def.name for block_def in block_defs],
        "filter_summary": filter_summary,
        "parser_warnings": dxf_context.get("parser_warnings", []),
        "fallback_usage": dxf_context.get("fallback_usage", {}),
        "extract_metadata": metadata,
        "idf_targets": {
            "Zone": "Derived from layer-prioritized room labels and, when available, S04 room-boundary candidates.",
            "FenestrationSurface:Detailed": "Derived from layer-prioritized opening annotations, explicit opening geometry, and opening evidence inserts.",
            "BuildingSurface:Detailed": "Derived from S04 boundary/wall layers first, with fallback geometry layers only when canonical layers are not available.",
            "Construction": "Not derivable from DXF extract alone; must be assigned downstream.",
            "Material": "Not derivable from DXF extract alone; must be assigned downstream.",
        },
        "manual_follow_up": [
            "Trace closed 2D geometry into surfaces before generating BuildingSurface:Detailed objects.",
            "Review all opening type_code classifications before converting to Window, Door, or GlassDoor.",
            "Assign constructions and materials separately because DXF text does not contain thermal definitions.",
            "Validate floor-to-floor height, zone height, and GlobalGeometryRules in the downstream IDF builder.",
        ],
        "sources": [
            "https://designbuilder.co.uk/helpv7.0/Content/IDFImport.htm",
            "https://designbuilder.co.uk/helpv7.0/Content/_Importing_DXF.htm",
            "https://support.designbuilder.co.uk/support/solutions/articles/103000181370-importing-2d-cad-data",
            "https://support.designbuilder.co.uk/support/solutions/articles/103000181312-why-do-i-get-out-of-range-errors-when-importing-dxf-floor-plans-",
        ],
    }


def build_mapping_summary(mapping_payload: dict[str, object]) -> dict[str, object]:
    apartment = mapping_payload.get("apartment")
    apartment_title = apartment.get("title_text") if isinstance(apartment, dict) else None
    openings = list(mapping_payload.get("candidate_openings", []))
    zones = list(mapping_payload.get("candidate_zones", []))
    dimensions = list(mapping_payload.get("dimension_annotations", []))
    boundary_candidates = list(mapping_payload.get("boundary_candidates", []))
    parser_warnings = list(mapping_payload.get("parser_warnings", []))

    return {
        "mapping_version": mapping_payload.get("mapping_version"),
        "mapping_purpose": mapping_payload.get("mapping_purpose"),
        "source_extract": mapping_payload.get("source_extract"),
        "upstream_source": mapping_payload.get("upstream_source"),
        "filter_profile": mapping_payload.get("filter_profile"),
        "selection_bbox_xy": mapping_payload.get("selection_bbox_xy"),
        "apartment_title": apartment_title,
        "candidate_zone_count": len(zones),
        "boundary_candidate_count": len(boundary_candidates),
        "candidate_opening_count": len(openings),
        "dimension_annotation_count": len(dimensions),
        "unmatched_symbol_insert_count": int(mapping_payload.get("unmatched_symbol_insert_count", 0) or 0),
        "zone_confidence_counts": counter_to_sorted_dict(
            Counter(str(item.get("candidate_confidence", "") or "unknown") for item in zones)
        ),
        "opening_confidence_counts": counter_to_sorted_dict(
            Counter(str(item.get("candidate_confidence", "") or "unknown") for item in openings)
        ),
        "opening_type_counts": counter_to_sorted_dict(
            Counter(str(item.get("candidate_fenestration_type", "") or "unknown") for item in openings)
        ),
        "opening_placement_context": mapping_payload.get("opening_placement_context", {}),
        "dimension_confidence_counts": counter_to_sorted_dict(
            Counter(str(item.get("candidate_confidence", "") or "unknown") for item in dimensions)
        ),
        "parser_warning_count": len(parser_warnings),
        "fallback_usage": mapping_payload.get("fallback_usage", {}),
        "retained_block_definition_count": len(list(mapping_payload.get("retained_block_definitions", []))),
    }


def build_mapping_artifacts(
    *,
    dxf_extract_path: Path | str,
    layer_profile_path: Path | str = DEFAULT_LAYER_PROFILE_PATH,
    zone_name_aliases: dict[str, object] | None = None,
) -> dict[str, object]:
    dxf_context = load_dxf_context(dxf_extract_path)
    layer_profile = load_layer_profile(layer_profile_path)
    mapping_payload = build_mapping_payload(
        dxf_context=dxf_context,
        layer_profile=layer_profile,
        zone_name_aliases=zone_name_aliases,
    )
    mapping_payload = apply_zone_name_aliases_to_mapping_payload(mapping_payload, zone_name_aliases)
    mapping_summary = build_mapping_summary(mapping_payload)
    return {
        "zone_candidates": list(mapping_payload.get("candidate_zones", [])),
        "opening_candidates": list(mapping_payload.get("candidate_openings", [])),
        "dimension_annotations": list(mapping_payload.get("dimension_annotations", [])),
        "mapping_payload": mapping_payload,
        "mapping_summary": mapping_summary,
    }


def write_mapping_outputs(
    mapping_artifacts: dict[str, object],
    *,
    output_dir: Path | str | None = None,
    legacy_payload_path: Path | str | None = None,
    project_id: str | None = None,
) -> dict[str, str]:
    resolved_output_dir = GUARD.resolve(output_dir or _resolve_default_output_dir(path_resolver.resolve_project_id(project_id)))
    if project_id is not None:
        path_resolver.assert_output_in_project_scope(path_resolver.resolve_project_id(project_id), resolved_output_dir)
    targets = {
        "zone_candidates": resolved_output_dir / "zone_candidates.json",
        "opening_candidates": resolved_output_dir / "opening_candidates.json",
        "dimension_annotations": resolved_output_dir / "dimension_annotations.json",
        "mapping_payload": resolved_output_dir / "mapping_payload.json",
        "mapping_summary": resolved_output_dir / "mapping_summary.json",
    }
    default_payloads = {
        "zone_candidates": [],
        "opening_candidates": [],
        "dimension_annotations": [],
        "mapping_payload": {},
        "mapping_summary": {},
    }

    for key, path in targets.items():
        GUARD.write_json(
            path,
            mapping_artifacts.get(key, default_payloads[key]),
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )

    if legacy_payload_path is not None:
        resolved_legacy_payload_path = GUARD.assert_write_path(
            legacy_payload_path,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )
        if resolved_legacy_payload_path != targets["mapping_payload"]:
            GUARD.write_json(
                resolved_legacy_payload_path,
                mapping_artifacts.get("mapping_payload", {}),
                allowed_roots=["5_output"],
                allow_create=True,
                allow_overwrite=True,
            )

    return {
        key: workspace_path(path)
        for key, path in targets.items()
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build semantic mapping artifacts from normalized DXF context."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--dxf-extract",
        default=None,
        help="Normalized DXF extract path. If omitted, resolves from 5_output/<project_id>/normalized/dxf.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Mapping artifact output directory. If omitted, defaults to 5_output/<project_id>/intermediate/mapping.",
    )
    parser.add_argument(
        "--layer-profile",
        default=str(DEFAULT_LAYER_PROFILE_PATH.relative_to(ROOT)),
        help="Layer recognition profile JSON inside 2_config/.",
    )
    parser.add_argument(
        "--legacy-payload-output",
        help="Optional extra path to mirror mapping_payload.json for backward compatibility.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)
    dxf_extract = args.dxf_extract or _resolve_default_dxf_extract(project_id)
    output_dir = args.output_dir or _resolve_default_output_dir(project_id)

    mapping_artifacts = build_mapping_artifacts(
        dxf_extract_path=dxf_extract,
        layer_profile_path=args.layer_profile,
    )
    written_paths = write_mapping_outputs(
        mapping_artifacts,
        output_dir=output_dir,
        legacy_payload_path=args.legacy_payload_output,
        project_id=project_id,
    )

    mapping_summary = dict(mapping_artifacts.get("mapping_summary", {}))
    print("MAPPING_BUILD_COMPLETE")
    print(f"DXF extract: {normalize_relative_path(str(dxf_extract))}")
    print(f"Zone candidates: {mapping_summary.get('candidate_zone_count', 0)}")
    print(f"Opening candidates: {mapping_summary.get('candidate_opening_count', 0)}")
    print(f"Dimension annotations: {mapping_summary.get('dimension_annotation_count', 0)}")
    for key, value in written_paths.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
