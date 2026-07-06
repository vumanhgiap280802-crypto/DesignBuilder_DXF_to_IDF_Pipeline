#!/usr/bin/env python3
"""
Synchronize the human-readable workspace rules markdown file to a
machine-readable JSON config.

Source of truth:
    6_docs/WORKSPACE_RULES.md

Generated target:
    2_config/workspace_rules.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_rules.workspace_guard import WorkspaceGuard


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root
SOURCE = ROOT / "6_docs" / "WORKSPACE_RULES.md"
TARGET = ROOT / "2_config" / "workspace_rules.json"
SCRIPT_RELATIVE = "3_scripts/workspace_rules/sync_workspace_rules.py"


@dataclass
class Section:
    level: int
    title: str
    parent: "Section | None" = None
    lines: list[str] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)

    def path(self) -> list[str]:
        current: Section | None = self
        items: list[str] = []
        while current is not None and current.level > 0:
            items.append(current.title)
            current = current.parent
        return list(reversed(items))

    def raw_text(self) -> str:
        return "\n".join(self.lines).strip()


def slugify(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"`+", "", lowered)
    lowered = re.sub(r"[^a-z0-9]+", "_", lowered)
    return lowered.strip("_")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_markdown_tree(markdown: str) -> Section:
    root = Section(level=0, title="ROOT")
    stack: list[Section] = [root]

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip("\n")
        heading = re.match(r"^(#{2,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            section = Section(level=level, title=title, parent=parent)
            parent.children.append(section)
            stack.append(section)
            continue

        stack[-1].lines.append(line)

    return root


def flatten_sections(root: Section) -> list[Section]:
    result: list[Section] = []

    def visit(node: Section) -> None:
        for child in node.children:
            result.append(child)
            visit(child)

    visit(root)
    return result


def find_section(root: Section, title: str) -> Section:
    for section in flatten_sections(root):
        if section.title == title:
            return section
    raise KeyError(f"Section not found: {title}")


def split_labeled_blocks(lines: list[str]) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {"__intro__": []}
    current = "__intro__"

    for raw in lines:
        stripped = raw.strip()
        if stripped == "---":
            continue
        if stripped.endswith(":") and not stripped.startswith("- ") and not re.match(r"^\d+\.\s", stripped):
            current = stripped
            blocks.setdefault(current, [])
            continue
        blocks.setdefault(current, []).append(raw)

    return blocks


def first_paragraph(lines: list[str]) -> str:
    current: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            if current:
                break
            continue
        if stripped == "---":
            continue
        if stripped.startswith("- ") or re.match(r"^\d+\.\s", stripped):
            break
        current.append(stripped)
    return " ".join(current).strip()


def parse_bullets(lines: list[str]) -> list[str]:
    items: list[str] = []
    for raw in lines:
        match = re.match(r"^\s*-\s+(.*)$", raw)
        if match:
            items.append(match.group(1).strip())
    return items


def parse_numbered(lines: list[str]) -> list[str]:
    items: list[str] = []
    for raw in lines:
        match = re.match(r"^\s*\d+\.\s+(.*)$", raw)
        if match:
            items.append(match.group(1).strip())
    return items


def parse_numbered_with_subitems(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in lines:
        numbered = re.match(r"^\s*(\d+)\.\s+(.*)$", raw)
        if numbered:
            if current is not None:
                items.append(current)
            current = {
                "index": int(numbered.group(1)),
                "text": numbered.group(2).strip(),
                "subitems": [],
            }
            continue

        bullet = re.match(r"^\s*-\s+(.*)$", raw)
        if bullet and current is not None:
            current["subitems"].append(bullet.group(1).strip())

    if current is not None:
        items.append(current)

    return items


def parse_subpath_map(lines: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in parse_bullets(lines):
        match = re.match(r"`([^`]+)`:\s*(.*)$", item)
        if match:
            mapping[match.group(1)] = match.group(2).strip()
    return mapping


def parse_grouped_bullets(lines: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    current_group: str | None = None

    for raw in lines:
        top_group = re.match(r"^\s*-\s+(.+?):\s*$", raw)
        child_item = re.match(r"^\s{2,}-\s+(.*)$", raw)

        if top_group and not raw.startswith("  -"):
            current_group = top_group.group(1).strip()
            groups.setdefault(current_group, [])
            continue

        if child_item and current_group is not None:
            groups[current_group].append(child_item.group(1).strip())

    return groups


def parse_source_links(lines: list[str]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    current_title: str | None = None

    for raw in lines:
        title_match = re.match(r"^\s*-\s+(.*?):\s*$", raw)
        url_match = re.match(r"^\s*-\s+(https?://\S+)\s*$", raw)

        if title_match and "http" not in title_match.group(1):
            current_title = title_match.group(1).strip()
            continue

        if url_match and current_title:
            links.append({"title": current_title, "url": url_match.group(1)})
            current_title = None

    return links


def code_refs_from_lines(lines: list[str]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        for ref in re.findall(r"`([^`]+)`", raw):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def build_document_sections(root: Section) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for section in flatten_sections(root):
        text = section.raw_text()
        sections.append(
            {
                "id": slugify("__".join(section.path())),
                "title": section.title,
                "level": section.level,
                "path": section.path(),
                "raw_markdown": text,
                "bullets": parse_bullets(section.lines),
                "numbered_items": parse_numbered(section.lines),
                "code_refs": code_refs_from_lines(section.lines),
            }
        )
    return sections


def normalize_directory_rules(section: Section) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    for subsection in section.children:
        name_match = re.search(r"`([^`/]+)/?`", subsection.title)
        if not name_match:
            continue

        dir_name = name_match.group(1)
        blocks = split_labeled_blocks(subsection.lines)
        rules[dir_name] = {
            "purpose": first_paragraph(blocks.get("__intro__", [])),
            "allowed_content": parse_bullets(blocks.get("Duoc phep luu:", [])),
            "forbidden_content": parse_bullets(blocks.get("Khong duoc phep luu:", [])),
        }

        subpaths = parse_subpath_map(blocks.get("Quy uoc con:", []))
        if subpaths:
            rules[dir_name]["subpaths"] = subpaths

        notes = parse_bullets(blocks.get("Luu y:", []))
        if notes:
            rules[dir_name]["notes"] = notes

        principles = parse_bullets(blocks.get("Nguyen tac:", []))
        if principles:
            rules[dir_name]["principles"] = principles

    return rules


def normalize_idf_import_rules(section: Section) -> dict[str, Any]:
    intro_blocks = split_labeled_blocks(section.lines)
    applies_to = parse_bullets(intro_blocks.get("Phan nay ap dung cho:", []))

    section1 = find_child(section, "1. Pham vi du lieu ma DesignBuilder import")
    blocks1 = split_labeled_blocks(section1.lines)

    section2 = find_child(section, "2. Nhom object duoc DesignBuilder ho tro cho import")
    groups2 = parse_grouped_bullets(section2.lines)

    section3 = find_child(section, "3. Gioi han import can dua vao rule workspace")
    blocks3 = split_labeled_blocks(section3.lines)

    section4 = find_child(section, "4. Cau truc IDF chuan cua workspace cho muc dich import")
    blocks4 = split_labeled_blocks(section4.lines)

    section5 = find_child(section, "5. Rule luu file IDF lien quan den import")
    blocks5 = split_labeled_blocks(section5.lines)

    section6 = find_child(section, "6. Nguon tham khao chinh thuc")

    return {
        "applies_to": applies_to,
        "import_scope": parse_bullets(
            blocks1.get("Theo tai lieu chinh thuc cua DesignBuilder, IDF Import chi nham vao:", [])
        ),
        "support_notes": parse_bullets(blocks1.get("Them vao do:", [])),
        "version_policy": parse_numbered(blocks1.get("Rule workspace:", [])),
        "supported_objects": groups2,
        "official_limitations": parse_numbered(
            blocks3.get("Theo tai lieu chinh thuc cua DesignBuilder:", [])
        ),
        "workspace_enforcement": parse_numbered_with_subitems(
            blocks3.get("Tu do, workspace nay ap dung cac quy dinh bo sung sau:", [])
        ),
        "preferred_workspace_object_order": parse_numbered(
            blocks4.get(
                "DesignBuilder khong cong bo mot thu tu object bat buoc duy nhat. Tuy nhien, workspace nay chuan hoa file IDF import-ready theo thu tu sau de doc, kiem tra, va so sanh on dinh hon:",
                [],
            )
        ),
        "preferred_workspace_object_order_notes": parse_bullets(blocks4.get("Luu y:", [])),
        "storage_rules": parse_numbered(blocks5.get("__intro__", [])),
        "sources": parse_source_links(section6.lines),
    }


def find_child(section: Section, title: str) -> Section:
    for child in section.children:
        if child.title == title:
            return child
    raise KeyError(f"Child section not found: {title}")


def normalize_rules(root: Section, markdown_text: str) -> dict[str, Any]:
    muc_dich = find_section(root, "Muc dich")
    sync_section = find_section(root, "Dong Bo Rule Va Config")
    rule_tong = find_section(root, "Rule Tong")
    dir_section = find_section(root, "Quy Tac Theo Thu Muc")
    root_section = find_section(root, "Rule Cho Root Workspace")
    idf_section = find_section(root, "Rule Cau Truc IDF De Import Vao DesignBuilder")
    overwrite_section = find_section(root, "Rule Dat Ten Va Ghi De")
    create_section = find_section(root, "Rule Khong Tao File Moi Neu Chua Duoc Yeu Cau")
    examples_section = find_section(root, "Rule Ap Dung Ngay Cho Workspace Hien Tai")
    quick_section = find_section(root, "Kiem Tra Nhanh")
    final_section = find_section(root, "Ket Luat")

    numbered_directories = []
    seen_dirs: set[str] = set()
    for ref in code_refs_from_lines(muc_dich.lines):
        if re.match(r"^\d+_[a-z]+", ref) and ref not in seen_dirs:
            seen_dirs.add(ref)
            numbered_directories.append(ref)

    rule_tong_blocks = split_labeled_blocks(rule_tong.lines)
    general_principle = ""
    for raw in rule_tong_blocks.get("Nguyen tac chung:", []):
        stripped = raw.strip().strip("`")
        if stripped:
            general_principle = stripped
            break

    sync_blocks = split_labeled_blocks(sync_section.lines)
    sync_source = code_refs_from_lines(sync_blocks.get("File nguon chuan cua rule la:", []))
    sync_target = code_refs_from_lines(
        sync_blocks.get("File config may doc duoc duoc sinh ra tu file rule la:", [])
    )
    sync_commands = parse_numbered_with_subitems(sync_blocks.get("Quy uoc dong bo:", []))

    root_blocks = split_labeled_blocks(root_section.lines)

    final_lines = final_section.lines
    role_map: dict[str, str] = {}
    for item in parse_bullets(final_lines):
        match = re.match(r"`([^`]+)`\s+cho\s+(.*)$", item)
        if match:
            role_map[match.group(1)] = match.group(2).strip()

    decision_rule = ""
    quick_blocks = split_labeled_blocks(quick_section.lines)
    intro_quick = [
        raw.strip()
        for raw in quick_blocks.get("__intro__", [])
        if raw.strip() and not raw.strip().startswith("Truoc khi")
    ]
    if intro_quick:
        decision_rule = " ".join(intro_quick)

    final_rule = ""
    for raw in final_lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("- "):
            final_rule = stripped
    source_stat = SOURCE.stat()
    normalized = {
        "metadata": {
            "name": "DesignBuilder_DXF_to_IDF_Pipeline Rules",
            "format": "json",
            "version": "2.0.0",
            "workspace_root": "DesignBuilder_DXF_to_IDF_Pipeline",
            "source_modified_utc": datetime.fromtimestamp(
                source_stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        },
        "sync": {
            "source_of_truth": sync_source[0] if sync_source else "6_docs/WORKSPACE_RULES.md",
            "generated_target": sync_target[0] if sync_target else "2_config/workspace_rules.json",
            "generated_by": SCRIPT_RELATIVE,
            "mode": "markdown_to_json",
            "manual_edit_policy": "do_not_edit_target_directly",
            "source_sha256": sha256_text(markdown_text),
            "sync_commands": sync_commands,
        },
        "workspace": {
            "numbered_directories": numbered_directories,
            "general_principle": general_principle,
            "hard_commitments": parse_numbered(
                rule_tong_blocks.get("Ba cam ket bat buoc:", [])
                or rule_tong_blocks.get("Hai cam ket bat buoc:", [])
            ),
        },
        "directory_rules": normalize_directory_rules(dir_section),
        "root_workspace_rules": {
            "allowed_content": parse_bullets(root_blocks.get("Tai root `DesignBuilder_DXF_to_IDF_Pipeline/`, chi duoc dat:", [])),
            "forbidden_content": parse_bullets(root_blocks.get("Khong dat tai root:", [])),
        },
        "idf_import_rules": normalize_idf_import_rules(idf_section),
        "overwrite_and_naming_rules": parse_numbered(overwrite_section.lines),
        "new_file_creation_policy": parse_numbered_with_subitems(create_section.lines),
        "current_workspace_examples": parse_bullets(examples_section.lines),
        "quick_check": {
            "questions": parse_numbered(quick_section.lines),
            "decision_rule": decision_rule,
        },
        "operating_model": {
            "role_map": role_map,
            "final_rule": final_rule,
        },
    }

    return normalized


def render_json(markdown_text: str) -> dict[str, Any]:
    root = parse_markdown_tree(markdown_text)
    normalized = normalize_rules(root, markdown_text)
    normalized["document_sections"] = build_document_sections(root)
    return normalized


def write_output() -> dict[str, Any]:
    markdown_text = GUARD.assert_read_path(SOURCE).read_text(encoding="utf-8")
    data = render_json(markdown_text)
    GUARD.write_json(
        TARGET,
        data,
        allowed_roots=["2_config"],
        allow_create=True,
        allow_overwrite=True,
    )
    return data


def check_output() -> bool:
    markdown_text = GUARD.assert_read_path(SOURCE).read_text(encoding="utf-8")
    expected = json.dumps(render_json(markdown_text), ensure_ascii=False, indent=2) + "\n"
    if not TARGET.exists():
        return False
    current = GUARD.assert_read_path(TARGET).read_text(encoding="utf-8")
    return current == expected


def watch() -> None:
    last_hash = None
    print(f"Watching {SOURCE} -> {TARGET}")
    while True:
        markdown_text = GUARD.assert_read_path(SOURCE).read_text(encoding="utf-8")
        current_hash = sha256_text(markdown_text)
        if current_hash != last_hash:
            write_output()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Synced workspace rules.")
            last_hash = current_hash
        time.sleep(2.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync WORKSPACE_RULES.md to workspace_rules.json.")
    parser.add_argument("--check", action="store_true", help="Exit non-zero if target is out of sync.")
    parser.add_argument("--watch", action="store_true", help="Watch the markdown source and resync on change.")
    args = parser.parse_args()

    if args.watch:
        watch()
        return 0

    if args.check:
        if check_output():
            print("WORKSPACE_RULES_IN_SYNC")
            return 0
        print("WORKSPACE_RULES_OUT_OF_SYNC")
        return 1

    write_output()
    print("WORKSPACE_RULES_SYNCED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
