#!/usr/bin/env python3
"""
Unified schema workbench for:
- analyzing schema files
- inspecting one schema in detail
- generating CSV schema files
- generating CSV data-entry templates
- generating populated CSV tables from DXF text extract schemas
- building a lean IDF file back from CSV input
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.dxf_raw_parser import Record, build_record, parse_records
from utils import path_resolver
from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
SCHEMA_ROOT = ROOT / "4_schemas"
DEFAULT_SCHEMA_JSON = ROOT / "4_schemas" / "output" / "idf_reference" / "Test1_for_DB_import_DBlean_crosscheck_schema.json"
DEFAULT_SCHEMA_OUTPUT_DIR = ROOT / "4_schemas" / "output" / "csv_bundle"
DEFAULT_TEMPLATE_OUTPUT_DIR = Path("5_output") / "<project_id>" / "csv" / "<project_id>_idf_input_bundle"
DEFAULT_CSV_INPUT_DIR = Path("5_output") / "<project_id>" / "csv" / "<project_id>_idf_input_bundle"
DEFAULT_IDF_OUTPUT = Path("5_output") / "<project_id>" / "idf" / "<project_id>_generated_from_csv.idf"
DEFAULT_DXF_SCHEMA_JSON = ROOT / "4_schemas" / "source" / "dxf" / "Apartment_A_filtered_extract_schema.json"
DEFAULT_DXE_SCHEMA_JSON = ROOT / "4_schemas" / "source" / "dxe" / "new_block_dxe_schema.json"
DEFAULT_DXF_CSV_OUTPUT_DIR = Path("5_output") / "<project_id>" / "normalized" / "dxf" / "csv"

SINGLETON_OBJECT_TYPES = {
    "Version",
    "Site:Location",
    "Building",
    "GlobalGeometryRules",
}

REQUIRED_NONEMPTY_OBJECT_TYPES = {
    "Zone",
    "Construction",
    "BuildingSurface:Detailed",
}


def _path_is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _assert_project_output_scope(project_id: str, path: Path) -> None:
    if _path_is_under(ROOT / "5_output", path):
        path_resolver.assert_output_in_project_scope(project_id, path)


def _resolve_default_template_output_dir(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "csv", f"{project_id}_idf_input_bundle")


def _resolve_single_match(paths: list[Path], label: str) -> Path | None:
    if not paths:
        return None
    if len(paths) > 1:
        raise WorkspaceRuleError(
            f"Multiple matches found for {label}: " + ", ".join(str(path.relative_to(ROOT)) for path in paths)
        )
    return paths[0]


def _resolve_default_csv_input_dir(project_id: str) -> Path:
    csv_root = path_resolver.resolve_output_dir_for_read(project_id, "csv")
    if csv_root is None or not csv_root.exists():
        raise WorkspaceRuleError(f"No CSV output directory found for project '{project_id}'.")
    preferred = csv_root / f"{project_id}_idf_input_bundle"
    if preferred.exists():
        return preferred
    matches = sorted(path for path in csv_root.glob("*_idf_input_bundle") if path.is_dir())
    resolved = _resolve_single_match(matches, f"CSV bundle directories for project '{project_id}'")
    if resolved is None:
        raise WorkspaceRuleError(f"No CSV bundle directory found for project '{project_id}'.")
    return resolved


def _resolve_default_idf_output(project_id: str) -> Path:
    return path_resolver.resolve_output_file(project_id, "idf", f"{project_id}_generated_from_csv.idf")


def _resolve_default_dxf_csv_output_dir(project_id: str, schema_json_path: Path) -> Path:
    stem = schema_json_path.stem
    if stem.endswith("_schema"):
        stem = stem[: -len("_schema")]
    return path_resolver.resolve_output_file(project_id, f"normalized/dxf/csv/{stem}")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def detect_schema_kind(data: dict[str, Any]) -> str:
    if "dxe_container_schema_inferred" in data:
        return "dxe_schema"
    if "actual_file_schema" in data:
        return "dxf_text_schema"
    if isinstance(data.get("schema"), list):
        return "idf_object_schema"
    return "unknown_schema"


def detect_source_file(data: dict[str, Any]) -> str:
    if isinstance(data.get("source_file"), str):
        return data["source_file"]
    if isinstance(data.get("source_file"), dict):
        filename = data["source_file"].get("filename")
        if filename:
            return str(filename)
    if isinstance(data.get("summary"), dict):
        source = data["summary"].get("source_file")
        if source:
            return str(source)
    return "unknown"


def normalize_filename(object_type: str) -> str:
    value = object_type.strip()
    value = value.replace(":", "_")
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def ensure_idf_schema(data: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    schema_items = data.get("schema")
    if not isinstance(schema_items, list):
        raise WorkspaceRuleError(f"Schema file is not an IDF object schema: {path}")
    return schema_items


def ensure_dxe_schema(data: dict[str, Any], path: Path) -> dict[str, Any]:
    if detect_schema_kind(data) != "dxe_schema":
        raise WorkspaceRuleError(f"Schema file is not a DXE schema: {path}")
    return data


def ensure_dxf_text_schema(data: dict[str, Any], path: Path) -> dict[str, Any]:
    if detect_schema_kind(data) != "dxf_text_schema":
        raise WorkspaceRuleError(f"Schema file is not a DXF text schema: {path}")
    return data


def classify_dxe_column(column_name: str) -> str:
    normalized = column_name.lower()

    if normalized.startswith("file ") or normalized in {
        "author",
        "comments",
        "drawing revision number",
        "hyperlink",
        "hyperlink base",
        "keywords",
        "subject",
        "title",
        "total editing time",
    }:
        return "file_metadata"

    if normalized in {
        "contents",
        "contentsrtf",
        "dt",
        "e01",
        "i=2%",
        "prompt",
        "rcua",
        "s01",
        "sheetnumber",
        "symbol",
        "symbol(1)",
        "symbol(2)",
        "tag",
        "value",
        "viewnumber",
        "visibility",
        "visibility1",
    }:
        return "text_attribute"

    if normalized in {
        "contrast",
        "fade",
        "image height",
        "image width",
        "imagetransparency",
        "path",
        "scale",
        "show clipped",
    }:
        return "image_raster"

    if "flip state" in normalized or normalized in {
        "arrow direction",
        "dynamicdimension",
        "flag display",
        "ref line length",
        "symbol rotation",
        "textdefinedsize",
    }:
        return "dynamic_block"

    if any(
        keyword in normalized
        for keyword in [
            "angle",
            "area",
            "center",
            "circumference",
            "delta",
            "diameter",
            "distance",
            "end ",
            "end x",
            "end y",
            "end z",
            "height",
            "length",
            "major ",
            "minor ",
            "origin",
            "position",
            "radius",
            "rotation",
            "scale ",
            "spacing",
            "start ",
            "start x",
            "start y",
            "start z",
            "thickness",
            "unit ",
            "width",
        ]
    ):
        return "geometry"

    return "general_property"


def build_dxe_table_profile(table_schema: dict[str, Any]) -> dict[str, Any]:
    columns = table_schema.get("columns", [])
    if not isinstance(columns, list):
        return {
            "name": table_schema.get("name"),
            "dataset_element": table_schema.get("dataset_element"),
            "column_count": 0,
            "optional_column_count": 0,
            "required_column_count": 0,
            "type_counts": {},
            "category_counts": {},
            "category_examples": {},
            "first_columns": [],
        }

    type_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    category_examples: dict[str, list[str]] = {}
    optional_count = 0
    first_columns: list[dict[str, Any]] = []

    for column in columns:
        column_type = str(column.get("type", "unknown"))
        column_name = str(column.get("name") or column.get("raw_name") or "")
        category = classify_dxe_column(column_name)

        type_counts.update([column_type])
        category_counts.update([category])
        optional_count += int(bool(column.get("optional")))
        category_examples.setdefault(category, [])
        if column_name and column_name not in category_examples[category] and len(category_examples[category]) < 8:
            category_examples[category].append(column_name)

        if len(first_columns) < 15:
            first_columns.append(
                {
                    "index": column.get("index"),
                    "name": column_name,
                    "type": column_type,
                    "category": category,
                }
            )

    return {
        "name": table_schema.get("name"),
        "dataset_element": table_schema.get("dataset_element"),
        "column_count": len(columns),
        "optional_column_count": optional_count,
        "required_column_count": len(columns) - optional_count,
        "type_counts": dict(sorted(type_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "category_examples": category_examples,
        "first_columns": first_columns,
    }


def resolve_dxf_extract_source(data: dict[str, Any], schema_path: Path) -> Path:
    source_file = data.get("source_file", {})
    if not isinstance(source_file, dict):
        raise WorkspaceRuleError(f"DXF text schema is missing source_file metadata: {schema_path}")
    source_path = source_file.get("path")
    if not isinstance(source_path, str) or not source_path.strip():
        raise WorkspaceRuleError(f"DXF text schema is missing source_file.path: {schema_path}")
    return GUARD.assert_read_path(source_path)


def json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def console_safe_text(value: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(encoding, errors="backslashreplace").decode(encoding, errors="strict")


def console_safe_path(path: Path) -> str:
    try:
        text = str(path.relative_to(ROOT))
    except ValueError:
        text = str(path)
    return console_safe_text(text)


def parse_extract_metadata(lines: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in lines:
        if line == "# BEGIN BLOCK DEFINITIONS":
            break
        if not line.startswith("#"):
            continue
        text = line[1:].strip()
        if not text or ":" not in text:
            continue
        key, value = text.split(":", 1)
        rows.append({"key": key.strip(), "value": value.strip()})
    return rows


def split_rendered_extract(lines: list[str]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    block_sections: list[dict[str, Any]] = []
    filtered_lines: list[str] = []
    state = "header"
    current_block_name = ""
    current_block_lines: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")

        if state == "header":
            if line == "# BEGIN BLOCK DEFINITIONS":
                state = "blocks"
            continue

        if state == "blocks":
            if line == "# END BLOCK DEFINITIONS":
                if current_block_name:
                    block_sections.append({"block_name": current_block_name, "lines": current_block_lines[:]})
                current_block_name = ""
                current_block_lines = []
                state = "between_sections"
                continue

            if line.startswith("# BLOCK:"):
                if current_block_name:
                    block_sections.append({"block_name": current_block_name, "lines": current_block_lines[:]})
                current_block_name = line.split(":", 1)[1].strip()
                current_block_lines = []
                continue

            if current_block_name:
                current_block_lines.append(line)
            continue

        if line == "# BEGIN FILTERED RECORDS":
            state = "filtered"
            continue

        if state == "filtered":
            if line == "# END FILTERED RECORDS":
                state = "done"
                continue
            filtered_lines.append(line)

    metadata_rows = parse_extract_metadata(lines)
    return block_sections, filtered_lines, metadata_rows


def split_rendered_record_chunks(lines: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("#"):
            continue

        if line == "" and len(current) % 2 == 0:
            if current:
                chunks.append(current)
                current = []
            continue

        current.append(line)

    if current:
        chunks.append(current)

    for chunk in chunks:
        if len(chunk) % 2 != 0:
            raise WorkspaceRuleError(
                "Rendered DXF extract produced an odd number of lines for one record chunk."
            )
        if not chunk or chunk[0].strip() != "0":
            raise WorkspaceRuleError("Each rendered DXF record chunk must start with group code 0.")

    return chunks


def build_records_from_chunks(section_name: str, chunks: list[list[str]]) -> list[Record]:
    records: list[Record] = []
    pair_offset = 0

    for chunk in chunks:
        record_type = chunk[1].rstrip("\r\n")
        pair_count = len(chunk) // 2
        record = build_record(
            section_name,
            record_type,
            chunk,
            pair_offset,
            pair_offset + pair_count - 1,
        )
        records.append(record)
        pair_offset += pair_count

    return records


def parse_dxf_extract_file(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    block_sections, filtered_lines, metadata_rows = split_rendered_extract(lines)

    parsed_block_sections: list[dict[str, Any]] = []
    for item in block_sections:
        block_lines = list(item["lines"])
        while block_lines and len(block_lines) % 2 != 0 and block_lines[-1] == "":
            block_lines.pop()
        records = parse_records(block_lines)
        for record in records:
            record.section = "BLOCK_DEFINITIONS"
        parsed_block_sections.append(
            {
                "block_name": item["block_name"],
                "records": records,
            }
        )

    filtered_chunks = split_rendered_record_chunks(filtered_lines)
    filtered_records = build_records_from_chunks("FILTERED_RECORDS", filtered_chunks)

    return {
        "metadata_rows": metadata_rows,
        "block_sections": parsed_block_sections,
        "filtered_records": filtered_records,
    }


def record_group_codes(record: Record) -> list[int]:
    codes: list[int] = []
    for index in range(0, len(record.raw_lines), 2):
        code = record.raw_lines[index].strip()
        if code.lstrip("-").isdigit():
            codes.append(int(code))
    return codes


def bbox_row_values(record: Record) -> dict[str, str]:
    bbox = record.bbox
    if bbox is None:
        return {
            "bbox_min_x": "",
            "bbox_min_y": "",
            "bbox_max_x": "",
            "bbox_max_y": "",
        }
    return {
        "bbox_min_x": str(bbox[0]),
        "bbox_min_y": str(bbox[1]),
        "bbox_max_x": str(bbox[2]),
        "bbox_max_y": str(bbox[3]),
    }


def build_dxf_csv_tables(parsed_extract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    extraction_metadata_rows = parsed_extract["metadata_rows"]
    block_sections = parsed_extract["block_sections"]
    filtered_records = parsed_extract["filtered_records"]

    block_definition_rows: list[dict[str, str]] = []
    filtered_rows: list[dict[str, str]] = []
    point_rows: list[dict[str, str]] = []
    text_rows: list[dict[str, str]] = []

    for block_section in block_sections:
        parent_block_name = block_section["block_name"]
        for record in block_section["records"]:
            block_definition_rows.append(
                {
                    "block_name": parent_block_name,
                    "record_type": record.record_type,
                    "layer": record.layer,
                    "handle": record.handle,
                    "text_values[]": json_cell(record.text_values),
                    "point_count": str(len(record.points)),
                    "raw_group_codes": json_cell(record_group_codes(record)),
                    "raw_payload": json_cell(record.raw_lines),
                }
            )

            for point_order, (x, y, z) in enumerate(record.points, start=1):
                point_rows.append(
                    {
                        "record_scope": "BLOCK_DEFINITIONS",
                        "record_type": record.record_type,
                        "handle": record.handle,
                        "point_order": str(point_order),
                        "x": str(x),
                        "y": str(y),
                        "z": str(z),
                    }
                )

            for text_order, text_value in enumerate(record.text_values, start=1):
                text_rows.append(
                    {
                        "record_scope": "BLOCK_DEFINITIONS",
                        "record_type": record.record_type,
                        "handle": record.handle,
                        "text_order": str(text_order),
                        "text_value": text_value,
                    }
                )

    for record in filtered_records:
        filtered_row = {
            "source_section": "FILTERED_RECORDS",
            "record_type": record.record_type,
            "layer": record.layer,
            "handle": record.handle,
            "block_name": record.block_name,
            "text_values[]": json_cell(record.text_values),
            "point_count": str(len(record.points)),
            "raw_group_codes": json_cell(record_group_codes(record)),
            "raw_payload": json_cell(record.raw_lines),
        }
        filtered_row.update(bbox_row_values(record))
        filtered_rows.append(filtered_row)

        for point_order, (x, y, z) in enumerate(record.points, start=1):
            point_rows.append(
                {
                    "record_scope": "FILTERED_RECORDS",
                    "record_type": record.record_type,
                    "handle": record.handle,
                    "point_order": str(point_order),
                    "x": str(x),
                    "y": str(y),
                    "z": str(z),
                }
            )

        for text_order, text_value in enumerate(record.text_values, start=1):
            text_rows.append(
                {
                    "record_scope": "FILTERED_RECORDS",
                    "record_type": record.record_type,
                    "handle": record.handle,
                    "text_order": str(text_order),
                    "text_value": text_value,
                }
            )

    return {
        "extraction_metadata": {
            "fieldnames": ["key", "value"],
            "rows": extraction_metadata_rows,
        },
        "block_definition_records": {
            "fieldnames": [
                "block_name",
                "record_type",
                "layer",
                "handle",
                "text_values[]",
                "point_count",
                "raw_group_codes",
                "raw_payload",
            ],
            "rows": block_definition_rows,
        },
        "filtered_records": {
            "fieldnames": [
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
            "rows": filtered_rows,
        },
        "record_points": {
            "fieldnames": ["record_scope", "record_type", "handle", "point_order", "x", "y", "z"],
            "rows": point_rows,
        },
        "text_values": {
            "fieldnames": ["record_scope", "record_type", "handle", "text_order", "text_value"],
            "rows": text_rows,
        },
    }


def write_named_csv_tables(
    tables: dict[str, dict[str, Any]],
    output_dir: Path,
    *,
    dry_run: bool,
) -> list[Path]:
    created: list[Path] = []
    for table_name, table in tables.items():
        target = output_dir / f"{table_name}.csv"
        write_csv(
            target,
            list(table["fieldnames"]),
            list(table["rows"]),
            allowed_roots=["5_output"],
            dry_run=dry_run,
        )
        created.append(target)
    return created


def build_file_summary(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    kind = detect_schema_kind(data)
    summary: dict[str, Any] = {
        "path": str(path.relative_to(ROOT)),
        "schema_kind": kind,
        "source_file": detect_source_file(data),
        "top_level_keys": sorted(data.keys()),
    }

    if kind == "idf_object_schema":
        summary["object_type_count"] = len(data.get("schema", []))
        summary["total_objects"] = data.get("summary", {}).get("total_objects")
        summary["object_types"] = [
            item.get("object_type") for item in data.get("schema", []) if item.get("object_type")
        ]
    elif kind == "dxe_schema":
        dxe_summary = data.get("summary", {})
        table_profile = build_dxe_table_profile(data.get("table_schema", {}))
        summary["detected_format"] = dxe_summary.get("detected_format")
        summary["column_count"] = dxe_summary.get("column_count")
        summary["row_count"] = dxe_summary.get("row_count")
        summary["table_schema_name"] = dxe_summary.get("table_schema_name")
        summary["dataset_element"] = table_profile.get("dataset_element")
    elif kind == "dxf_text_schema":
        file_profile = data.get("file_profile", {})
        top_level_counts = data.get("top_level_counts", {})
        actual_file_schema = data.get("actual_file_schema", {})
        sections = actual_file_schema.get("sections", [])
        summary["detected_format"] = data.get("source_file", {}).get("detected_format")
        summary["acad_version"] = file_profile.get("acad_version")
        summary["drawing_units_name"] = file_profile.get("drawing_units_name")
        summary["section_count"] = top_level_counts.get("section_count")
        summary["sections"] = [item.get("section_name") for item in sections if item.get("section_name")]

    return summary


def analyze_schema_directory(schema_root: Path) -> dict[str, Any]:
    files = sorted(schema_root.rglob("*.json"))
    file_summaries: list[dict[str, Any]] = []
    kind_counter: Counter[str] = Counter()
    subdir_counter: Counter[str] = Counter()

    for file_path in files:
        GUARD.assert_read_path(file_path)
        data = load_json(file_path)
        summary = build_file_summary(file_path, data)
        file_summaries.append(summary)
        kind_counter.update([summary["schema_kind"]])
        subdir_counter.update([str(file_path.parent.relative_to(ROOT))])

    aggregate: dict[str, Any] = {
        "schema_root": str(schema_root.relative_to(ROOT)),
        "total_schema_files": len(file_summaries),
        "schema_kinds": dict(sorted(kind_counter.items())),
        "directories": dict(sorted(subdir_counter.items())),
        "files": file_summaries,
    }

    idf_files = [item for item in file_summaries if item["schema_kind"] == "idf_object_schema"]
    if idf_files:
        aggregate["idf_overview"] = {
            "file_count": len(idf_files),
            "sources": [item["source_file"] for item in idf_files],
            "total_object_types_across_files": sum(int(item.get("object_type_count") or 0) for item in idf_files),
        }

    dxe_files = [item for item in file_summaries if item["schema_kind"] == "dxe_schema"]
    if dxe_files:
        aggregate["dxe_overview"] = {
            "file_count": len(dxe_files),
            "sources": [item["source_file"] for item in dxe_files],
            "total_columns_across_files": sum(int(item.get("column_count") or 0) for item in dxe_files),
            "total_rows_across_files": sum(int(item.get("row_count") or 0) for item in dxe_files),
            "files": [
                {
                    "path": item["path"],
                    "source_file": item["source_file"],
                    "table_schema_name": item.get("table_schema_name"),
                    "dataset_element": item.get("dataset_element"),
                    "column_count": item.get("column_count"),
                    "row_count": item.get("row_count"),
                }
                for item in dxe_files
            ],
        }

    dxf_files = [item for item in file_summaries if item["schema_kind"] == "dxf_text_schema"]
    if dxf_files:
        aggregate["dxf_text_overview"] = {
            "file_count": len(dxf_files),
            "sources": [item["source_file"] for item in dxf_files],
        }

    return aggregate


def analyze_single_schema(path: Path) -> dict[str, Any]:
    GUARD.assert_read_path(path)
    data = load_json(path)
    kind = detect_schema_kind(data)
    report: dict[str, Any] = {
        "path": str(path.relative_to(ROOT)),
        "schema_kind": kind,
        "source_file": detect_source_file(data),
        "top_level_keys": sorted(data.keys()),
    }

    if kind == "idf_object_schema":
        summary = data.get("summary", {})
        schema_items = data.get("schema", [])
        report["summary"] = {
            "total_objects": summary.get("total_objects"),
            "object_type_counts": summary.get("object_type_counts", {}),
            "notes": summary.get("notes", []),
        }
        report["object_types"] = [
            {
                "object_type": item.get("object_type"),
                "count": item.get("count"),
                "field_count": len(item.get("fields", [])),
                "fields": item.get("fields", []),
                "sample_record": item.get("sample_record", {}),
            }
            for item in schema_items
        ]
    elif kind == "dxe_schema":
        report["summary"] = data.get("summary", {})
        report["container_schema"] = data.get("dxe_container_schema_inferred", {})
        report["table_profile"] = build_dxe_table_profile(data.get("table_schema", {}))
    elif kind == "dxf_text_schema":
        report["source_profile"] = data.get("source_file", {})
        report["file_profile"] = data.get("file_profile", {})
        report["top_level_counts"] = data.get("top_level_counts", {})
        report["actual_file_schema"] = data.get("actual_file_schema", {})
    else:
        report["raw"] = data

    return report


def print_directory_report(report: dict[str, Any]) -> None:
    print("CURRENT SCHEMA ANALYSIS")
    print("=" * 80)
    print(f"Schema root: {report['schema_root']}")
    print(f"Total schema files: {report['total_schema_files']}")
    print()

    print("Schema kinds:")
    for kind, count in report.get("schema_kinds", {}).items():
        print(f"  - {kind}: {count}")
    print()

    print("Directories:")
    for directory, count in report.get("directories", {}).items():
        print(f"  - {directory}: {count}")
    print()

    print("Files:")
    for item in report.get("files", []):
        print(f"  - {item['path']}")
        print(f"    kind: {item['schema_kind']}")
        print(f"    source: {item['source_file']}")
        if "object_type_count" in item:
            print(f"    object types: {item['object_type_count']}")
            print(f"    total objects: {item.get('total_objects')}")
        if "column_count" in item:
            print(f"    columns: {item.get('column_count')}")
            print(f"    rows: {item.get('row_count')}")
        if "section_count" in item:
            print(f"    sections: {item.get('section_count')}")
            print(f"    acad_version: {item.get('acad_version')}")
            print(f"    units: {item.get('drawing_units_name')}")
        print()


def print_single_schema_report(report: dict[str, Any]) -> None:
    print("SCHEMA DETAIL")
    print("=" * 80)
    print(f"Path: {report['path']}")
    print(f"Kind: {report['schema_kind']}")
    print(f"Source file: {report['source_file']}")
    print()

    if report["schema_kind"] == "idf_object_schema":
        summary = report.get("summary", {})
        print("Summary:")
        print(f"  - total_objects: {summary.get('total_objects')}")
        print(f"  - object_types: {len(report.get('object_types', []))}")
        print()

        object_type_counts = summary.get("object_type_counts", {})
        if object_type_counts:
            print("Object type counts:")
            for object_type, count in object_type_counts.items():
                print(f"  - {object_type}: {count}")
            print()

        notes = summary.get("notes", [])
        if notes:
            print("Notes:")
            for note in notes:
                print(f"  - {note}")
            print()

        print("Object schemas:")
        for item in report.get("object_types", []):
            print(f"  - {item['object_type']}")
            print(f"    count: {item.get('count')}")
            print(f"    field_count: {item.get('field_count')}")
            if item.get("fields"):
                print("    fields:")
                for field_name in item["fields"]:
                    print(f"      - {field_name}")
            sample_record = item.get("sample_record", {})
            if sample_record:
                print("    sample_record:")
                for key, value in sample_record.items():
                    print(f"      - {key}: {value}")
            print()
    elif report["schema_kind"] == "dxe_schema":
        summary = report.get("summary", {})
        table_profile = report.get("table_profile", {})

        print("Summary:")
        print(f"  - detected_format: {summary.get('detected_format')}")
        print(f"  - row_count: {summary.get('row_count')}")
        print(f"  - column_count: {summary.get('column_count')}")
        print(f"  - table_schema_name: {summary.get('table_schema_name')}")
        print(f"  - dataset_element: {table_profile.get('dataset_element')}")
        print()

        top_entities = summary.get("top_entity_names", [])
        if top_entities:
            print("Top entity names:")
            for item in top_entities[:10]:
                entity_name = console_safe_text(str(item.get("name", "")))
                print(f"  - {entity_name}: {item.get('count')}")
            print()

        print("Table profile:")
        print(f"  - optional_column_count: {table_profile.get('optional_column_count')}")
        print(f"  - required_column_count: {table_profile.get('required_column_count')}")
        print()

        type_counts = table_profile.get("type_counts", {})
        if type_counts:
            print("Column types:")
            for type_name, count in type_counts.items():
                print(f"  - {type_name}: {count}")
            print()

        category_counts = table_profile.get("category_counts", {})
        if category_counts:
            print("Column categories:")
            for category_name, count in category_counts.items():
                print(f"  - {category_name}: {count}")
            print()

        category_examples = table_profile.get("category_examples", {})
        if category_examples:
            print("Category examples:")
            for category_name, names in category_examples.items():
                safe_names = ", ".join(console_safe_text(str(name)) for name in names)
                print(f"  - {category_name}: {safe_names}")
            print()

        first_columns = table_profile.get("first_columns", [])
        if first_columns:
            print("First columns:")
            for item in first_columns:
                column_name = console_safe_text(str(item.get("name", "")))
                print(
                    f"  - #{item.get('index')}: {column_name} "
                    f"[{item.get('type')}, {item.get('category')}]"
                )
            print()
    else:
        console_encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
        ensure_ascii = console_encoding != "utf-8"
        print(json.dumps(report, ensure_ascii=ensure_ascii, indent=2))


def build_schema_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    object_type = item.get("object_type", "")
    count = item.get("count")
    fields = item.get("fields", [])
    sample_record = item.get("sample_record", {}) or {}
    rows: list[dict[str, Any]] = []

    for index, field_name in enumerate(fields, start=1):
        rows.append(
            {
                "object_type": object_type,
                "object_count_in_reference": count,
                "field_order": index,
                "field_name": field_name,
                "sample_value": sample_record.get(field_name, ""),
            }
        )

    return rows


def build_template_rows(item: dict[str, Any], include_sample_row: bool) -> tuple[list[str], list[dict[str, Any]]]:
    fields = list(item.get("fields", []))
    rows: list[dict[str, Any]] = []
    if include_sample_row:
        sample_record = item.get("sample_record", {}) or {}
        rows.append({field_name: sample_record.get(field_name, "") for field_name in fields})
    return fields, rows


def write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, Any]],
    *,
    allowed_roots: list[str],
    dry_run: bool,
) -> None:
    target = GUARD.assert_write_path(
        path,
        allowed_roots=allowed_roots,
        allow_create=True,
        allow_overwrite=True,
    )
    if dry_run:
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def generate_schema_csvs(schema_items: list[dict[str, Any]], output_dir: Path, *, dry_run: bool) -> list[Path]:
    created: list[Path] = []
    for item in schema_items:
        object_type = item.get("object_type", "")
        target = output_dir / f"{normalize_filename(object_type)}.schema.csv"
        rows = build_schema_rows(item)
        write_csv(
            target,
            ["object_type", "object_count_in_reference", "field_order", "field_name", "sample_value"],
            rows,
            allowed_roots=["4_schemas"],
            dry_run=dry_run,
        )
        created.append(target)
    return created


def generate_template_csvs(
    schema_items: list[dict[str, Any]],
    output_dir: Path,
    *,
    include_sample_row: bool,
    dry_run: bool,
) -> list[Path]:
    created: list[Path] = []
    for item in schema_items:
        object_type = item.get("object_type", "")
        target = output_dir / f"{normalize_filename(object_type)}.csv"
        fieldnames, rows = build_template_rows(item, include_sample_row=include_sample_row)
        write_csv(
            target,
            fieldnames,
            rows,
            allowed_roots=["5_output"],
            dry_run=dry_run,
        )
        created.append(target)
    return created


def print_csv_generation_summary(
    *,
    schema_json_path: Path,
    schema_file_count: int,
    schema_targets: list[Path],
    template_targets: list[Path],
    dry_run: bool,
) -> None:
    print("CSV GENERATION PLAN")
    print("=" * 80)
    print(f"Source schema: {schema_json_path.relative_to(ROOT)}")
    print(f"Object types: {schema_file_count}")
    print(f"Mode: {'dry-run' if dry_run else 'write'}")
    print()

    if schema_targets:
        print("Schema CSV files:")
        for path in schema_targets:
            print(f"  - {path.relative_to(ROOT)}")
        print()

    if template_targets:
        print("Template CSV files:")
        for path in template_targets:
            print(f"  - {path.relative_to(ROOT)}")
        print()


def print_dxf_csv_materialization_summary(
    *,
    schema_json_path: Path,
    source_extract_path: Path,
    output_dir: Path,
    tables: dict[str, dict[str, Any]],
    created_targets: list[Path],
    dry_run: bool,
) -> None:
    print("DXF CSV MATERIALIZATION")
    print("=" * 80)
    print(f"Schema JSON: {schema_json_path.relative_to(ROOT)}")
    print(f"Source extract: {source_extract_path.relative_to(ROOT)}")
    print(f"Output dir: {output_dir.relative_to(ROOT)}")
    print(f"Mode: {'dry-run' if dry_run else 'write'}")
    print()

    print("Tables:")
    for table_name, table in tables.items():
        print(f"  - {table_name}: {len(table.get('rows', []))} rows")
    print()

    if created_targets:
        print("CSV files:")
        for path in created_targets:
            print(f"  - {path.relative_to(ROOT)}")
        print()


def read_csv_rows(path: Path, required_fields: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise WorkspaceRuleError(f"CSV file has no header: {path}")

        missing = [field for field in required_fields if field not in reader.fieldnames]
        if missing:
            raise WorkspaceRuleError(f"CSV file {path} is missing required columns: {missing}")

        for raw_row in reader:
            normalized = {key: (value.strip() if isinstance(value, str) else "") for key, value in raw_row.items()}
            if not any(value for value in normalized.values()):
                continue
            rows.append({field: normalized.get(field, "") for field in required_fields})

    return rows


def load_csv_bundle(schema_items: list[dict[str, Any]], csv_dir: Path) -> dict[str, dict[str, Any]]:
    bundle: dict[str, dict[str, Any]] = {}

    for item in schema_items:
        object_type = item.get("object_type", "")
        fields = list(item.get("fields", []))
        csv_path = csv_dir / f"{normalize_filename(object_type)}.csv"
        rows = read_csv_rows(csv_path, fields) if csv_path.exists() else []
        bundle[object_type] = {
            "fields": fields,
            "csv_path": csv_path,
            "rows": rows,
        }

    return bundle


def first_field_name(bundle_item: dict[str, Any]) -> str | None:
    fields = bundle_item.get("fields", [])
    return fields[0] if fields else None


def collect_names(bundle: dict[str, dict[str, Any]], object_type: str) -> set[str]:
    item = bundle.get(object_type, {})
    key = first_field_name(item)
    if not key:
        return set()
    return {row.get(key, "") for row in item.get("rows", []) if row.get(key, "")}


def validate_bundle(bundle: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []

    for object_type in SINGLETON_OBJECT_TYPES:
        count = len(bundle.get(object_type, {}).get("rows", []))
        if count != 1:
            errors.append(f"{object_type} must have exactly 1 row, found {count}")

    for object_type in REQUIRED_NONEMPTY_OBJECT_TYPES:
        count = len(bundle.get(object_type, {}).get("rows", []))
        if count == 0:
            errors.append(f"{object_type} must have at least 1 row")

    for object_type, item in bundle.items():
        key = first_field_name(item)
        if not key:
            continue
        seen: set[str] = set()
        for row_index, row in enumerate(item.get("rows", []), start=1):
            value = row.get(key, "")
            if not value:
                errors.append(f"{object_type} row {row_index} is missing primary field {key}")
                continue
            if value in seen:
                errors.append(f"{object_type} has duplicate primary value {value}")
            seen.add(value)

    zone_names = collect_names(bundle, "Zone")
    construction_names = collect_names(bundle, "Construction")
    surface_names = collect_names(bundle, "BuildingSurface:Detailed")
    fenestration_names = collect_names(bundle, "FenestrationSurface:Detailed")
    frame_names = collect_names(bundle, "WindowProperty:FrameAndDivider")

    material_like_names: set[str] = set()
    for object_type in [
        "Material",
        "Material:NoMass",
        "Material:AirGap",
        "WindowMaterial:SimpleGlazingSystem",
        "WindowMaterial:Glazing",
        "WindowMaterial:Gas",
        "WindowMaterial:GasMixture",
    ]:
        material_like_names.update(collect_names(bundle, object_type))

    for row_index, row in enumerate(bundle.get("Construction", {}).get("rows", []), start=1):
        for field_name, value in row.items():
            if field_name.startswith("layer_") and value and value not in material_like_names:
                errors.append(
                    f"Construction row {row_index} references unknown material/window material {value} in {field_name}"
                )

    for row_index, row in enumerate(bundle.get("BuildingSurface:Detailed", {}).get("rows", []), start=1):
        if row.get("zone_name", "") not in zone_names:
            errors.append(
                f"BuildingSurface:Detailed row {row_index} references unknown zone {row.get('zone_name', '')}"
            )
        if row.get("construction_name", "") not in construction_names:
            errors.append(
                f"BuildingSurface:Detailed row {row_index} references unknown construction {row.get('construction_name', '')}"
            )
        outside_condition = row.get("outside_boundary_condition", "")
        outside_ref = row.get("outside_boundary_condition_object", "")
        if outside_condition == "Surface" and outside_ref and outside_ref not in surface_names:
            errors.append(
                f"BuildingSurface:Detailed row {row_index} references unknown adjacent surface {outside_ref}"
            )
        if row.get("number_of_vertices", "") and row.get("number_of_vertices", "") not in {"4", "4.0"}:
            errors.append(
                f"BuildingSurface:Detailed row {row_index} has unsupported number_of_vertices {row.get('number_of_vertices', '')}; current CSV format supports 4 only"
            )

    for row_index, row in enumerate(bundle.get("FenestrationSurface:Detailed", {}).get("rows", []), start=1):
        if row.get("construction_name", "") not in construction_names:
            errors.append(
                f"FenestrationSurface:Detailed row {row_index} references unknown construction {row.get('construction_name', '')}"
            )
        if row.get("building_surface_name", "") not in surface_names:
            errors.append(
                f"FenestrationSurface:Detailed row {row_index} references unknown building surface {row.get('building_surface_name', '')}"
            )
        frame_name = row.get("frame_and_divider_name", "")
        if frame_name and frame_name not in frame_names:
            errors.append(
                f"FenestrationSurface:Detailed row {row_index} references unknown frame/divider {frame_name}"
            )
        outside_ref = row.get("outside_boundary_condition_object", "")
        if outside_ref and outside_ref not in fenestration_names:
            errors.append(
                f"FenestrationSurface:Detailed row {row_index} references unknown outside boundary condition object {outside_ref}"
            )
        if row.get("number_of_vertices", "") and row.get("number_of_vertices", "") not in {"4", "4.0"}:
            errors.append(
                f"FenestrationSurface:Detailed row {row_index} has unsupported number_of_vertices {row.get('number_of_vertices', '')}; current CSV format supports 4 only"
            )

    return errors


def render_idf_object(object_type: str, fields: list[str], row: dict[str, str]) -> str:
    lines = [f"{object_type},"]
    for index, field_name in enumerate(fields):
        suffix = ";" if index == len(fields) - 1 else ","
        value = row.get(field_name, "")
        lines.append(f"  {value}{suffix}")
    return "\n".join(lines)


def build_idf_text(schema_items: list[dict[str, Any]], bundle: dict[str, dict[str, Any]]) -> str:
    chunks: list[str] = []
    for item in schema_items:
        object_type = item.get("object_type", "")
        fields = list(item.get("fields", []))
        rows = bundle.get(object_type, {}).get("rows", [])
        for row in rows:
            chunks.append(render_idf_object(object_type, fields, row))
    return "\n\n".join(chunks) + "\n"


def print_build_summary(
    schema_json: Path,
    csv_dir: Path,
    output_idf: Path,
    schema_items: list[dict[str, Any]],
    bundle: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
) -> None:
    print("BUILD IDF FROM CSV")
    print("=" * 80)
    print(f"Schema JSON: {schema_json.relative_to(ROOT)}")
    print(f"CSV input dir: {csv_dir.relative_to(ROOT)}")
    print(f"Output IDF: {output_idf.relative_to(ROOT)}")
    print(f"Mode: {'dry-run' if dry_run else 'write'}")
    print()
    print("Object counts from CSV:")
    for item in schema_items:
        object_type = item.get("object_type", "")
        count = len(bundle.get(object_type, {}).get("rows", []))
        print(f"  - {object_type}: {count}")


def command_analyze(args: argparse.Namespace) -> int:
    project_id = path_resolver.resolve_project_id(args.project) if args.output else None
    if args.file:
        schema_file = GUARD.assert_read_path(args.file)
        report = analyze_single_schema(schema_file)
        print_single_schema_report(report)
    else:
        schema_root = GUARD.assert_read_path(args.schema_root)
        report = analyze_schema_directory(schema_root)
        print_directory_report(report)

    if args.output:
        resolved_output = GUARD.resolve(args.output)
        if project_id is not None:
            _assert_project_output_scope(project_id, resolved_output)
        GUARD.write_json(
            args.output,
            report,
            allowed_roots=["5_output", "7_archive"],
            allow_create=True,
            allow_overwrite=True,
        )
        print(f"REPORT_WRITTEN {console_safe_path(GUARD.resolve(args.output))}")
    return 0


def command_analyze_dxe(args: argparse.Namespace) -> int:
    project_id = path_resolver.resolve_project_id(args.project) if args.output else None
    schema_json_path = GUARD.assert_read_path(args.schema_json)
    data = load_json(schema_json_path)
    ensure_dxe_schema(data, schema_json_path)
    report = analyze_single_schema(schema_json_path)
    print_single_schema_report(report)

    if args.output:
        resolved_output = GUARD.resolve(args.output)
        if project_id is not None:
            _assert_project_output_scope(project_id, resolved_output)
        GUARD.write_json(
            args.output,
            report,
            allowed_roots=["5_output", "7_archive"],
            allow_create=True,
            allow_overwrite=True,
        )
        print(f"REPORT_WRITTEN {console_safe_path(GUARD.resolve(args.output))}")
    return 0


def command_generate_csv(args: argparse.Namespace) -> int:
    project_id = path_resolver.resolve_project_id(args.project)
    schema_json_path = GUARD.assert_read_path(args.schema_json)
    data = load_json(schema_json_path)
    schema_items = ensure_idf_schema(data, schema_json_path)

    schema_targets: list[Path] = []
    template_targets: list[Path] = []

    if args.mode in {"schema", "both"}:
        schema_output_dir = GUARD.resolve(args.schema_output_dir)
        schema_targets = generate_schema_csvs(schema_items, schema_output_dir, dry_run=args.dry_run)

    if args.mode in {"template", "both"}:
        template_output_dir = GUARD.resolve(args.template_output_dir or _resolve_default_template_output_dir(project_id))
        _assert_project_output_scope(project_id, template_output_dir)
        template_targets = generate_template_csvs(
            schema_items,
            template_output_dir,
            include_sample_row=args.include_sample_row,
            dry_run=args.dry_run,
        )

    print_csv_generation_summary(
        schema_json_path=schema_json_path,
        schema_file_count=len(schema_items),
        schema_targets=schema_targets,
        template_targets=template_targets,
        dry_run=args.dry_run,
    )
    return 0


def command_generate_dxf_csv(args: argparse.Namespace) -> int:
    project_id = path_resolver.resolve_project_id(args.project)
    schema_json_path = GUARD.assert_read_path(args.schema_json)
    data = load_json(schema_json_path)
    ensure_dxf_text_schema(data, schema_json_path)
    source_extract_path = resolve_dxf_extract_source(data, schema_json_path)
    output_dir = GUARD.resolve(args.output_dir or _resolve_default_dxf_csv_output_dir(project_id, schema_json_path))
    _assert_project_output_scope(project_id, output_dir)

    parsed_extract = parse_dxf_extract_file(source_extract_path)
    tables = build_dxf_csv_tables(parsed_extract)
    created_targets = write_named_csv_tables(tables, output_dir, dry_run=args.dry_run)

    print_dxf_csv_materialization_summary(
        schema_json_path=schema_json_path,
        source_extract_path=source_extract_path,
        output_dir=output_dir,
        tables=tables,
        created_targets=created_targets,
        dry_run=args.dry_run,
    )
    return 0


def command_build_idf(args: argparse.Namespace) -> int:
    project_id = path_resolver.resolve_project_id(args.project)
    schema_json_path = GUARD.assert_read_path(args.schema_json)
    csv_dir = GUARD.assert_read_path(args.csv_dir or _resolve_default_csv_input_dir(project_id))
    data = load_json(schema_json_path)
    schema_items = ensure_idf_schema(data, schema_json_path)
    bundle = load_csv_bundle(schema_items, csv_dir)

    errors = validate_bundle(bundle)
    if errors:
        print("BUILD_IDF_VALIDATION_FAILED")
        for error in errors:
            print(f"  - {error}")
        return 1

    output_idf = GUARD.resolve(args.output_idf or _resolve_default_idf_output(project_id))
    _assert_project_output_scope(project_id, output_idf)
    idf_text = build_idf_text(schema_items, bundle)
    print_build_summary(
        schema_json=schema_json_path,
        csv_dir=csv_dir,
        output_idf=output_idf,
        schema_items=schema_items,
        bundle=bundle,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        GUARD.write_text(
            output_idf,
            idf_text,
            allowed_roots=["5_output"],
            allow_create=True,
            allow_overwrite=True,
        )
        print()
        print(f"IDF_WRITTEN {console_safe_path(output_idf)}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified schema workbench.")
    subparsers = parser.add_subparsers(dest="command")

    analyze_parser = subparsers.add_parser("analyze", help="Analyze schema files.")
    analyze_parser.add_argument(
        "--project",
        default=None,
        help="Project ID used when writing reports under 5_output/<project_id>/reports/.",
    )
    analyze_parser.add_argument("--schema-root", default="4_schemas", help="Schema root to analyze.")
    analyze_parser.add_argument("--file", help="Inspect one schema file in detail.")
    analyze_parser.add_argument(
        "--output",
        help="Optional output JSON report path. If under 5_output, use 5_output/<project_id>/reports/.",
    )
    analyze_parser.set_defaults(handler=command_analyze)

    analyze_dxe_parser = subparsers.add_parser("analyze-dxe", help="Analyze a DXE schema JSON file.")
    analyze_dxe_parser.add_argument(
        "--project",
        default=None,
        help="Project ID used when writing reports under 5_output/<project_id>/reports/.",
    )
    analyze_dxe_parser.add_argument(
        "--schema-json",
        default=str(DEFAULT_DXE_SCHEMA_JSON.relative_to(ROOT)),
        help="Source DXE schema JSON path.",
    )
    analyze_dxe_parser.add_argument(
        "--output",
        help="Optional output JSON report path. If under 5_output, use 5_output/<project_id>/reports/.",
    )
    analyze_dxe_parser.set_defaults(handler=command_analyze_dxe)

    csv_parser = subparsers.add_parser("generate-csv", help="Generate CSV schema and template files.")
    csv_parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve 5_output/<project_id>/csv defaults.",
    )
    csv_parser.add_argument(
        "--schema-json",
        default=str(DEFAULT_SCHEMA_JSON.relative_to(ROOT)),
        help="Source schema JSON path.",
    )
    csv_parser.add_argument(
        "--mode",
        choices=["schema", "template", "both"],
        default="both",
        help="What to generate. Default: both",
    )
    csv_parser.add_argument(
        "--schema-output-dir",
        default=str(DEFAULT_SCHEMA_OUTPUT_DIR.relative_to(ROOT)),
        help="Output directory for CSV schema files.",
    )
    csv_parser.add_argument(
        "--template-output-dir",
        default=None,
        help="Output directory for CSV templates. Default: 5_output/<project_id>/csv/<project_id>_idf_input_bundle/",
    )
    csv_parser.add_argument(
        "--include-sample-row",
        action="store_true",
        help="Include one sample data row in template CSV files.",
    )
    csv_parser.add_argument("--dry-run", action="store_true", help="Plan generation without writing files.")
    csv_parser.set_defaults(handler=command_generate_csv)

    dxf_csv_parser = subparsers.add_parser(
        "generate-dxf-csv",
        help="Generate populated CSV tables from a DXF text extract schema.",
    )
    dxf_csv_parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve 5_output/<project_id>/normalized/dxf/csv defaults.",
    )
    dxf_csv_parser.add_argument(
        "--schema-json",
        default=str(DEFAULT_DXF_SCHEMA_JSON.relative_to(ROOT)),
        help="Source DXF text schema JSON path.",
    )
    dxf_csv_parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for populated normalized CSV tables. Default: 5_output/<project_id>/normalized/dxf/csv/<schema_stem>/",
    )
    dxf_csv_parser.add_argument("--dry-run", action="store_true", help="Plan generation without writing files.")
    dxf_csv_parser.set_defaults(handler=command_generate_dxf_csv)

    build_parser = subparsers.add_parser("build-idf", help="Build an IDF file from CSV input.")
    build_parser.add_argument(
        "--project",
        default=None,
        help="Project ID used to resolve CSV bundle and IDF output defaults.",
    )
    build_parser.add_argument(
        "--schema-json",
        default=str(DEFAULT_SCHEMA_JSON.relative_to(ROOT)),
        help="Source schema JSON path.",
    )
    build_parser.add_argument(
        "--csv-dir",
        default=None,
        help="CSV bundle directory containing input data. Default: first project bundle under 5_output/<project_id>/csv/.",
    )
    build_parser.add_argument(
        "--output-idf",
        default=None,
        help="Output IDF path. Default: 5_output/<project_id>/idf/<project_id>_generated_from_csv.idf.",
    )
    build_parser.add_argument("--dry-run", action="store_true", help="Validate and plan without writing the IDF.")
    build_parser.set_defaults(handler=command_build_idf)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not getattr(args, "command", None):
        args = parser.parse_args(["analyze"])

    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
