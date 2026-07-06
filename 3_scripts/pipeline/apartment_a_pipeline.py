#!/usr/bin/env python3
"""
Main Apartment A pipeline script for the current workspace flow.

Responsibilities kept here:
- extract Apartment A DXF records into the normalized TXT/schema contract
- orchestrate mapping -> geometry -> surfaces -> walls -> fenestration -> bundle -> rebuilt IDF

Authoritative downstream algorithms live in their dedicated modules.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, deque
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context.mapping_builder import (
    DEFAULT_OUTPUT_DIR as CONTEXT_MAPPING_OUTPUT_DIR,
    DEFAULT_MAPPING_PAYLOAD_OUTPUT as CONTEXT_MAPPING_PAYLOAD_OUTPUT,
    build_mapping_artifacts as build_context_mapping_artifacts,
    write_mapping_outputs as write_context_mapping_outputs,
)
from parsers.dxf_raw_parser import (
    DEFAULT_DXF_LAYER_PROFILE,
    DEFAULT_DXF_OUTPUT_DIR,
    BlockDefinition,
    Record,
    build_block_definitions,
    classify_record_layer,
    first_group_code_value,
    parse_dxf_file,
    load_layer_profile,
    record_anchor_xy,
    record_is_closed,
    record_matches_layer_roles,
    write_dxf_outputs,
)
from transformers.geometry_inference import (
    DEFAULT_OUTPUT_DIR as GEOMETRY_OUTPUT_DIR,
    DEFAULT_ZONE_OUTPUT_PREFIX as GEOMETRY_DEFAULT_ZONE_OUTPUT_PREFIX,
    infer_apartment_a_geometry,
    write_geometry_outputs,
)
from transformers.fenestration_builder import (
    DEFAULT_OUTPUT_DIR as FENESTRATION_OUTPUT_DIR,
    build_fenestration_artifacts,
    write_fenestration_outputs,
)
from transformers.surface_builder import (
    DEFAULT_OUTPUT_DIR as SURFACE_OUTPUT_DIR,
    build_surface_artifacts,
    write_surface_outputs,
)
from transformers.wall_logic import (
    DEFAULT_OUTPUT_DIR as WALL_OUTPUT_DIR,
    build_wall_artifacts,
    write_wall_outputs,
)
from writers.bundle_writer import (
    build_bundle_artifacts_from_paths as build_bundle_writer_artifacts_from_paths,
    write_bundle_outputs as write_bundle_writer_outputs,
)
from writers.rebuild_idf_from_bundle import (
    DEFAULT_IDF_OUTPUT as DEFAULT_REBUILT_IDF_OUTPUT,
    rebuild_idf_from_bundle as rebuild_idf_from_bundle_writer,
)
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError
from utils import path_resolver
from utils.common import workspace_path


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_INPUT = Path("1_input") / "<project_id>" / "clean" / "txt_dxf" / "<project>.txt"
DEFAULT_OUTPUT = Path("5_output") / "<project_id>" / "normalized" / "dxf" / "<project>_filtered_extract.txt"
DEFAULT_SCHEMA_OUTPUT = Path("5_output") / "<project_id>" / "normalized" / "dxf" / "<project>_filtered_extract_schema.json"
DEFAULT_MAPPING_OUTPUT = Path("5_output") / "<project_id>" / "intermediate" / "mapping" / "mapping_payload.json"
DEFAULT_MAPPING_OUTPUT_DIR = Path("5_output") / "<project_id>" / "intermediate" / "mapping"
DEFAULT_IDF_BUNDLE_OUTPUT_DIR = Path("5_output") / "<project_id>" / "csv" / "<project_id>_idf_input_bundle"
DEFAULT_APARTMENT_A_GEOMETRY_POLICY = ROOT / "2_config" / "apartment_a_geometry_policy.json"
DEFAULT_DXF_LAYER_PROFILE_PATH = DEFAULT_DXF_LAYER_PROFILE
DEFAULT_ZONE_OUTPUT_PREFIX = GEOMETRY_DEFAULT_ZONE_OUTPUT_PREFIX
DEFAULT_ROOM_PATTERN_TEXTS = (
    r"PK\s*\+\s*PB",
    r"PN\s*0?1",
    r"PN\s*0?2",
    r"WC\s*0?1",
    r"WC\s*0?2",
    r"LOGIA",
)
DEFAULT_TITLE_PATTERN_TEXTS = (
    r"CH[\s\-]?A",
    r"CAN\s+HO",
)


def _canonical_output_basename(input_path: Path, project_id: str) -> str:
    stem = input_path.stem.strip()
    lowered = stem.lower()
    suffixes = (
        "_dxf_raw",
        "_dxf_review",
        "_dxf_ready",
        "_dxf",
        " dxf raw",
        " dxf review",
        " dxf ready",
        " dxf",
    )
    for suffix in suffixes:
        if lowered.endswith(suffix):
            stem = stem[: -len(suffix)].strip(" _-")
            break
    token = re.sub(r"\s+", "_", stem).strip("_")
    return token or project_id


def _resolve_default_pipeline_input(project_id: str) -> Path:
    resolved = path_resolver.resolve_project_dxf_text_input(project_id)
    if resolved is None:
        raise WorkspaceRuleError(f"No DXF text input found for project '{project_id}'.")
    return resolved


def _resolve_default_pipeline_output(project_id: str, input_path: Path | None = None) -> Path:
    resolved_input = input_path or _resolve_default_pipeline_input(project_id)
    filename = f"{_canonical_output_basename(resolved_input, project_id)}_filtered_extract.txt"
    return path_resolver.resolve_output_file(project_id, "normalized/dxf", filename)


def _resolve_default_pipeline_schema_output(project_id: str, input_path: Path | None = None) -> Path:
    resolved_input = input_path or _resolve_default_pipeline_input(project_id)
    filename = f"{_canonical_output_basename(resolved_input, project_id)}_filtered_extract_schema.json"
    return path_resolver.resolve_output_file(project_id, "normalized/dxf", filename)


def _resolve_default_mapping_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/mapping")


def _resolve_default_mapping_payload_path(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "intermediate/mapping", "mapping_payload.json")


def _resolve_default_bundle_output_dir(project_id: str, input_path: Path | None = None) -> Path:
    resolved_input = input_path or _resolve_default_pipeline_input(project_id)
    filename = f"{_canonical_output_basename(resolved_input, project_id)}_idf_input_bundle"
    return path_resolver.resolve_output_file(project_id, "csv", filename)


def _resolve_default_rebuilt_idf_output(project_id: str, input_path: Path | None = None) -> Path:
    resolved_input = input_path or _resolve_default_pipeline_input(project_id)
    filename = f"{_canonical_output_basename(resolved_input, project_id)}_generated_from_bundle.idf"
    return path_resolver.resolve_output_file(project_id, "idf", filename)


def compile_text_patterns(pattern_texts: list[str] | tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(pattern_text, re.IGNORECASE) for pattern_text in pattern_texts]


def configure_anchor_patterns(
    room_pattern_texts: list[str] | tuple[str, ...] = DEFAULT_ROOM_PATTERN_TEXTS,
    title_pattern_texts: list[str] | tuple[str, ...] = DEFAULT_TITLE_PATTERN_TEXTS,
) -> None:
    global ROOM_PATTERNS, TITLE_PATTERNS
    ROOM_PATTERNS = compile_text_patterns(room_pattern_texts)
    TITLE_PATTERNS = compile_text_patterns(title_pattern_texts)

configure_anchor_patterns()

GEOMETRY_RECORD_TYPES = {
    "LINE",
    "LWPOLYLINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "SPLINE",
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

INSERT_LAYERS = {
    "TAC - Door window",
    "TAC - CUA+LC",
    "TAC - Betong",
}

OPENING_TEXT_PATTERNS = [
    re.compile(r"\b\d{2,4}\s*[Xx]\s*\d{2,4}\b"),
    re.compile(r"^\+\s*\d+(?:[.,]\d+)?$"),
    re.compile(r"^(?:DN\d+|DWN|DG|SW|SN|LC\d+)$", re.IGNORECASE),
]

DIMENSION_TEXT_PATTERNS = [
    re.compile(r"\\A1;[0-9]+", re.IGNORECASE),
]

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


def parse_decimal_text(value: str) -> float | None:
    cleaned = value.strip().replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def matches_patterns(text: str, patterns: list[re.Pattern[str]]) -> bool:
    if not text:
        return False
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    candidates = (text, normalized) if normalized != text else (text,)
    return any(pattern.search(candidate) for candidate in candidates for pattern in patterns)


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


def json_metadata_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def role_names_from_profile(layer_profile: dict[str, object], key: str) -> set[str]:
    return {str(value) for value in list(layer_profile.get(key, []))}


def classification_for_roles(
    record: Record,
    layer_profile: dict[str, object],
    role_names: set[str],
) -> dict[str, object] | None:
    classification = classify_record_layer(record, layer_profile)
    for match in classification.get("matches", []):
        if str(match.get("role", "")) in role_names:
            return dict(match)
    return None


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


def canonical_room_label(text: str) -> tuple[str | None, str]:
    display_text = strip_mtext_formatting(text)
    first_line = str(display_text.split("\n", 1)[0]).strip()
    label_text = re.split(r"\s*\(", first_line, maxsplit=1)[0].strip() or first_line
    normalized = unicodedata.normalize("NFKD", label_text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").upper()
    ascii_text = re.sub(r"[^A-Z0-9+]+", " ", ascii_text)
    ascii_text = re.sub(r"\s+", " ", ascii_text).strip()
    compact = ascii_text.replace(" ", "")

    if re.search(r"PK\s*\+\s*PB", ascii_text):
        return "PK_PB", "PK + PB"
    if compact == "PK":
        return "PK", "PK"
    if re.search(r"PN\s*0?1", ascii_text):
        return "PN_01", "PN 01"
    if re.search(r"PN\s*0?2", ascii_text):
        return "PN_02", "PN 02"
    if compact == "PN":
        return "PN", "PN"
    if re.search(r"WC\s*0?1", ascii_text):
        return "WC_01", "WC 01"
    if re.search(r"WC\s*0?2", ascii_text):
        return "WC_02", "WC 02"
    if compact in {"LOGIA", "LO_GIA", "LOGIAA", "LGIA"} or re.search(r"L\s*GIA", ascii_text):
        return "LOGIA", "LOGIA"
    return None, label_text


def generic_room_label_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("+", "_")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    return re.sub(r"_+", "_", ascii_text).strip("_").upper() or "UNNAMED"


def probable_room_label_text(raw_text: str, *, layer_role: str, area_m2: float | None) -> str | None:
    display_text = strip_mtext_formatting(raw_text)
    first_line = str(display_text.split("\n", 1)[0]).strip()
    label_text = re.split(r"\s*\(", first_line, maxsplit=1)[0].strip() or first_line
    label_text = re.sub(r"\s+", " ", label_text).strip()
    if not label_text:
        return None
    if matches_patterns(label_text, TITLE_PATTERNS):
        return None
    if any(pattern.search(label_text) for pattern in OPENING_TEXT_PATTERNS + DIMENSION_TEXT_PATTERNS):
        return None
    if re.fullmatch(r"[+\-]?\d+(?:[.,]\d+)?", label_text):
        return None
    if len(label_text) > 40:
        return None
    if area_m2 is None and str(layer_role) != "room_label":
        return None
    ascii_label = unicodedata.normalize("NFKD", label_text).encode("ascii", "ignore").decode("ascii")
    if not re.search(r"[A-Za-z]", ascii_label):
        return None
    return label_text


def room_candidate_confidence(match: dict[str, object], *, has_area: bool, has_anchor: bool) -> str:
    match_source = str(match.get("match_source", ""))
    role_name = str(match.get("role", ""))
    if role_name == "room_label" and match_source in {"canonical", "alias"} and has_area and has_anchor:
        return "high"
    if role_name == "room_label" and has_anchor:
        return "medium"
    return "low"


def title_candidate_confidence(match: dict[str, object], *, has_anchor: bool) -> str:
    match_source = str(match.get("match_source", ""))
    if match_source in {"canonical", "alias"} and has_anchor:
        return "high"
    if has_anchor:
        return "medium"
    return "low"


def build_room_label_candidate(record: Record, layer_profile: dict[str, object]) -> dict[str, object] | None:
    room_label_roles = role_names_from_profile(layer_profile, "room_label_roles")
    match = classification_for_roles(record, layer_profile, room_label_roles)
    if match is None or record.record_type not in {"MTEXT", "TEXT", "ATTRIB"}:
        return None

    raw_text = record.text_blob
    zone_key, zone_name = canonical_room_label(raw_text)
    area_match = re.search(r"\(([0-9]+(?:[.,][0-9]+)?)\s*m2\)", raw_text, flags=re.IGNORECASE)
    area_m2 = parse_decimal_text(area_match.group(1)) if area_match else None
    if zone_key is None:
        zone_name = probable_room_label_text(
            raw_text,
            layer_role=str(match.get("role", "")),
            area_m2=area_m2,
        ) or ""
        if not zone_name and not matches_patterns(raw_text, ROOM_PATTERNS):
            return None
        zone_key = canonical_room_label(zone_name)[0] or generic_room_label_key(zone_name)
    anchor = record_anchor_xy(record)
    bbox = record.bbox

    return {
        "zone_name": zone_name,
        "canonical_text": zone_name,
        "zone_key": zone_key,
        "area_m2": area_m2,
        "label_handle": record.handle,
        "source_layer": record.layer,
        "layer_role": match.get("role"),
        "layer_canonical": match.get("canonical_layer"),
        "layer_match_source": match.get("match_source"),
        "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
        "bbox_xy": list(bbox) if bbox else None,
        "raw_text": raw_text,
        "source_text": strip_mtext_formatting(raw_text),
        "candidate_confidence": room_candidate_confidence(
            match,
            has_area=area_m2 is not None,
            has_anchor=anchor is not None,
        ),
    }


def build_title_candidate(record: Record, layer_profile: dict[str, object]) -> dict[str, object] | None:
    title_roles = role_names_from_profile(layer_profile, "room_label_roles") | {"title_fallback"}
    match = classification_for_roles(record, layer_profile, title_roles)
    if match is None or record.record_type not in {"MTEXT", "TEXT", "ATTRIB"}:
        return None
    if not matches_patterns(record.text_blob, TITLE_PATTERNS):
        return None

    anchor = record_anchor_xy(record)
    title_text = strip_mtext_formatting(record.text_blob)
    return {
        "title_handle": record.handle,
        "source_layer": record.layer,
        "layer_role": match.get("role"),
        "layer_canonical": match.get("canonical_layer"),
        "layer_match_source": match.get("match_source"),
        "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
        "title_text": title_text,
        "candidate_confidence": title_candidate_confidence(match, has_anchor=anchor is not None),
    }


def bbox_contains_point(bbox: tuple[float, float, float, float] | list[float], point_xy: tuple[float, float]) -> bool:
    min_x, min_y, max_x, max_y = (float(value) for value in bbox)
    point_x, point_y = point_xy
    return min_x - 1e-6 <= point_x <= max_x + 1e-6 and min_y - 1e-6 <= point_y <= max_y + 1e-6


def build_boundary_candidate(record: Record, layer_profile: dict[str, object]) -> dict[str, object] | None:
    boundary_roles = role_names_from_profile(layer_profile, "boundary_roles")
    match = classification_for_roles(record, layer_profile, boundary_roles)
    if match is None or record.record_type not in {"LWPOLYLINE", "POLYLINE", "LINE"}:
        return None
    if record.bbox is None or not record.points:
        return None

    candidate_scope = "supporting"
    if match.get("role") == "apartment_boundary":
        candidate_scope = "apartment"
    elif match.get("role") == "room_boundary":
        candidate_scope = "room"

    if candidate_scope == "supporting" and (record.record_type not in {"LWPOLYLINE", "POLYLINE"} or len(record.points) < 4):
        return None

    closed_polyline = record_is_closed(record)
    candidate_confidence = "high" if closed_polyline and candidate_scope in {"apartment", "room"} else "medium"
    if str(match.get("match_source", "")) == "fuzzy":
        candidate_confidence = "low" if candidate_confidence == "medium" else "medium"

    min_x, min_y, max_x, max_y = (float(value) for value in record.bbox)
    return {
        "handle": record.handle,
        "source_layer": record.layer,
        "layer_role": match.get("role"),
        "layer_canonical": match.get("canonical_layer"),
        "layer_match_source": match.get("match_source"),
        "candidate_scope": candidate_scope,
        "candidate_confidence": candidate_confidence,
        "priority": int(match.get("priority", 0) or 0),
        "record_type": record.record_type,
        "closed_polyline": closed_polyline,
        "point_count": len(record.points),
        "bbox_xy": [min_x, min_y, max_x, max_y],
        "bbox_area_mm2": (max_x - min_x) * (max_y - min_y),
        "points_xy": [[point[0], point[1]] for point in record.points],
    }


def build_opening_evidence_candidate(record: Record, layer_profile: dict[str, object]) -> dict[str, object] | None:
    opening_roles = role_names_from_profile(layer_profile, "opening_roles")
    match = classification_for_roles(record, layer_profile, opening_roles)
    if match is None:
        return None

    anchor = record_anchor_xy(record)
    bbox = record.bbox
    if anchor is None and bbox is None:
        return None

    confidence = "high" if str(match.get("role", "")) == "opening_evidence" else "medium"
    if str(match.get("match_source", "")) == "fuzzy":
        confidence = "low"
    return {
        "handle": record.handle,
        "source_layer": record.layer,
        "layer_role": match.get("role"),
        "layer_canonical": match.get("canonical_layer"),
        "layer_match_source": match.get("match_source"),
        "record_type": record.record_type,
        "block_name": record.block_name,
        "anchor_xy": [anchor[0], anchor[1]] if anchor else None,
        "bbox_xy": list(bbox) if bbox else None,
        "candidate_confidence": confidence,
    }


def derive_selection_bbox(
    parser_candidates: dict[str, object],
    records_by_handle: dict[str, Record],
    padding: float,
) -> tuple[tuple[float, float, float, float], str]:
    apartment_extent_candidates = list(parser_candidates.get("apartment_extent_candidates", []))
    room_boundary_candidates = [
        candidate
        for candidate in list(parser_candidates.get("boundary_candidates", []))
        if str(candidate.get("candidate_scope", "")) == "room"
    ]
    room_records = [
        records_by_handle[handle]
        for handle in [
            candidate.get("label_handle")
            for candidate in list(parser_candidates.get("room_label_candidates", []))
            if candidate.get("label_handle") in records_by_handle
        ]
    ]
    title_records = [
        records_by_handle[handle]
        for handle in [
            candidate.get("title_handle")
            for candidate in list(parser_candidates.get("title_candidates", []))
            if candidate.get("title_handle") in records_by_handle
        ]
    ]

    if apartment_extent_candidates:
        bbox_values = apartment_extent_candidates[0].get("bbox_xy", [])
        if isinstance(bbox_values, list) and len(bbox_values) == 4:
            base_bbox = tuple(float(value) for value in bbox_values)
            return expand_bbox(base_bbox, min(padding, 1500.0)), "layer_apartment_boundary"

    if room_boundary_candidates:
        min_x = min(float(candidate["bbox_xy"][0]) for candidate in room_boundary_candidates)
        min_y = min(float(candidate["bbox_xy"][1]) for candidate in room_boundary_candidates)
        max_x = max(float(candidate["bbox_xy"][2]) for candidate in room_boundary_candidates)
        max_y = max(float(candidate["bbox_xy"][3]) for candidate in room_boundary_candidates)
        return expand_bbox((min_x, min_y, max_x, max_y), min(padding, 1500.0)), "layer_room_boundary_union"

    anchor_records = room_records or title_records
    if not anchor_records:
        raise WorkspaceRuleError("Could not derive Apartment A selection bbox from S04 layer candidates or text anchors.")
    return expand_bbox(bbox_from_records(anchor_records), padding), "text_anchor_fallback"


def build_parser_candidates(
    records: list[Record],
    layer_profile: dict[str, object],
    *,
    padding: float,
) -> dict[str, object]:
    room_label_candidates = [
        candidate
        for candidate in (build_room_label_candidate(record, layer_profile) for record in records)
        if candidate is not None
    ]
    title_candidates = [
        candidate
        for candidate in (build_title_candidate(record, layer_profile) for record in records)
        if candidate is not None
    ]
    boundary_candidates = [
        candidate
        for candidate in (build_boundary_candidate(record, layer_profile) for record in records)
        if candidate is not None
    ]
    boundary_candidates.sort(key=boundary_candidate_sort_key)

    room_anchor_points = [
        (float(candidate["anchor_xy"][0]), float(candidate["anchor_xy"][1]))
        for candidate in room_label_candidates
        if isinstance(candidate.get("anchor_xy"), list) and len(candidate["anchor_xy"]) >= 2
    ]
    apartment_extent_candidates: list[dict[str, object]] = []
    for candidate in boundary_candidates:
        bbox_xy = candidate.get("bbox_xy")
        if not isinstance(bbox_xy, list) or len(bbox_xy) != 4:
            continue
        contains_room_anchors = bool(room_anchor_points) and all(
            bbox_contains_point(bbox_xy, point_xy)
            for point_xy in room_anchor_points
        )
        candidate["contains_room_anchors"] = contains_room_anchors
        if str(candidate.get("candidate_scope", "")) == "apartment":
            apartment_extent_candidates.append(candidate)
        elif bool(candidate.get("closed_polyline")) and contains_room_anchors:
            apartment_extent_candidates.append(candidate)
    apartment_extent_candidates.sort(key=boundary_candidate_sort_key)

    records_by_handle = {record.handle: record for record in records if record.handle}
    selection_bbox, selection_source = derive_selection_bbox(
        {
            "apartment_extent_candidates": apartment_extent_candidates,
            "boundary_candidates": boundary_candidates,
            "room_label_candidates": room_label_candidates,
            "title_candidates": title_candidates,
        },
        records_by_handle,
        padding,
    )

    opening_candidates = [
        candidate
        for candidate in (build_opening_evidence_candidate(record, layer_profile) for record in records)
        if candidate is not None
        and (
            candidate.get("handle") in records_by_handle
            and (
                (candidate.get("anchor_xy") and bbox_contains_point(selection_bbox, (float(candidate["anchor_xy"][0]), float(candidate["anchor_xy"][1]))))
                or (candidate.get("bbox_xy") and intersects(tuple(float(value) for value in candidate["bbox_xy"]), selection_bbox))
            )
        )
    ]

    parser_warnings: list[str] = []
    if not apartment_extent_candidates:
        parser_warnings.append("No high-priority apartment boundary layer candidate was found; selection bbox used fallback logic.")
    if not any(str(candidate.get("layer_role", "")) == "room_label" for candidate in room_label_candidates):
        parser_warnings.append("No room labels were confirmed on the primary room-label layer; fallback layer logic was used.")
    if not any(str(candidate.get("layer_role", "")) == "room_boundary" for candidate in boundary_candidates):
        parser_warnings.append("No room-boundary candidates were confirmed on the primary S04 room-boundary layer.")
    if not opening_candidates:
        parser_warnings.append("No opening evidence was retained inside the selected apartment scope.")

    fallback_usage = {
        "selection_bbox_source": selection_source,
        "used_room_label_fallback": any(str(candidate.get("layer_role", "")) != "room_label" for candidate in room_label_candidates),
        "used_boundary_fallback": not any(str(candidate.get("candidate_scope", "")) == "apartment" for candidate in apartment_extent_candidates),
    }

    return {
        "room_label_candidates": room_label_candidates,
        "title_candidates": title_candidates,
        "boundary_candidates": boundary_candidates,
        "apartment_extent_candidates": apartment_extent_candidates,
        "opening_candidates": opening_candidates,
        "selection_bbox": selection_bbox,
        "selection_source": selection_source,
        "parser_warnings": parser_warnings,
        "fallback_usage": fallback_usage,
    }


def expand_bbox(bbox: tuple[float, float, float, float], padding: float) -> tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = bbox
    return min_x - padding, min_y - padding, max_x + padding, max_y + padding


def bbox_from_records(records: list[Record]) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for record in records:
        for x, y, _z in record.points:
            points.append((x, y))
    if not points:
        raise WorkspaceRuleError("Could not derive Apartment A bounding box from anchor records.")
    xs = [item[0] for item in points]
    ys = [item[1] for item in points]
    return min(xs), min(ys), max(xs), max(ys)


def intersects(record_bbox: tuple[float, float, float, float] | None, selection_bbox: tuple[float, float, float, float]) -> bool:
    if record_bbox is None:
        return False
    min_x, min_y, max_x, max_y = record_bbox
    sel_min_x, sel_min_y, sel_max_x, sel_max_y = selection_bbox
    return not (
        max_x < sel_min_x
        or min_x > sel_max_x
        or max_y < sel_min_y
        or min_y > sel_max_y
    )


def collect_block_names_from_records(records: list[Record]) -> set[str]:
    return {record.block_name for record in records if record.record_type == "INSERT" and record.block_name}


def resolve_block_dependencies(initial_names: set[str], block_defs: dict[str, BlockDefinition]) -> list[BlockDefinition]:
    ordered: list[BlockDefinition] = []
    visited: set[str] = set()
    pending = deque(sorted(initial_names))

    while pending:
        block_name = pending.popleft()
        if block_name in visited:
            continue
        visited.add(block_name)

        block_def = block_defs.get(block_name)
        if block_def is None:
            continue

        ordered.append(block_def)
        nested_names = collect_block_names_from_records(block_def.records)
        for nested in sorted(nested_names):
            if nested not in visited:
                pending.append(nested)

    return ordered


def is_excluded_insert_name(block_name: str) -> bool:
    if not block_name:
        return False
    return any(pattern.search(block_name) for pattern in EXCLUDED_INSERT_NAME_PATTERNS)


def matches_text_values(record: Record, patterns: list[re.Pattern[str]]) -> bool:
    if matches_patterns(record.text_blob, patterns):
        return True
    return any(pattern.search(text_value) for text_value in record.text_values for pattern in patterns)


def is_geometry_record_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    geometry_roles = role_names_from_profile(layer_profile, "boundary_roles") | {"structural_fallback"}
    return (
        record.record_type in GEOMETRY_RECORD_TYPES
        and record_matches_layer_roles(record, layer_profile, geometry_roles)
    )


def is_useful_opening_geometry_record_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    return (
        record.record_type in GEOMETRY_RECORD_TYPES
        and record_matches_layer_roles(record, layer_profile, {"opening_evidence"})
    )


def is_useful_insert_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    return (
        record.record_type == "INSERT"
        and record_matches_layer_roles(
            record,
            layer_profile,
            role_names_from_profile(layer_profile, "opening_roles") | {"structural_fallback"},
        )
        and not is_excluded_insert_name(record.block_name)
    )


def is_useful_text_record_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    if record.record_type not in {"MTEXT", "ATTRIB", "TEXT"}:
        return False

    if matches_text_values(record, ROOM_PATTERNS) or matches_text_values(record, TITLE_PATTERNS):
        return True

    if (
        record_matches_layer_roles(record, layer_profile, role_names_from_profile(layer_profile, "opening_roles"))
        and matches_text_values(record, OPENING_TEXT_PATTERNS)
    ):
        return True

    if (
        record_matches_layer_roles(record, layer_profile, {"dimension_fallback"})
        and matches_text_values(record, DIMENSION_TEXT_PATTERNS)
    ):
        return True

    return False


def is_useful_block_record_for_idf(record: Record, layer_profile: dict[str, object]) -> bool:
    if record.record_type in {"BLOCK", "ENDBLK"}:
        return True
    if is_geometry_record_for_idf(record, layer_profile):
        return True
    if is_useful_opening_geometry_record_for_idf(record, layer_profile):
        return True
    if record.record_type == "INSERT" and not is_excluded_insert_name(record.block_name):
        return is_useful_insert_for_idf(record, layer_profile)
    return False


def filter_block_definitions_for_idf(
    block_defs: list[BlockDefinition],
    layer_profile: dict[str, object],
) -> list[BlockDefinition]:
    filtered_defs: list[BlockDefinition] = []
    for block_def in block_defs:
        kept_records = [record for record in block_def.records if is_useful_block_record_for_idf(record, layer_profile)]
        if kept_records:
            filtered_defs.append(BlockDefinition(name=block_def.name, records=kept_records))
    return filtered_defs


def filter_apartment_records(
    records: list[Record],
    selection_bbox: tuple[float, float, float, float],
    layer_profile: dict[str, object],
    parser_candidates: dict[str, object],
) -> tuple[list[Record], dict[str, object]]:
    kept: list[Record] = []
    excluded_counts: Counter[str] = Counter()
    kept_category_counts: Counter[str] = Counter()
    kept_layers: Counter[str] = Counter()
    rejected_layers: Counter[str] = Counter()
    matched_layer_aliases: dict[str, dict[str, object]] = {}
    entities_kept_by_layer: dict[str, dict[str, object]] = {}
    rejected_entities: Counter[str] = Counter()
    opening_role_names = role_names_from_profile(layer_profile, "opening_roles")

    forced_keep_handles = {
        str(candidate.get("label_handle", ""))
        for candidate in list(parser_candidates.get("room_label_candidates", []))
        if candidate.get("label_handle")
    }
    forced_keep_handles.update(
        str(candidate.get("title_handle", ""))
        for candidate in list(parser_candidates.get("title_candidates", []))
        if candidate.get("title_handle")
    )
    forced_keep_handles.update(
        str(candidate.get("handle", ""))
        for candidate in list(parser_candidates.get("boundary_candidates", []))
        if candidate.get("handle")
    )
    forced_keep_handles.update(
        str(candidate.get("handle", ""))
        for candidate in list(parser_candidates.get("opening_candidates", []))
        if candidate.get("handle")
    )
    in_scope_opening_owner_handles = {
        record.handle
        for record in records
        if record.section in {"ENTITIES", "BLOCKS"}
        and record.record_type == "INSERT"
        and record.handle
        and is_useful_insert_for_idf(record, layer_profile)
        and (record.handle in forced_keep_handles or intersects(record.bbox, selection_bbox))
    }

    for record in records:
        if record.section not in {"ENTITIES", "BLOCKS"}:
            continue

        layer_name = record.layer or "<NO_LAYER>"
        classification = classify_record_layer(record, layer_profile)
        primary_match = classification.get("primary")
        matched_roles = {str(match.get("role", "")) for match in classification.get("matches", [])}
        rejected_role_names = [str(match.get("role", "")) for match in classification.get("rejected", [])]

        in_scope = record.handle in forced_keep_handles or intersects(record.bbox, selection_bbox)
        if (
            record.record_type in {"MTEXT", "TEXT", "ATTRIB"}
            and (matches_patterns(record.text_blob, ROOM_PATTERNS) or matches_patterns(record.text_blob, TITLE_PATTERNS))
        ):
            in_scope = True
        if (
            not in_scope
            and record.record_type == "ATTRIB"
            and record_matches_layer_roles(record, layer_profile, opening_role_names)
        ):
            owner_handle = first_group_code_value(record, "330")
            if owner_handle and owner_handle in in_scope_opening_owner_handles:
                in_scope = True

        if not in_scope:
            continue

        category = ""
        rejection_reason = ""
        if is_geometry_record_for_idf(record, layer_profile):
            category = "geometry"
        elif is_useful_opening_geometry_record_for_idf(record, layer_profile):
            category = "opening_geometry"
        elif is_useful_insert_for_idf(record, layer_profile):
            category = "insert"
        elif is_useful_text_record_for_idf(record, layer_profile):
            if record.record_type == "DIMENSION" or "dimension_fallback" in matched_roles:
                category = "auxiliary"
            else:
                category = "text"
        else:
            excluded_counts[record.record_type] += 1
            if rejected_role_names:
                rejection_reason = f"rejected_layer:{'/'.join(sorted(rejected_role_names))}"
            elif primary_match is None:
                rejection_reason = "no_supported_layer_role"
            else:
                rejection_reason = "unsupported_record_role_combination"
            rejected_layers[layer_name] += 1
            rejected_entities[f"{layer_name}|{record.record_type}|{rejection_reason}"] += 1
            continue

        kept.append(record)
        kept_category_counts[category] += 1
        kept_layers[layer_name] += 1
        if isinstance(primary_match, dict) and str(primary_match.get("match_source", "")) in {"alias", "fuzzy"}:
            matched_layer_aliases[layer_name] = {
                "role": primary_match.get("role"),
                "canonical_layer": primary_match.get("canonical_layer"),
                "match_source": primary_match.get("match_source"),
                "match_confidence": primary_match.get("match_confidence"),
            }
        layer_entry = entities_kept_by_layer.setdefault(
            layer_name,
            {
                "count": 0,
                "record_types": Counter(),
                "roles": Counter(),
            },
        )
        layer_entry["count"] = int(layer_entry.get("count", 0) or 0) + 1
        layer_entry["record_types"][record.record_type] += 1
        if isinstance(primary_match, dict) and primary_match.get("role"):
            layer_entry["roles"][str(primary_match["role"])] += 1

    summary: dict[str, object] = {
        "profile": "idf-prep-layer-first",
        "kept_category_counts": counter_to_sorted_dict(kept_category_counts),
        "kept_layers": counter_to_sorted_dict(kept_layers),
        "excluded_record_type_counts": counter_to_sorted_dict(excluded_counts),
        "kept_insert_block_names": sorted(
            {record.block_name for record in kept if record.record_type == "INSERT" and record.block_name}
        ),
        "rejected_layers": counter_to_sorted_dict(rejected_layers),
        "matched_layer_aliases": matched_layer_aliases,
        "entities_kept_by_layer": {
            layer_name: {
                "count": int(payload.get("count", 0) or 0),
                "record_types": counter_to_sorted_dict(payload.get("record_types", Counter())),
                "roles": counter_to_sorted_dict(payload.get("roles", Counter())),
            }
            for layer_name, payload in sorted(entities_kept_by_layer.items())
        },
        "entities_rejected": [
            {
                "layer": key.split("|", 2)[0],
                "record_type": key.split("|", 2)[1],
                "reason": key.split("|", 2)[2],
                "count": int(count),
            }
            for key, count in sorted(rejected_entities.items())
        ],
        "room_label_candidates": list(parser_candidates.get("room_label_candidates", [])),
        "title_candidates": list(parser_candidates.get("title_candidates", [])),
        "boundary_candidates": list(parser_candidates.get("boundary_candidates", [])),
        "apartment_extent_candidates": list(parser_candidates.get("apartment_extent_candidates", [])),
        "opening_candidates": list(parser_candidates.get("opening_candidates", [])),
        "selection_source": parser_candidates.get("selection_source"),
        "parser_warnings": list(parser_candidates.get("parser_warnings", [])),
        "fallback_usage": dict(parser_candidates.get("fallback_usage", {})),
    }
    return kept, summary


def render_output(
    source_path: Path,
    output_path: Path,
    selection_bbox: tuple[float, float, float, float],
    room_anchor_count: int,
    title_anchor_count: int,
    kept_records: list[Record],
    block_defs: list[BlockDefinition],
    filter_summary: dict[str, object],
) -> str:
    lines: list[str] = []
    lines.append(f"# Source: {source_path.relative_to(ROOT)}")
    lines.append(f"# Output: {output_path.relative_to(ROOT)}")
    lines.append(f"# Filter profile: {filter_summary.get('profile', 'idf-prep')}")
    lines.append(f"# Room anchors: {room_anchor_count}")
    lines.append(f"# Title anchors: {title_anchor_count}")
    lines.append(
        "# Selection bbox: "
        f"{selection_bbox[0]:.3f}, {selection_bbox[1]:.3f}, {selection_bbox[2]:.3f}, {selection_bbox[3]:.3f}"
    )
    lines.append(f"# Kept records: {len(kept_records)}")
    lines.append(f"# Referenced block definitions: {len(block_defs)}")
    lines.append(
        "# Kept categories: "
        + ", ".join(f"{key}={value}" for key, value in (filter_summary.get("kept_category_counts", {}) or {}).items())
    )
    lines.append(
        "# Kept layers: "
        + ", ".join(f"{key}={value}" for key, value in (filter_summary.get("kept_layers", {}) or {}).items())
    )
    lines.append(
        "# Excluded record types: "
        + ", ".join(
            f"{key}={value}" for key, value in (filter_summary.get("excluded_record_type_counts", {}) or {}).items()
        )
    )
    lines.append(f"# Rejected layers: {json_metadata_text(filter_summary.get('rejected_layers', {}))}")
    lines.append(f"# Matched layer aliases: {json_metadata_text(filter_summary.get('matched_layer_aliases', {}))}")
    lines.append(f"# Entities kept by layer: {json_metadata_text(filter_summary.get('entities_kept_by_layer', {}))}")
    lines.append(f"# Entities rejected: {json_metadata_text(filter_summary.get('entities_rejected', []))}")
    lines.append(f"# Room label candidates: {json_metadata_text(filter_summary.get('room_label_candidates', []))}")
    lines.append(f"# Title candidates: {json_metadata_text(filter_summary.get('title_candidates', []))}")
    lines.append(f"# Boundary candidates: {json_metadata_text(filter_summary.get('boundary_candidates', []))}")
    lines.append(
        f"# Apartment extent candidates: {json_metadata_text(filter_summary.get('apartment_extent_candidates', []))}"
    )
    lines.append(f"# Opening candidates: {json_metadata_text(filter_summary.get('opening_candidates', []))}")
    lines.append(f"# Selection source: {filter_summary.get('selection_source', '')}")
    lines.append(f"# Fallback usage: {json_metadata_text(filter_summary.get('fallback_usage', {}))}")
    lines.append(f"# Parser warnings: {json_metadata_text(filter_summary.get('parser_warnings', []))}")
    lines.append("")

    lines.append("# BEGIN BLOCK DEFINITIONS")
    for block_def in block_defs:
        lines.append(f"# BLOCK: {block_def.name}")
        lines.extend(block_def.raw_lines)
        lines.append("")
    lines.append("# END BLOCK DEFINITIONS")
    lines.append("")

    lines.append("# BEGIN FILTERED RECORDS")
    for record in kept_records:
        lines.extend(record.raw_lines)
        lines.append("")
    lines.append("# END FILTERED RECORDS")
    lines.append("")
    return "\n".join(lines)


def counter_to_sorted_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def load_csv_dict_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            {key: (value.strip() if isinstance(value, str) else "") for key, value in row.items()}
            for row in reader
        ]


def run_workspace_pipeline_from_mapping(
    mapping_payload: dict[str, object],
    *,
    project_id: str | None = None,
    layer_profile_path: Path | str = DEFAULT_DXF_LAYER_PROFILE_PATH,
    geometry_policy_path: Path | str = DEFAULT_APARTMENT_A_GEOMETRY_POLICY,
    ceiling_height_m: float | None = None,
    zone_output_prefix: str = DEFAULT_ZONE_OUTPUT_PREFIX,
    object_output_prefix: str = "",
    geometry_output_dir: Path | str | None = None,
    surface_output_dir: Path | str | None = None,
    wall_output_dir: Path | str | None = None,
    fenestration_output_dir: Path | str | None = None,
) -> None:
    resolved_project_id = path_resolver.resolve_project_id(project_id) if (
        project_id is not None
        or geometry_output_dir is None
        or surface_output_dir is None
        or wall_output_dir is None
        or fenestration_output_dir is None
    ) else None
    resolved_geometry_output_dir = geometry_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/geometry")
    resolved_surface_output_dir = surface_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/surfaces")
    resolved_wall_output_dir = wall_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/walls")
    resolved_fenestration_output_dir = fenestration_output_dir or path_resolver.resolve_output_file(
        resolved_project_id,
        "intermediate/fenestration",
    )

    geometry_payload = infer_apartment_a_geometry(
        mapping_payload=mapping_payload,
        policy_path=geometry_policy_path,
        ceiling_height_m=ceiling_height_m,
        zone_output_prefix=zone_output_prefix,
    )
    if geometry_payload is None:
        raise WorkspaceRuleError("IDF bundle generation requires geometry inference outputs.")
    if object_output_prefix:
        geometry_payload["object_output_prefix"] = str(object_output_prefix).strip().rstrip("_")

    write_geometry_outputs(
        geometry_payload,
        output_dir=resolved_geometry_output_dir,
        project_id=resolved_project_id,
    )

    surface_artifacts = build_surface_artifacts(
        geometry_payload=geometry_payload,
    )
    write_surface_outputs(
        surface_artifacts,
        output_dir=resolved_surface_output_dir,
        project_id=resolved_project_id,
    )

    wall_artifacts = build_wall_artifacts(
        surface_rows=list(surface_artifacts.get("surface_rows", [])),
        mapping_payload=mapping_payload,
        geometry_payload=geometry_payload,
        dimension_annotations=list(mapping_payload.get("dimension_annotations", [])),
        layer_profile_path=layer_profile_path,
    )
    write_wall_outputs(
        wall_artifacts,
        output_dir=resolved_wall_output_dir,
        project_id=resolved_project_id,
    )

    fenestration_artifacts = build_fenestration_artifacts(
        mapping_payload=mapping_payload,
        opening_candidates=list(mapping_payload.get("candidate_openings", [])),
        geometry_payload=geometry_payload,
        surface_rows=list(wall_artifacts.get("surface_rows", [])),
        wall_inventory_rows=list(wall_artifacts.get("wall_inventory_rows", [])),
        wall_resolution=dict(wall_artifacts.get("wall_resolution", {})),
        project_id=project_id,
    )
    write_fenestration_outputs(
        fenestration_artifacts,
        output_dir=resolved_fenestration_output_dir,
        project_id=resolved_project_id,
    )


def resolve_bundle_source_path(path: Path | str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = GUARD.assert_read_path(candidate)
    if not resolved.exists():
        raise WorkspaceRuleError(f"Bundle source artifact is missing: {workspace_path(resolved)}")
    return resolved


def build_idf_input_bundle_artifacts(
    mapping_payload: dict[str, object],
    *,
    project_id: str | None = None,
    mapping_payload_path: Path | str | None = None,
    layer_profile_path: Path | str = DEFAULT_DXF_LAYER_PROFILE_PATH,
    geometry_policy_path: Path | str = DEFAULT_APARTMENT_A_GEOMETRY_POLICY,
    ceiling_height_m: float | None = None,
    zone_output_prefix: str = DEFAULT_ZONE_OUTPUT_PREFIX,
    object_output_prefix: str = "",
    geometry_output_dir: Path | str | None = None,
    surface_output_dir: Path | str | None = None,
    wall_output_dir: Path | str | None = None,
    fenestration_output_dir: Path | str | None = None,
) -> dict[str, object]:
    run_workspace_pipeline_from_mapping(
        mapping_payload,
        project_id=project_id,
        layer_profile_path=layer_profile_path,
        geometry_policy_path=geometry_policy_path,
        ceiling_height_m=ceiling_height_m,
        zone_output_prefix=zone_output_prefix,
        object_output_prefix=object_output_prefix,
        geometry_output_dir=geometry_output_dir,
        surface_output_dir=surface_output_dir,
        wall_output_dir=wall_output_dir,
        fenestration_output_dir=fenestration_output_dir,
    )
    resolved_project_id = path_resolver.resolve_project_id(project_id) if (
        project_id is not None
        or mapping_payload_path is None
        or geometry_output_dir is None
        or surface_output_dir is None
        or wall_output_dir is None
        or fenestration_output_dir is None
    ) else None
    resolved_mapping_payload_path = resolve_bundle_source_path(
        mapping_payload_path or _resolve_default_mapping_payload_path(resolved_project_id)
    )
    geometry_output_path = GUARD.resolve(geometry_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/geometry"))
    surface_output_path = GUARD.resolve(surface_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/surfaces"))
    wall_output_path = GUARD.resolve(wall_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/walls"))
    fenestration_output_path = GUARD.resolve(
        fenestration_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/fenestration")
    )
    geometry_payload_path = resolve_bundle_source_path(geometry_output_path / "geometry_payload.json")
    surface_rows_path = resolve_bundle_source_path(surface_output_path / "surface_rows.json")
    wall_inventory_path = resolve_bundle_source_path(wall_output_path / "wall_inventory.json")
    wall_resolution_path = resolve_bundle_source_path(wall_output_path / "wall_resolution.json")
    fenestration_rows_path = resolve_bundle_source_path(fenestration_output_path / "fenestration_rows.json")
    opening_host_mapping_path = fenestration_output_path / "opening_host_mapping.json"
    return build_bundle_writer_artifacts_from_paths(
        mapping_payload_path=resolved_mapping_payload_path,
        geometry_payload_path=geometry_payload_path,
        surface_rows_path=surface_rows_path,
        wall_inventory_path=wall_inventory_path,
        wall_resolution_path=wall_resolution_path,
        fenestration_rows_path=fenestration_rows_path,
        opening_host_mapping_path=(
            resolve_bundle_source_path(opening_host_mapping_path)
            if opening_host_mapping_path.exists()
            else None
        ),
        project_id=resolved_project_id,
    )


def default_bundle_intermediate_dir(bundle_output_dir: Path) -> Path:
    resolved_bundle_output_dir = GUARD.resolve(bundle_output_dir)
    project_id = str(resolved_bundle_output_dir.relative_to(ROOT / "5_output")).split("\\", 1)[0].split("/", 1)[0]
    if not project_id or project_id in path_resolver.GLOBAL_OUTPUT_CATEGORIES:
        raise WorkspaceRuleError(
            f"Bundle output dir must live under 5_output/<project_id>/csv/: {workspace_path(resolved_bundle_output_dir)}"
        )
    return path_resolver.resolve_output_file(project_id, "intermediate", resolved_bundle_output_dir.name)


def write_idf_input_bundle(
    bundle_output_dir: Path,
    mapping_payload: dict[str, object],
    *,
    project_id: str | None = None,
    mapping_payload_path: Path | str | None = None,
    layer_profile_path: Path | str = DEFAULT_DXF_LAYER_PROFILE_PATH,
    geometry_policy_path: Path | str = DEFAULT_APARTMENT_A_GEOMETRY_POLICY,
    ceiling_height_m: float | None = None,
    zone_output_prefix: str = DEFAULT_ZONE_OUTPUT_PREFIX,
    object_output_prefix: str = "",
    geometry_output_dir: Path | str | None = None,
    surface_output_dir: Path | str | None = None,
    wall_output_dir: Path | str | None = None,
    fenestration_output_dir: Path | str | None = None,
) -> tuple[list[Path], list[Path]]:
    bundle_artifacts = build_idf_input_bundle_artifacts(
        mapping_payload,
        project_id=project_id,
        mapping_payload_path=mapping_payload_path,
        layer_profile_path=layer_profile_path,
        geometry_policy_path=geometry_policy_path,
        ceiling_height_m=ceiling_height_m,
        zone_output_prefix=zone_output_prefix,
        object_output_prefix=object_output_prefix,
        geometry_output_dir=geometry_output_dir,
        surface_output_dir=surface_output_dir,
        wall_output_dir=wall_output_dir,
        fenestration_output_dir=fenestration_output_dir,
    )
    return write_bundle_writer_outputs(
        bundle_output_dir=bundle_output_dir,
        bundle_artifacts=bundle_artifacts,
        project_id=project_id,
    )


def flatten_block_records(block_defs: list[BlockDefinition]) -> list[Record]:
    records: list[Record] = []
    for block_def in block_defs:
        records.extend(block_def.records)
    return records


def collect_point_extents(records: list[Record]) -> dict[str, list[float]] | None:
    points = [point for record in records for point in record.points]
    if not points:
        return None

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    return {
        "min_xyz": [min(xs), min(ys), min(zs)],
        "max_xyz": [max(xs), max(ys), max(zs)],
    }


def build_section_summary(section_name: str, records: list[Record]) -> dict[str, object]:
    record_counts = Counter(record.record_type for record in records)
    return {
        "section_name": section_name,
        "pair_count": sum(len(record.raw_lines) // 2 for record in records),
        "record_count": len(records),
        "record_type_count": len(record_counts),
        "record_counts": counter_to_sorted_dict(record_counts),
    }


def build_record_type_schema(records: list[Record]) -> dict[str, dict[str, object]]:
    details: dict[str, dict[str, object]] = {}

    for record in records:
        detail = details.setdefault(
            record.record_type,
            {
                "count": 0,
                "group_codes": set(),
                "sample_values": {},
                "layers": Counter(),
                "source_sections": Counter(),
                "text_record_count": 0,
                "point_record_count": 0,
            },
        )
        detail["count"] += 1
        detail["source_sections"][record.section] += 1
        if record.layer:
            detail["layers"][record.layer] += 1
        if record.text_values:
            detail["text_record_count"] += 1
        if record.points:
            detail["point_record_count"] += 1

        sample_values = detail["sample_values"]
        group_codes = detail["group_codes"]
        for index in range(0, len(record.raw_lines), 2):
            code = record.raw_lines[index].strip()
            if not code.lstrip("-").isdigit():
                continue
            group_codes.add(int(code))
            value = record.raw_lines[index + 1].strip() if index + 1 < len(record.raw_lines) else ""
            if not value:
                continue
            bucket = sample_values.setdefault(code, [])
            if value not in bucket and len(bucket) < 3:
                bucket.append(value)

    normalized: dict[str, dict[str, object]] = {}
    for record_type, detail in sorted(details.items(), key=lambda item: (-int(item[1]["count"]), item[0])):
        sample_values = detail["sample_values"]
        normalized[record_type] = {
            "count": int(detail["count"]),
            "source_sections": counter_to_sorted_dict(detail["source_sections"]),
            "layers": counter_to_sorted_dict(detail["layers"]),
            "text_record_count": int(detail["text_record_count"]),
            "point_record_count": int(detail["point_record_count"]),
            "group_codes": sorted(detail["group_codes"]),
            "sample_values": {
                code: values
                for code, values in sorted(sample_values.items(), key=lambda item: int(item[0]))
            },
        }

    return normalized


def build_layer_schema(records: list[Record]) -> list[dict[str, object]]:
    per_layer: dict[str, Counter[str]] = {}
    for record in records:
        layer_name = record.layer or "<NO_LAYER>"
        per_layer.setdefault(layer_name, Counter()).update([record.record_type])

    rows: list[dict[str, object]] = []
    for layer_name, counter in sorted(
        per_layer.items(),
        key=lambda item: (-sum(item[1].values()), item[0]),
    ):
        rows.append(
            {
                "layer": layer_name,
                "record_count": sum(counter.values()),
                "record_types": counter_to_sorted_dict(counter),
            }
        )
    return rows


def build_block_definition_schema(block_defs: list[BlockDefinition]) -> dict[str, object]:
    return {
        "count": len(block_defs),
        "names": [block_def.name for block_def in block_defs],
        "blocks": [
            {
                "block_name": block_def.name,
                "record_count": len(block_def.records),
                "record_type_count": len({record.record_type for record in block_def.records}),
                "record_counts": counter_to_sorted_dict(Counter(record.record_type for record in block_def.records)),
            }
            for block_def in block_defs
        ],
    }


def build_extract_schema(
    *,
    source_path: Path,
    output_path: Path,
    padding: float,
    selection_bbox: tuple[float, float, float, float],
    room_anchor_count: int,
    title_anchor_count: int,
    kept_records: list[Record],
    block_defs: list[BlockDefinition],
    filter_summary: dict[str, object],
    payload: str,
) -> dict[str, object]:
    block_records = flatten_block_records(block_defs)
    all_records = [*block_records, *kept_records]
    record_extents = collect_point_extents(all_records)
    total_record_types = Counter(record.record_type for record in all_records)
    referenced_block_names = collect_block_names_from_records(kept_records)
    payload_lines = payload.splitlines()
    actual_file_schema_notes = [
        "This file is a filtered extract generated from the raw Apartment A DXF text source.",
        "Lines starting with # are extraction metadata and section markers, not DXF group-code/value pairs.",
        "Raw DXF records are preserved under BLOCK_DEFINITIONS and FILTERED_RECORDS sections.",
    ]

    file_profile: dict[str, object] = {
        "extract_variant": "Apartment A filtered DXF text extract",
        "generator": workspace_path(Path(__file__).resolve()),
        "filter_profile": str(filter_summary.get("profile", "idf-prep")),
        "section_order": ["BLOCK_DEFINITIONS", "FILTERED_RECORDS"],
        "selection_bbox_xy": list(selection_bbox),
        "padding": padding,
    }
    if record_extents:
        file_profile.update(record_extents)

    extraction_profile: dict[str, object] = {
        "filter_profile": str(filter_summary.get("profile", "idf-prep")),
        "room_anchor_count": room_anchor_count,
        "title_anchor_count": title_anchor_count,
        "selection_bbox_xy": list(selection_bbox),
        "padding": padding,
        "kept_record_count": len(kept_records),
        "referenced_block_definition_count": len(block_defs),
        "referenced_block_names": sorted(referenced_block_names),
        "kept_category_counts": filter_summary.get("kept_category_counts", {}),
        "kept_layers": filter_summary.get("kept_layers", {}),
        "excluded_record_type_counts": filter_summary.get("excluded_record_type_counts", {}),
        "rejected_layers": filter_summary.get("rejected_layers", {}),
        "matched_layer_aliases": filter_summary.get("matched_layer_aliases", {}),
        "entities_kept_by_layer": filter_summary.get("entities_kept_by_layer", {}),
        "entities_rejected": filter_summary.get("entities_rejected", []),
        "room_label_candidates": filter_summary.get("room_label_candidates", []),
        "title_candidates": filter_summary.get("title_candidates", []),
        "boundary_candidates": filter_summary.get("boundary_candidates", []),
        "apartment_extent_candidates": filter_summary.get("apartment_extent_candidates", []),
        "opening_candidates": filter_summary.get("opening_candidates", []),
        "selection_source": filter_summary.get("selection_source"),
        "fallback_usage": filter_summary.get("fallback_usage", {}),
        "parser_warnings": filter_summary.get("parser_warnings", []),
    }

    return {
        "source_file": {
            "filename": output_path.name,
            "path": workspace_path(output_path),
            "size_bytes": len(payload.encode("utf-8")),
            "detected_format": "Filtered Apartment A DXF text extract with metadata header",
        },
        "upstream_source": {
            "filename": source_path.name,
            "path": workspace_path(source_path),
            "detected_format": "AutoCAD DXF text source",
        },
        "extraction_profile": extraction_profile,
        "file_profile": file_profile,
        "top_level_counts": {
            "total_lines": len(payload_lines),
            "total_group_code_pairs": sum(len(record.raw_lines) // 2 for record in all_records),
            "section_count": 2,
            "metadata_comment_line_count": sum(1 for line in payload_lines if line.startswith("#")),
            "block_definition_count": len(block_defs),
            "block_definition_record_count": len(block_records),
            "filtered_record_count": len(kept_records),
            "unique_record_type_count": len(total_record_types),
            "unique_layer_count": len({record.layer for record in all_records if record.layer}),
            "text_record_count": sum(1 for record in all_records if record.text_values),
        },
        "actual_file_schema": {
            "notes": actual_file_schema_notes,
            "sections": [
                build_section_summary("BLOCK_DEFINITIONS", block_records),
                build_section_summary("FILTERED_RECORDS", kept_records),
            ],
            "record_types": build_record_type_schema(all_records),
            "layers": build_layer_schema(all_records),
            "block_definitions": build_block_definition_schema(block_defs),
        },
        "normalized_relational_schema": {
            "extraction_metadata": {
                "source_section": "Leading comment header",
                "row_granularity": "One row per metadata key emitted by the extraction script",
                "columns": ["key", "value"],
            },
            "block_definition_records": {
                "source_section": "BLOCK_DEFINITIONS",
                "row_granularity": "One row per DXF record inside a referenced block definition",
                "columns": [
                    "block_name",
                    "record_type",
                    "layer",
                    "handle",
                    "text_values[]",
                    "point_count",
                    "raw_group_codes",
                    "raw_payload",
                ],
            },
            "filtered_records": {
                "source_section": "FILTERED_RECORDS",
                "row_granularity": "One row per kept DXF record related to Apartment A",
                "columns": [
                    "source_section",
                    "record_type",
                    "layer",
                    "handle",
                    "block_name",
                    "text_values[]",
                    "point_count",
                    "bbox_min_x",
                    "bbox_min_y",
                    "bbox_max_x",
                    "bbox_max_y",
                    "raw_group_codes",
                    "raw_payload",
                ],
            },
            "record_points": {
                "source_section": "BLOCK_DEFINITIONS + FILTERED_RECORDS",
                "row_granularity": "One row per parsed point from a kept record",
                "columns": ["record_scope", "record_type", "handle", "point_order", "x", "y", "z"],
            },
            "text_values": {
                "source_section": "BLOCK_DEFINITIONS + FILTERED_RECORDS",
                "row_granularity": "One row per extracted text value from group codes 1 or 3",
                "columns": ["record_scope", "record_type", "handle", "text_order", "text_value"],
            },
        },
    }


def run_pipeline(
    *,
    project_id: str | None = None,
    input_path: Path | str | None = None,
    output_path: Path | str | None = None,
    schema_output_path: Path | str | None = None,
    mapping_output_path: Path | str | None = None,
    mapping_output_dir: Path | str | None = None,
    idf_bundle_output_dir: Path | str | None = None,
    rebuilt_idf_output_path: Path | str | None = None,
    padding: float = 2500.0,
    room_pattern_texts: list[str] | tuple[str, ...] = DEFAULT_ROOM_PATTERN_TEXTS,
    title_pattern_texts: list[str] | tuple[str, ...] = DEFAULT_TITLE_PATTERN_TEXTS,
    geometry_policy_path: Path | str = DEFAULT_APARTMENT_A_GEOMETRY_POLICY,
    ceiling_height_m: float | None = None,
    layer_profile_path: Path | str = DEFAULT_DXF_LAYER_PROFILE_PATH,
    zone_output_prefix: str = DEFAULT_ZONE_OUTPUT_PREFIX,
    object_output_prefix: str = "",
    dxf_normalized_output_dir: Path | str | None = None,
    geometry_output_dir: Path | str | None = None,
    surface_output_dir: Path | str | None = None,
    wall_output_dir: Path | str | None = None,
    fenestration_output_dir: Path | str | None = None,
) -> dict[str, object]:
    configure_anchor_patterns(room_pattern_texts, title_pattern_texts)
    resolved_project_id = path_resolver.resolve_project_id(project_id)
    resolved_input_default = _resolve_default_pipeline_input(resolved_project_id)

    input_path = input_path or resolved_input_default
    output_path = output_path or _resolve_default_pipeline_output(resolved_project_id, resolved_input_default)
    schema_output_path = schema_output_path or _resolve_default_pipeline_schema_output(
        resolved_project_id,
        resolved_input_default,
    )
    mapping_output_dir = mapping_output_dir or _resolve_default_mapping_output_dir(resolved_project_id)
    if idf_bundle_output_dir is not None:
        idf_bundle_output_dir = idf_bundle_output_dir
    rebuilt_idf_output_path = rebuilt_idf_output_path or _resolve_default_rebuilt_idf_output(
        resolved_project_id,
        resolved_input_default,
    )
    dxf_normalized_output_dir = dxf_normalized_output_dir or path_resolver.resolve_output_file(
        resolved_project_id,
        "normalized/dxf",
    )
    geometry_output_dir = geometry_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/geometry")
    surface_output_dir = surface_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/surfaces")
    wall_output_dir = wall_output_dir or path_resolver.resolve_output_file(resolved_project_id, "intermediate/walls")
    fenestration_output_dir = fenestration_output_dir or path_resolver.resolve_output_file(
        resolved_project_id,
        "intermediate/fenestration",
    )
    if idf_bundle_output_dir is not None:
        idf_bundle_output_dir = GUARD.resolve(idf_bundle_output_dir)
        path_resolver.assert_output_in_project_scope(resolved_project_id, idf_bundle_output_dir)

    resolved_input_path = GUARD.assert_read_path(input_path)
    resolved_output_path = GUARD.resolve(output_path)
    path_resolver.assert_output_in_project_scope(resolved_project_id, resolved_output_path)
    GUARD.assert_write_path(
        resolved_output_path,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )

    resolved_schema_output_path: Path | None = None
    if schema_output_path is not None:
        path_resolver.assert_output_in_project_scope(resolved_project_id, GUARD.resolve(schema_output_path))
        resolved_schema_output_path = GUARD.assert_write_path(
            schema_output_path,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )

    resolved_mapping_output_path: Path | None = None
    if mapping_output_path is not None:
        resolved_mapping_output_path = GUARD.assert_write_path(
            mapping_output_path,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )

    resolved_mapping_output_dir = GUARD.resolve(mapping_output_dir)
    path_resolver.assert_output_in_project_scope(resolved_project_id, resolved_mapping_output_dir)
    GUARD.assert_write_path(
        resolved_mapping_output_dir / "mapping_payload.json",
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )

    resolved_idf_bundle_output_dir: Path | None = None
    if idf_bundle_output_dir is not None:
        resolved_idf_bundle_output_dir = GUARD.resolve(idf_bundle_output_dir)
        if ceiling_height_m is None:
            raise WorkspaceRuleError("IDF build requires human-provided --ceiling-height-m.")
        ceiling_height_m = float(ceiling_height_m)
        if ceiling_height_m <= 0.0:
            raise WorkspaceRuleError("--ceiling-height-m must be greater than 0.")
        GUARD.assert_write_path(
            resolved_idf_bundle_output_dir / "Version.csv",
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )

    resolved_geometry_policy_path = GUARD.resolve(geometry_policy_path)
    resolved_layer_profile_path = GUARD.assert_read_path(layer_profile_path)
    resolved_rebuilt_idf_output_path = GUARD.resolve(rebuilt_idf_output_path)
    path_resolver.assert_output_in_project_scope(resolved_project_id, resolved_rebuilt_idf_output_path)
    resolved_dxf_normalized_output_dir = GUARD.resolve(dxf_normalized_output_dir)
    resolved_geometry_output_dir = GUARD.resolve(geometry_output_dir)
    resolved_surface_output_dir = GUARD.resolve(surface_output_dir)
    resolved_wall_output_dir = GUARD.resolve(wall_output_dir)
    resolved_fenestration_output_dir = GUARD.resolve(fenestration_output_dir)
    for scoped_output_dir in (
        resolved_dxf_normalized_output_dir,
        resolved_geometry_output_dir,
        resolved_surface_output_dir,
        resolved_wall_output_dir,
        resolved_fenestration_output_dir,
    ):
        path_resolver.assert_output_in_project_scope(resolved_project_id, scoped_output_dir)

    geometry_policy_for_mapping = json.loads(
        GUARD.assert_read_path(resolved_geometry_policy_path).read_text(encoding="utf-8")
    )
    if not isinstance(geometry_policy_for_mapping, dict):
        raise WorkspaceRuleError(
            f"Geometry policy must be a JSON object: {workspace_path(resolved_geometry_policy_path)}"
        )
    mapping_zone_name_aliases = dict(geometry_policy_for_mapping.get("zone_name_aliases", {}))

    layer_profile = load_layer_profile(resolved_layer_profile_path)
    parsed_dxf = parse_dxf_file(resolved_input_path, layer_profile_path=resolved_layer_profile_path)
    write_dxf_outputs(parsed_dxf, output_dir=resolved_dxf_normalized_output_dir)
    records = list(parsed_dxf.get("records", []))
    parser_candidates = build_parser_candidates(records, layer_profile, padding=padding)
    records_by_handle = {record.handle: record for record in records if record.handle}
    room_records = [
        records_by_handle[handle]
        for handle in [
            candidate.get("label_handle")
            for candidate in list(parser_candidates.get("room_label_candidates", []))
            if candidate.get("label_handle") in records_by_handle
        ]
    ]
    title_records = [
        records_by_handle[handle]
        for handle in [
            candidate.get("title_handle")
            for candidate in list(parser_candidates.get("title_candidates", []))
            if candidate.get("title_handle") in records_by_handle
        ]
    ]
    if not room_records and not title_records:
        raise WorkspaceRuleError("No Apartment A anchors were found in the input file.")

    selection_bbox = tuple(float(value) for value in parser_candidates.get("selection_bbox", []))
    if len(selection_bbox) != 4:
        raise WorkspaceRuleError("Parser candidates did not produce a valid Apartment A selection bbox.")
    kept_records, filter_summary = filter_apartment_records(records, selection_bbox, layer_profile, parser_candidates)

    if not kept_records:
        raise WorkspaceRuleError("No DXF records matched the Apartment A selection bbox.")

    block_defs = build_block_definitions(records)
    referenced_block_defs = resolve_block_dependencies(collect_block_names_from_records(kept_records), block_defs)
    referenced_block_defs = filter_block_definitions_for_idf(referenced_block_defs, layer_profile)

    payload = render_output(
        source_path=resolved_input_path,
        output_path=resolved_output_path,
        selection_bbox=selection_bbox,
        room_anchor_count=len(room_records),
        title_anchor_count=len(title_records),
        kept_records=kept_records,
        block_defs=referenced_block_defs,
        filter_summary=filter_summary,
    )

    GUARD.write_text(
        resolved_output_path,
        payload,
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )

    if resolved_schema_output_path is not None:
        schema_payload = build_extract_schema(
            source_path=resolved_input_path,
            output_path=resolved_output_path,
            padding=padding,
            selection_bbox=selection_bbox,
            room_anchor_count=len(room_records),
            title_anchor_count=len(title_records),
            kept_records=kept_records,
            block_defs=referenced_block_defs,
            filter_summary=filter_summary,
            payload=payload,
        )
        GUARD.write_json(
            resolved_schema_output_path,
            schema_payload,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )

    mapping_payload: dict[str, object] | None = None
    mapping_written_paths: dict[str, str] = {}
    if resolved_mapping_output_path is not None or resolved_idf_bundle_output_dir is not None:
        mapping_artifacts = build_context_mapping_artifacts(
            dxf_extract_path=resolved_output_path,
            layer_profile_path=resolved_layer_profile_path,
            zone_name_aliases=mapping_zone_name_aliases,
        )
        mapping_written_paths = write_context_mapping_outputs(
            mapping_artifacts,
            output_dir=resolved_mapping_output_dir,
            legacy_payload_path=resolved_mapping_output_path,
            project_id=resolved_project_id,
        )
        mapping_payload = dict(mapping_artifacts.get("mapping_payload", {}))

    bundle_written_paths: list[Path] = []
    bundle_intermediate_paths: list[Path] = []
    rebuilt_idf_path: Path | None = None
    if resolved_idf_bundle_output_dir is not None:
        if mapping_payload is None:
            raise WorkspaceRuleError("IDF bundle generation requires a mapping payload.")
        bundle_written_paths, bundle_intermediate_paths = write_idf_input_bundle(
            resolved_idf_bundle_output_dir,
            mapping_payload,
            project_id=resolved_project_id,
            mapping_payload_path=mapping_written_paths.get("mapping_payload"),
            layer_profile_path=resolved_layer_profile_path,
            geometry_policy_path=resolved_geometry_policy_path,
            ceiling_height_m=ceiling_height_m,
            zone_output_prefix=zone_output_prefix,
            object_output_prefix=object_output_prefix,
            geometry_output_dir=resolved_geometry_output_dir,
            surface_output_dir=resolved_surface_output_dir,
            wall_output_dir=resolved_wall_output_dir,
            fenestration_output_dir=resolved_fenestration_output_dir,
        )
        rebuilt_idf_path = rebuild_idf_from_bundle_writer(
            resolved_idf_bundle_output_dir,
            output_path=resolved_rebuilt_idf_output_path,
            wall_resolution_path=resolved_wall_output_dir / "wall_resolution.json",
            project_id=resolved_project_id,
        )

    print("APARTMENT_A_EXTRACTION_COMPLETE")
    print(f"Source: {resolved_input_path.relative_to(ROOT)}")
    print(f"Output: {resolved_output_path.relative_to(ROOT)}")
    print(f"Room anchors: {len(room_records)}")
    print(f"Title anchors: {len(title_records)}")
    print(
        "Selection bbox: "
        f"{selection_bbox[0]:.3f}, {selection_bbox[1]:.3f}, {selection_bbox[2]:.3f}, {selection_bbox[3]:.3f}"
    )
    print(f"Selection source: {filter_summary.get('selection_source', '')}")
    print(f"Kept records: {len(kept_records)}")
    print(f"Referenced block definitions: {len(referenced_block_defs)}")
    print(f"Kept categories: {filter_summary.get('kept_category_counts', {})}")
    print(f"Parser warnings: {len(list(filter_summary.get('parser_warnings', [])))}")
    if resolved_schema_output_path is not None:
        print(f"Schema: {resolved_schema_output_path.relative_to(ROOT)}")
    if mapping_written_paths:
        print(f"Mapping payload: {mapping_written_paths.get('mapping_payload', '')}")
        print(f"Mapping summary: {mapping_written_paths.get('mapping_summary', '')}")
    if resolved_mapping_output_path is not None:
        print(f"Mapping mirror: {resolved_mapping_output_path.relative_to(ROOT)}")
    if resolved_idf_bundle_output_dir is not None:
        geometry_payload_path = resolved_geometry_output_dir / "geometry_payload.json"
        geometry_zone_rectangles_path = resolved_geometry_output_dir / "zone_rectangles.json"
        geometry_partition_summary_path = resolved_geometry_output_dir / "partition_summary.json"
        surface_rows_path = resolved_surface_output_dir / "surface_rows.json"
        surface_adjacency_summary_path = resolved_surface_output_dir / "adjacency_summary.json"
        wall_inventory_path = resolved_wall_output_dir / "wall_inventory.json"
        wall_resolution_path = resolved_wall_output_dir / "wall_resolution.json"
        if geometry_payload_path.exists():
            print(f"Geometry payload: {workspace_path(geometry_payload_path)}")
        if geometry_zone_rectangles_path.exists():
            print(f"Geometry zone rectangles: {workspace_path(geometry_zone_rectangles_path)}")
        if geometry_partition_summary_path.exists():
            print(f"Geometry partition summary: {workspace_path(geometry_partition_summary_path)}")
        if surface_rows_path.exists():
            print(f"Surface rows: {workspace_path(surface_rows_path)}")
        if surface_adjacency_summary_path.exists():
            print(f"Surface adjacency summary: {workspace_path(surface_adjacency_summary_path)}")
        if wall_inventory_path.exists():
            print(f"Wall inventory: {workspace_path(wall_inventory_path)}")
        if wall_resolution_path.exists():
            print(f"Wall resolution: {workspace_path(wall_resolution_path)}")
        print(f"IDF input bundle: {resolved_idf_bundle_output_dir.relative_to(ROOT)}")
        print(f"Bundle files: {len(bundle_written_paths)}")
        if bundle_intermediate_paths:
            print(
                "Bundle intermediate artifacts: "
                f"{default_bundle_intermediate_dir(resolved_idf_bundle_output_dir).relative_to(ROOT)}"
            )
        if rebuilt_idf_path is not None:
            print(f"Rebuilt IDF: {rebuilt_idf_path.relative_to(ROOT)}")

    return {
        "input_path": resolved_input_path,
        "extract_output_path": resolved_output_path,
        "schema_output_path": resolved_schema_output_path,
        "mapping_output_dir": resolved_mapping_output_dir,
        "bundle_output_dir": resolved_idf_bundle_output_dir,
        "rebuilt_idf_output_path": rebuilt_idf_path,
        "room_anchor_count": len(room_records),
        "title_anchor_count": len(title_records),
        "selection_bbox": selection_bbox,
        "kept_record_count": len(kept_records),
        "referenced_block_definition_count": len(referenced_block_defs),
        "mapping_written_paths": mapping_written_paths,
        "bundle_written_paths": bundle_written_paths,
        "bundle_intermediate_paths": bundle_intermediate_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the DXF-to-artifacts pipeline for one project."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Source DXF text file. If omitted, resolves from 1_input/<project_id>/clean/txt_dxf then raw/txt_dxf.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output filtered extract path. If omitted, defaults to 5_output/<project_id>/normalized/dxf/.",
    )
    parser.add_argument(
        "--schema-output",
        help="Optional JSON schema output path. If omitted, defaults to 5_output/<project_id>/normalized/dxf/.",
    )
    parser.add_argument(
        "--mapping-output",
        help="Optional extra mirror path for mapping_payload.json for compatibility workflows.",
    )
    parser.add_argument(
        "--idf-bundle-output-dir",
        help="Optional CSV input-bundle directory. If omitted, the pipeline stops after mapping unless a bundle path is provided externally.",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=2500.0,
        help="Bounding-box padding in drawing units. Default: 2500",
    )
    parser.add_argument(
        "--ceiling-height-m",
        type=float,
        default=None,
        help="Human-provided zone/model ceiling height in meters. Required when writing an IDF bundle.",
    )
    args = parser.parse_args()
    run_pipeline(
        project_id=args.project,
        input_path=args.input,
        output_path=args.output,
        schema_output_path=args.schema_output if args.schema_output else None,
        mapping_output_path=args.mapping_output if args.mapping_output else None,
        idf_bundle_output_dir=args.idf_bundle_output_dir if args.idf_bundle_output_dir else None,
        padding=args.padding,
        ceiling_height_m=args.ceiling_height_m,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
