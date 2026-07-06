#!/usr/bin/env python3
"""
Shared helpers for manifest-driven library paths.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from workspace_rules.workspace_guard import WorkspaceGuard, WorkspaceRuleError


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
DEFAULT_LIBRARY_PATHS_FILE = ROOT / "2_config" / "library_paths.json"


@lru_cache(maxsize=1)
def load_library_paths_manifest() -> dict[str, Any]:
    if not DEFAULT_LIBRARY_PATHS_FILE.exists():
        return {}
    try:
        payload = json.loads(
            GUARD.assert_read_path(DEFAULT_LIBRARY_PATHS_FILE).read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise WorkspaceRuleError(f"Invalid library path manifest: {DEFAULT_LIBRARY_PATHS_FILE}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceRuleError(f"Library path manifest root must be an object: {DEFAULT_LIBRARY_PATHS_FILE}")
    return payload


def _lookup_manifest_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def resolve_library_path(*keys: str, default: str | Path | None = None) -> Path | None:
    payload = load_library_paths_manifest()
    value = _lookup_manifest_value(payload, tuple(str(key) for key in keys))
    if value in {None, ""}:
        if default in {None, ""}:
            return None
        value = default
    candidate = Path(str(value))
    return candidate if candidate.is_absolute() else (ROOT / candidate).resolve()
