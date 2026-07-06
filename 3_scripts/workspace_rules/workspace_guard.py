#!/usr/bin/env python3
"""
Shared enforcement helpers for workspace rules.

This module makes the JSON rule file actionable for Python scripts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class WorkspaceRuleError(RuntimeError):
    """Raised when a script violates workspace rules."""


@dataclass
class WorkspaceGuard:
    script_path: str | Path
    root: Path = field(init=False)
    rules_path: Path = field(init=False)
    rules: dict[str, Any] = field(init=False)
    documented_write_roots: set[str] = field(init=False)
    script_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.script_file = Path(self.script_path).resolve()
        self.root = self._find_workspace_root()
        self.rules_path = self.root / "2_config" / "workspace_rules.json"
        if self.rules_path.exists():
            self.rules = json.loads(self.rules_path.read_text(encoding="utf-8"))
        else:
            # Allow bootstrap scripts to regenerate the JSON target from markdown.
            self.rules = {}
        self.documented_write_roots = self._load_documented_write_roots()
        self.assert_script_in_scripts_dir()

    def _find_workspace_root(self) -> Path:
        current = self.script_file.parent
        for candidate in [current, *current.parents]:
            if (
                (candidate / "3_scripts").exists()
                and (candidate / "6_docs" / "WORKSPACE_RULES.md").exists()
            ):
                return candidate
        raise WorkspaceRuleError(f"Cannot locate workspace root from {self.script_file}")

    def assert_script_in_scripts_dir(self) -> None:
        scripts_dir = self.root / "3_scripts"
        if scripts_dir not in self.script_file.parents:
            raise WorkspaceRuleError(
                f"Script must live under {scripts_dir}, got {self.script_file}"
            )

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else (self.root / candidate).resolve()

    def _is_under(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _workspace_relative_string(self, path: Path) -> str:
        return str(path.relative_to(self.root)).replace("\\", "/")

    def _normalize_rule_path(self, value: str | Path) -> str:
        return str(value).replace("\\", "/").strip().rstrip("/")

    def _load_documented_write_roots(self) -> set[str]:
        if not self.rules:
            return set()

        documented: set[str] = set()

        workspace = self.rules.get("workspace", {})
        if isinstance(workspace, dict):
            for item in workspace.get("numbered_directories", []):
                normalized = self._normalize_rule_path(str(item))
                if normalized:
                    documented.add(normalized)

        directory_rules = self.rules.get("directory_rules", {})
        if isinstance(directory_rules, dict):
            for dir_name, payload in directory_rules.items():
                normalized_dir = self._normalize_rule_path(str(dir_name))
                if normalized_dir:
                    documented.add(normalized_dir)
                if not isinstance(payload, dict):
                    continue
                for subpath in dict(payload.get("subpaths", {})).keys():
                    normalized_subpath = self._normalize_rule_path(str(subpath))
                    if normalized_subpath:
                        documented.add(normalized_subpath)

        return documented

    def assert_read_path(self, path: str | Path) -> Path:
        resolved = self.resolve(path)
        if not resolved.exists():
            raise WorkspaceRuleError(f"Read path does not exist: {resolved}")
        if not self._is_under(resolved, self.root):
            raise WorkspaceRuleError(f"Read path must stay inside workspace: {resolved}")
        return resolved

    def assert_write_path(
        self,
        path: str | Path,
        *,
        allowed_roots: Iterable[str],
        allow_create: bool = False,
        allow_overwrite: bool = True,
    ) -> Path:
        resolved = self.resolve(path)
        if not self._is_under(resolved, self.root):
            raise WorkspaceRuleError(f"Write path must stay inside workspace: {resolved}")

        allowed_dirs: list[Path] = []
        for item in allowed_roots:
            allowed_dir = self.resolve(item)
            if not self._is_under(allowed_dir, self.root):
                raise WorkspaceRuleError(f"Allowed write root must stay inside workspace: {allowed_dir}")
            if self.documented_write_roots:
                allowed_root_key = self._workspace_relative_string(allowed_dir)
                if allowed_root_key not in self.documented_write_roots:
                    raise WorkspaceRuleError(
                        f"Allowed write root is not declared in workspace rules: {allowed_root_key}"
                    )
            allowed_dirs.append(allowed_dir)

        if not any(self._is_under(resolved, allowed) for allowed in allowed_dirs):
            raise WorkspaceRuleError(
                f"Write path {resolved} must be inside one of: "
                + ", ".join(str(item) for item in allowed_dirs)
            )

        input_dir = self.root / "1_input"
        raw_input_dir = input_dir / "raw"
        clean_input_dir = input_dir / "clean"
        library_input_dir = input_dir / "library"
        if self._is_under(resolved, raw_input_dir):
            raise WorkspaceRuleError(f"Scripts must not write into raw input: {resolved}")
        if (
            self._is_under(resolved, input_dir)
            and not self._is_under(resolved, clean_input_dir)
            and not self._is_under(resolved, library_input_dir)
        ):
            raise WorkspaceRuleError(
                f"Scripts may only write inside 1_input/clean or 1_input/library when touching input paths: {resolved}"
            )

        if resolved.exists() and not allow_overwrite:
            raise WorkspaceRuleError(f"Overwrite is not allowed for: {resolved}")

        if not resolved.exists() and not allow_create:
            raise WorkspaceRuleError(
                f"New file creation is blocked by default. Explicitly allow create for: {resolved}"
            )

        return resolved

    def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        allowed_roots: Iterable[str],
        allow_create: bool = False,
        allow_overwrite: bool = True,
    ) -> Path:
        resolved = self.assert_write_path(
            path,
            allowed_roots=allowed_roots,
            allow_create=allow_create,
            allow_overwrite=allow_overwrite,
        )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return resolved

    def write_json(
        self,
        path: str | Path,
        data: Any,
        *,
        allowed_roots: Iterable[str],
        allow_create: bool = False,
        allow_overwrite: bool = True,
    ) -> Path:
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return self.write_text(
            path,
            payload,
            allowed_roots=allowed_roots,
            allow_create=allow_create,
            allow_overwrite=allow_overwrite,
        )
