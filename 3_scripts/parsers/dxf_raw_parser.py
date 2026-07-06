#!/usr/bin/env python3
"""
Low-level DXF raw record parsing shared by parser pipeline scripts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError
from utils.common import workspace_path
from utils import path_resolver


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_DXF_LAYER_PROFILE = ROOT / "2_config" / "dxf_layer_profile_s04.json"
DEFAULT_DXF_OUTPUT_DIR = Path("5_output") / "<project_id>" / "normalized" / "dxf"


def _resolve_default_dxf_input(project_id: str | None = None) -> Path:
    project_id = path_resolver.resolve_project_id(project_id)
    resolved = path_resolver.resolve_project_dxf_text_input(project_id)
    if resolved is None:
        raise WorkspaceRuleError(f"No default DXF input found for project '{project_id}'.")
    return resolved


def _resolve_default_dxf_output_dir(project_id: str | None = None) -> Path:
    project_id = path_resolver.resolve_project_id(project_id)
    return path_resolver.resolve_output_file(project_id, "normalized/dxf")

DXF_NORMALIZED_ROW_COLUMNS = [
    {"name": "row_index", "type": "integer"},
    {"name": "section", "type": "string"},
    {"name": "entity_name", "type": "string"},
    {"name": "record_type", "type": "string"},
    {"name": "layer", "type": "string"},
    {"name": "layer_token", "type": "string"},
    {"name": "layer_primary_role", "type": "string|null"},
    {"name": "layer_canonical", "type": "string|null"},
    {"name": "layer_match_source", "type": "string|null"},
    {"name": "layer_match_confidence", "type": "string|null"},
    {"name": "handle", "type": "string"},
    {"name": "block_name", "type": "string"},
    {"name": "text_values", "type": "array[string]"},
    {"name": "text_blob", "type": "string"},
    {"name": "point_count", "type": "integer"},
    {"name": "points", "type": "array[object]"},
    {"name": "anchor_x", "type": "number|null"},
    {"name": "anchor_y", "type": "number|null"},
    {"name": "bbox_min_x", "type": "number|null"},
    {"name": "bbox_min_y", "type": "number|null"},
    {"name": "bbox_max_x", "type": "number|null"},
    {"name": "bbox_max_y", "type": "number|null"},
    {"name": "start_pair_index", "type": "integer"},
    {"name": "end_pair_index", "type": "integer"},
    {"name": "is_closed", "type": "boolean"},
    {"name": "raw_group_codes", "type": "array[string]"},
    {"name": "raw_lines", "type": "array[string]"},
]


@dataclass
class Record:
    section: str
    record_type: str
    raw_lines: list[str]
    start_pair_index: int
    end_pair_index: int
    layer: str = ""
    handle: str = ""
    block_name: str = ""
    text_values: list[str] = field(default_factory=list)
    points: list[tuple[float, float, float]] = field(default_factory=list)

    @property
    def text_blob(self) -> str:
        return " ".join(self.text_values)

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        if not self.points:
            return None
        xs = [point[0] for point in self.points]
        ys = [point[1] for point in self.points]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class BlockDefinition:
    name: str
    records: list[Record]

    @property
    def raw_lines(self) -> list[str]:
        lines: list[str] = []
        for record in self.records:
            lines.extend(record.raw_lines)
        return lines


def counter_to_sorted_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter)}


def normalize_ascii_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def layer_token(value: str) -> str:
    ascii_text = normalize_ascii_text(value).upper()
    ascii_text = re.sub(r"[^A-Z0-9]+", "_", ascii_text)
    return re.sub(r"_+", "_", ascii_text).strip("_")


def _token_set(value: str) -> set[str]:
    token = layer_token(value)
    return {part for part in token.split("_") if part}


def load_layer_profile(profile_path: Path | str | None = None) -> dict[str, Any]:
    target_path = profile_path or DEFAULT_DXF_LAYER_PROFILE
    resolved_path = GUARD.assert_read_path(target_path)
    payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"DXF layer profile must be a JSON object: {workspace_path(resolved_path)}")
    payload["_resolved_path"] = workspace_path(resolved_path)
    return payload


def _match_layer_role_entry(
    *,
    layer_name: str,
    record_type: str,
    role_name: str,
    role_config: dict[str, Any],
) -> dict[str, Any] | None:
    allowed_record_types = {str(value) for value in list(role_config.get("record_types", []))}
    if allowed_record_types and record_type not in allowed_record_types:
        return None

    normalized_layer = str(layer_name or "")
    normalized_token = layer_token(normalized_layer)
    layer_tokens = _token_set(normalized_layer)

    canonical_layers = [str(value) for value in list(role_config.get("canonical_layers", []))]
    alias_layers = [str(value) for value in list(role_config.get("alias_layers", []))]
    fuzzy_tokens = [
        {str(token).upper() for token in tokens if str(token).strip()}
        for tokens in list(role_config.get("fuzzy_tokens", []))
        if isinstance(tokens, list)
    ]

    def canonical_match_key(value: str) -> str:
        token = layer_token(value)
        return token[3:] if token.startswith("EM_") else token

    def resolve_canonical_layer(matched_candidate: str = "", match_source: str = "") -> str:
        if match_source == "canonical" and matched_candidate:
            return matched_candidate
        if normalized_token.startswith("EM_"):
            return normalized_layer
        reference_key = canonical_match_key(matched_candidate or normalized_layer)
        for candidate in canonical_layers:
            if canonical_match_key(candidate) == reference_key:
                return candidate
        return canonical_layers[0] if canonical_layers else normalized_layer

    raw_candidates = [(candidate, "canonical") for candidate in canonical_layers]
    raw_candidates.extend((candidate, "alias") for candidate in alias_layers)
    for candidate, match_source in raw_candidates:
        if normalized_layer == candidate or normalized_token == layer_token(candidate):
            confidence = "high" if match_source == "canonical" else "medium"
            return {
                "role": role_name,
                "kind": str(role_config.get("kind", "primary") or "primary"),
                "priority": int(role_config.get("priority", 0) or 0),
                "canonical_layer": resolve_canonical_layer(candidate, match_source),
                "match_source": match_source,
                "match_confidence": confidence,
            }

    for token_group in fuzzy_tokens:
        if token_group and token_group.issubset(layer_tokens):
            return {
                "role": role_name,
                "kind": str(role_config.get("kind", "primary") or "primary"),
                "priority": int(role_config.get("priority", 0) or 0),
                "canonical_layer": resolve_canonical_layer(match_source="fuzzy"),
                "match_source": "fuzzy",
                "match_confidence": "low",
            }
    return None


def classify_record_layer(record: Record, layer_profile: dict[str, Any]) -> dict[str, Any]:
    raw_layer = str(record.layer or "")
    matches: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    layer_roles = layer_profile.get("layer_roles", {})
    rejected_roles = layer_profile.get("rejected_roles", {})

    if isinstance(layer_roles, dict):
        for role_name, role_config in layer_roles.items():
            if not isinstance(role_config, dict):
                continue
            match = _match_layer_role_entry(
                layer_name=raw_layer,
                record_type=record.record_type,
                role_name=str(role_name),
                role_config=role_config,
            )
            if match is not None:
                matches.append(match)

    if isinstance(rejected_roles, dict):
        for role_name, role_config in rejected_roles.items():
            if not isinstance(role_config, dict):
                continue
            match = _match_layer_role_entry(
                layer_name=raw_layer,
                record_type=record.record_type,
                role_name=str(role_name),
                role_config=role_config,
            )
            if match is not None:
                rejected.append(match)

    source_order = {"canonical": 0, "alias": 1, "fuzzy": 2}
    matches.sort(
        key=lambda item: (
            -int(item.get("priority", 0) or 0),
            source_order.get(str(item.get("match_source", "")), 99),
            str(item.get("role", "")),
        )
    )
    rejected.sort(
        key=lambda item: (
            -int(item.get("priority", 0) or 0),
            source_order.get(str(item.get("match_source", "")), 99),
            str(item.get("role", "")),
        )
    )

    primary = matches[0] if matches else None
    return {
        "raw_layer": raw_layer,
        "layer_token": layer_token(raw_layer),
        "primary": primary,
        "matches": matches,
        "rejected": rejected,
    }


def record_matches_layer_roles(record: Record, layer_profile: dict[str, Any], role_names: set[str]) -> bool:
    classification = classify_record_layer(record, layer_profile)
    return any(str(match.get("role", "")) in role_names for match in classification.get("matches", []))


def record_is_closed(record: Record) -> bool:
    if record.record_type not in {"LWPOLYLINE", "POLYLINE"}:
        return False
    for idx in range(0, len(record.raw_lines), 2):
        code_raw = record.raw_lines[idx].strip()
        value_raw = record.raw_lines[idx + 1].strip() if idx + 1 < len(record.raw_lines) else ""
        if code_raw == "70":
            try:
                return (int(value_raw) & 1) == 1
            except ValueError:
                return False
    return False


def parse_numeric(value: str) -> float | None:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None


def parse_records(lines: list[str]) -> list[Record]:
    records: list[Record] = []
    section = "ROOT"
    i = 0
    pair_count = len(lines) // 2

    while i + 1 < len(lines):
        code = lines[i].strip()
        value = lines[i + 1].rstrip("\r\n")

        if code == "0" and value == "SECTION":
            if i + 3 < len(lines) and lines[i + 2].strip() == "2":
                section = lines[i + 3].rstrip("\r\n")
            i += 4
            continue

        if code == "0" and value == "ENDSEC":
            section = "ROOT"
            i += 2
            continue

        if code != "0":
            i += 2
            continue

        start = i
        j = i + 2
        while j + 1 < len(lines):
            next_code = lines[j].strip()
            next_value = lines[j + 1].rstrip("\r\n")
            if next_code == "0" and next_value in {"SECTION", "ENDSEC"}:
                break
            if next_code == "0":
                break
            j += 2

        raw_lines = [line.rstrip("\r\n") for line in lines[start:j]]
        record = build_record(section, value, raw_lines, start // 2, (j // 2) - 1)
        records.append(record)
        i = j

    if len(lines) % 2 != 0:
        raise WorkspaceRuleError(
            f"DXF text line count must be even (group-code/value pairs). Got {len(lines)} lines."
        )

    if pair_count == 0:
        raise WorkspaceRuleError("DXF text file is empty.")

    return records


def build_record(section: str, record_type: str, raw_lines: list[str], start_pair_index: int, end_pair_index: int) -> Record:
    layer = ""
    handle = ""
    block_name = ""
    text_values: list[str] = []
    point_slots: dict[int, dict[str, float]] = {}
    lwpolyline_vertices: list[dict[str, float]] = []

    for idx in range(0, len(raw_lines), 2):
        code_raw = raw_lines[idx].strip()
        value_raw = raw_lines[idx + 1] if idx + 1 < len(raw_lines) else ""
        value = value_raw.strip()

        if code_raw == "8":
            layer = value
        elif code_raw == "5":
            handle = value
        elif code_raw == "2" and record_type in {"INSERT", "BLOCK"}:
            block_name = value
        elif code_raw in {"1", "3"} and value:
            text_values.append(value)

        if code_raw.isdigit():
            code_num = int(code_raw)
            parsed = parse_numeric(value)
            if parsed is None:
                continue
            if record_type == "LWPOLYLINE":
                if code_num == 10:
                    lwpolyline_vertices.append({"x": parsed})
                    continue
                if code_num == 20 and lwpolyline_vertices:
                    lwpolyline_vertices[-1]["y"] = parsed
                    continue
                if code_num == 30 and lwpolyline_vertices:
                    lwpolyline_vertices[-1]["z"] = parsed
                    continue
            if 10 <= code_num <= 18:
                slot = code_num - 10
                point_slots.setdefault(slot, {})["x"] = parsed
            elif 20 <= code_num <= 28:
                slot = code_num - 20
                point_slots.setdefault(slot, {})["y"] = parsed
            elif 30 <= code_num <= 38:
                slot = code_num - 30
                point_slots.setdefault(slot, {})["z"] = parsed

    points: list[tuple[float, float, float]] = []
    if record_type == "LWPOLYLINE" and lwpolyline_vertices:
        for coords in lwpolyline_vertices:
            if "x" in coords and "y" in coords:
                points.append((coords["x"], coords["y"], coords.get("z", 0.0)))
    else:
        for slot in sorted(point_slots):
            coords = point_slots[slot]
            if "x" in coords and "y" in coords:
                points.append((coords["x"], coords["y"], coords.get("z", 0.0)))

    return Record(
        section=section,
        record_type=record_type,
        raw_lines=raw_lines,
        start_pair_index=start_pair_index,
        end_pair_index=end_pair_index,
        layer=layer,
        handle=handle,
        block_name=block_name,
        text_values=text_values,
        points=points,
    )


def group_code_values(record: Record, code: str) -> list[str]:
    values: list[str] = []
    for idx in range(0, len(record.raw_lines), 2):
        if record.raw_lines[idx].strip() == code and idx + 1 < len(record.raw_lines):
            values.append(record.raw_lines[idx + 1].strip())
    return values


def first_group_code_value(record: Record, code: str) -> str:
    values = group_code_values(record, code)
    return values[0] if values else ""


def numeric_group_code_value(record: Record, code: str) -> float | None:
    return parse_numeric(first_group_code_value(record, code))


def record_anchor_xy(record: Record) -> tuple[float, float] | None:
    x_value = numeric_group_code_value(record, "10")
    y_value = numeric_group_code_value(record, "20")
    if x_value is not None and y_value is not None:
        return x_value, y_value
    if record.points:
        return record.points[0][0], record.points[0][1]
    return None


def load_dxf_lines(dxf_path: Path | str) -> list[str]:
    path = GUARD.assert_read_path(dxf_path)
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def build_block_definitions(records: list[Record]) -> dict[str, BlockDefinition]:
    block_defs: dict[str, BlockDefinition] = {}
    current_records: list[Record] = []
    current_name = ""

    for record in records:
        if record.section != "BLOCKS":
            continue
        if record.record_type == "BLOCK":
            current_records = [record]
            current_name = record.block_name
            continue
        if current_records:
            current_records.append(record)
            if record.record_type == "ENDBLK":
                if current_name:
                    block_defs[current_name] = BlockDefinition(name=current_name, records=current_records.copy())
                current_records = []
                current_name = ""

    return block_defs


def record_group_codes(record: Record) -> list[str]:
    return [record.raw_lines[index].strip() for index in range(0, len(record.raw_lines), 2)]


def normalize_dxf_rows(records: list[Record], layer_profile: dict[str, Any]) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []

    for row_index, record in enumerate(records, start=1):
        anchor = record_anchor_xy(record)
        bbox = record.bbox
        layer_classification = classify_record_layer(record, layer_profile)
        primary_match = layer_classification.get("primary")
        normalized_rows.append(
            {
                "row_index": row_index,
                "section": record.section,
                "entity_name": record.record_type,
                "record_type": record.record_type,
                "layer": record.layer,
                "layer_token": layer_classification.get("layer_token", ""),
                "layer_primary_role": primary_match.get("role") if isinstance(primary_match, dict) else None,
                "layer_canonical": primary_match.get("canonical_layer") if isinstance(primary_match, dict) else None,
                "layer_match_source": primary_match.get("match_source") if isinstance(primary_match, dict) else None,
                "layer_match_confidence": primary_match.get("match_confidence") if isinstance(primary_match, dict) else None,
                "handle": record.handle,
                "block_name": record.block_name,
                "text_values": list(record.text_values),
                "text_blob": record.text_blob,
                "point_count": len(record.points),
                "points": [
                    {
                        "point_order": point_order,
                        "x": x_value,
                        "y": y_value,
                        "z": z_value,
                    }
                    for point_order, (x_value, y_value, z_value) in enumerate(record.points, start=1)
                ],
                "anchor_x": anchor[0] if anchor is not None else None,
                "anchor_y": anchor[1] if anchor is not None else None,
                "bbox_min_x": bbox[0] if bbox is not None else None,
                "bbox_min_y": bbox[1] if bbox is not None else None,
                "bbox_max_x": bbox[2] if bbox is not None else None,
                "bbox_max_y": bbox[3] if bbox is not None else None,
                "start_pair_index": record.start_pair_index,
                "end_pair_index": record.end_pair_index,
                "is_closed": record_is_closed(record),
                "raw_group_codes": record_group_codes(record),
                "raw_lines": list(record.raw_lines),
            }
        )

    return normalized_rows


def build_dxf_block_definition_artifact(
    records: list[Record],
    block_definitions: dict[str, BlockDefinition],
) -> dict[str, Any]:
    row_index_by_identity = {
        id(record): row_index
        for row_index, record in enumerate(records, start=1)
    }
    blocks: list[dict[str, Any]] = []

    for block_name in sorted(block_definitions):
        block_definition = block_definitions[block_name]
        row_indexes = [
            row_index_by_identity[id(record)]
            for record in block_definition.records
            if id(record) in row_index_by_identity
        ]
        record_type_counts = Counter(record.record_type for record in block_definition.records)
        layer_counts = Counter(record.layer for record in block_definition.records if record.layer)
        blocks.append(
            {
                "block_name": block_name,
                "record_count": len(block_definition.records),
                "record_row_indexes": row_indexes,
                "record_type_counts": {
                    record_type: int(count)
                    for record_type, count in sorted(record_type_counts.items())
                },
                "layer_counts": {
                    layer: int(count)
                    for layer, count in sorted(layer_counts.items())
                },
            }
        )

    return {
        "block_definition_count": len(blocks),
        "blocks": blocks,
    }


def build_dxf_schema_profile(
    *,
    dxf_path: Path,
    records: list[Record],
    block_definition_artifact: dict[str, Any],
    layer_profile_summary: dict[str, Any],
) -> dict[str, Any]:
    section_counts = Counter(record.section for record in records)
    record_type_counts = Counter(record.record_type for record in records)
    layer_counts = Counter(record.layer for record in records if record.layer)

    return {
        "source_path": workspace_path(dxf_path),
        "schema_kind": "dxf_raw_records",
        "row_type": "dxf_record",
        "columns": list(DXF_NORMALIZED_ROW_COLUMNS),
        "column_count": len(DXF_NORMALIZED_ROW_COLUMNS),
        "row_count": len(records),
        "section_names": sorted(section_counts),
        "section_counts": {
            section: int(count)
            for section, count in sorted(section_counts.items())
        },
        "record_type_counts": {
            record_type: int(count)
            for record_type, count in sorted(record_type_counts.items())
        },
        "layer_counts": {
            layer: int(count)
            for layer, count in sorted(layer_counts.items())
        },
        "block_definition_count": int(block_definition_artifact.get("block_definition_count", 0)),
        "block_definition_names": [
            str(block.get("block_name", ""))
            for block in list(block_definition_artifact.get("blocks", []))
        ],
        "layer_profile": layer_profile_summary,
    }


def build_dxf_summary(
    *,
    dxf_path: Path,
    normalized_rows: list[dict[str, Any]],
    block_definition_artifact: dict[str, Any],
    layer_profile_summary: dict[str, Any],
) -> dict[str, Any]:
    text_record_count = sum(1 for row in normalized_rows if row.get("text_values"))
    point_record_count = sum(1 for row in normalized_rows if int(row.get("point_count", 0) or 0) > 0)
    section_counts = Counter(str(row.get("section", "")) for row in normalized_rows if row.get("section"))

    return {
        "available": True,
        "dxf_path": workspace_path(dxf_path),
        "row_count": len(normalized_rows),
        "text_record_count": text_record_count,
        "point_record_count": point_record_count,
        "section_counts": {
            section: int(count)
            for section, count in sorted(section_counts.items())
        },
        "block_definition_count": int(block_definition_artifact.get("block_definition_count", 0)),
        "operational_input_standard": str(layer_profile_summary.get("operational_input_standard", "parser_readable_dxf_text")),
        "layer_profile_source": layer_profile_summary.get("profile_source"),
    }


def build_layer_profile_summary(
    *,
    records: list[Record],
    layer_profile: dict[str, Any],
) -> dict[str, Any]:
    matched_role_counts: Counter[str] = Counter()
    match_source_counts: Counter[str] = Counter()
    unmatched_layer_counts: Counter[str] = Counter()
    rejected_layer_counts: Counter[str] = Counter()
    matched_layer_aliases: dict[str, dict[str, Any]] = {}
    primary_layers_found: set[str] = set()

    for record in records:
        if record.section not in {"ENTITIES", "BLOCKS"}:
            continue
        classification = classify_record_layer(record, layer_profile)
        layer_name = str(record.layer or "<NO_LAYER>")
        primary = classification.get("primary")
        if isinstance(primary, dict):
            role_name = str(primary.get("role", ""))
            matched_role_counts[role_name] += 1
            match_source_counts[str(primary.get("match_source", ""))] += 1
            primary_layers_found.add(role_name)
            if str(primary.get("match_source", "")) in {"alias", "fuzzy"}:
                matched_layer_aliases[layer_name] = {
                    "role": role_name,
                    "canonical_layer": primary.get("canonical_layer"),
                    "match_source": primary.get("match_source"),
                    "match_confidence": primary.get("match_confidence"),
                }
            continue
        if classification.get("rejected"):
            rejected_layer_counts[layer_name] += 1
            continue
        unmatched_layer_counts[layer_name] += 1

    required_roles = [str(value) for value in list(layer_profile.get("required_roles", []))]
    recommended_roles = [str(value) for value in list(layer_profile.get("recommended_roles", []))]
    missing_required_roles = [role for role in required_roles if role not in primary_layers_found]
    missing_recommended_roles = [role for role in recommended_roles if role not in primary_layers_found]

    return {
        "profile_name": layer_profile.get("profile_name"),
        "profile_version": layer_profile.get("profile_version"),
        "profile_source": layer_profile.get("_resolved_path"),
        "operational_input_standard": layer_profile.get("operational_input_standard", "parser_readable_dxf_text"),
        "matched_role_counts": counter_to_sorted_dict(matched_role_counts),
        "match_source_counts": counter_to_sorted_dict(match_source_counts),
        "matched_layer_aliases": matched_layer_aliases,
        "unmatched_layer_counts": counter_to_sorted_dict(unmatched_layer_counts),
        "rejected_layer_counts": counter_to_sorted_dict(rejected_layer_counts),
        "missing_required_roles": missing_required_roles,
        "missing_recommended_roles": missing_recommended_roles,
    }


def parse_dxf_file(dxf_path: Path | str, layer_profile_path: Path | str | None = None) -> dict[str, Any]:
    path = GUARD.assert_read_path(dxf_path)
    lines = load_dxf_lines(path)
    records = parse_records(lines)
    block_definitions = build_block_definitions(records)
    layer_profile = load_layer_profile(layer_profile_path)
    layer_profile_summary = build_layer_profile_summary(records=records, layer_profile=layer_profile)
    normalized_rows = normalize_dxf_rows(records, layer_profile)
    block_definition_artifact = build_dxf_block_definition_artifact(records, block_definitions)
    schema_profile = build_dxf_schema_profile(
        dxf_path=path,
        records=records,
        block_definition_artifact=block_definition_artifact,
        layer_profile_summary=layer_profile_summary,
    )
    summary = build_dxf_summary(
        dxf_path=path,
        normalized_rows=normalized_rows,
        block_definition_artifact=block_definition_artifact,
        layer_profile_summary=layer_profile_summary,
    )
    return {
        "available": True,
        "dxf_path": workspace_path(path),
        "schema_profile": schema_profile,
        "row_count": len(normalized_rows),
        "records": records,
        "normalized_rows": normalized_rows,
        "block_definitions": block_definition_artifact,
        "layer_profile": layer_profile_summary,
        "summary": summary,
    }


def write_dxf_outputs(
    parsed_dxf: dict[str, Any],
    output_dir: Path | str | None = None,
) -> dict[str, str]:
    output_dir = output_dir or _resolve_default_dxf_output_dir()
    target_dir = GUARD.resolve(output_dir)
    schema_profile_path = target_dir / "dxf_schema_profile.json"
    rows_path = target_dir / "dxf_rows.json"
    block_definitions_path = target_dir / "dxf_block_definitions.json"
    layer_profile_path = target_dir / "dxf_layer_profile.json"
    summary_path = target_dir / "dxf_summary.json"

    for path, payload in [
        (schema_profile_path, parsed_dxf.get("schema_profile", {})),
        (rows_path, parsed_dxf.get("normalized_rows", [])),
        (block_definitions_path, parsed_dxf.get("block_definitions", {})),
        (layer_profile_path, parsed_dxf.get("layer_profile", {})),
        (summary_path, parsed_dxf.get("summary", {})),
    ]:
        GUARD.write_json(
            path,
            payload,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )

    return {
        "schema_profile_path": workspace_path(schema_profile_path),
        "rows_path": workspace_path(rows_path),
        "block_definitions_path": workspace_path(block_definitions_path),
        "layer_profile_path": workspace_path(layer_profile_path),
        "summary_path": workspace_path(summary_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse raw DXF text into normalized DXF artifacts."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project ID to resolve project-scoped input and output paths.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Source DXF text file. If omitted, auto-resolves from project input.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Normalized DXF output directory. If omitted, defaults to 5_output/<project_id>/normalized/dxf.",
    )
    parser.add_argument(
        "--layer-profile",
        default=str(DEFAULT_DXF_LAYER_PROFILE.relative_to(ROOT)),
        help="Layer recognition profile JSON inside 2_config/.",
    )
    args = parser.parse_args()
    project_id = path_resolver.resolve_project_id(args.project)

    resolved_input = args.input or _resolve_default_dxf_input(project_id)
    input_path = GUARD.assert_read_path(resolved_input)
    output_dir = args.output_dir or _resolve_default_dxf_output_dir(project_id)
    output_dir = GUARD.resolve(output_dir)
    path_resolver.assert_output_in_project_scope(project_id, output_dir)
    GUARD.assert_write_path(
        output_dir / "dxf_schema_profile.json",
        allowed_roots=["5_output"],
        allow_create=True,
        allow_overwrite=True,
    )

    parsed_dxf = parse_dxf_file(input_path, layer_profile_path=args.layer_profile)
    written_paths = write_dxf_outputs(parsed_dxf, output_dir=output_dir)
    summary = dict(parsed_dxf.get("summary", {}))
    schema_profile = dict(parsed_dxf.get("schema_profile", {}))
    layer_profile = dict(parsed_dxf.get("layer_profile", {}))

    print("DXF_PARSE_COMPLETE")
    print(f"DXF input: {parsed_dxf['dxf_path']}")
    print(f"Row count: {summary.get('row_count', 0)}")
    print(f"Column count: {schema_profile.get('column_count', 0)}")
    print(f"Block definitions: {summary.get('block_definition_count', 0)}")
    print(f"Text records: {summary.get('text_record_count', 0)}")
    print(f"Layer profile: {layer_profile.get('profile_source', '')}")
    for label, rel_path in written_paths.items():
        print(f"{label}: {rel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
