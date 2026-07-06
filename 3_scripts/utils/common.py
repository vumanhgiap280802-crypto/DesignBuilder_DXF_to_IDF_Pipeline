#!/usr/bin/env python3
"""
Small shared helpers used by DXF -> IDF pipeline scripts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
ADIABATIC_HALF_CONSTRUCTION_SUFFIX = "_AdiabaticHalf"


def workspace_path(path: Path | str) -> str:
    resolved = Path(path)
    try:
        return str(resolved.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(resolved).replace("\\", "/")


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


def ascii_token(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("+", "_")
    ascii_text = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text)
    ascii_text = re.sub(r"_+", "_", ascii_text).strip("_")
    return ascii_text.upper() or "UNNAMED"


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


def normalize_rect(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = rect
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def line_points_from_payload(
    payload: dict[str, object],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
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


def designbuilder_adiabatic_construction_name(
    construction_name: str,
    suffix: str = ADIABATIC_HALF_CONSTRUCTION_SUFFIX,
) -> str:
    if construction_name.endswith(suffix):
        return construction_name[: -len(suffix)]
    return construction_name
