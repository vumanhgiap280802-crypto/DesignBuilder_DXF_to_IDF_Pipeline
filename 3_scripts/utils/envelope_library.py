from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

from workspace_rules.workspace_guard import WorkspaceGuard


GUARD = WorkspaceGuard(__file__)
ROOT = GUARD.root

SIMPLE_GLAZING_FIELDS = [
    "simple_glazing_name",
    "u_factor_w_per_m2k",
    "solar_heat_gain_coefficient",
    "visible_transmittance",
]


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with GUARD.assert_read_path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _clean(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _project_envelope_dir(project_id: str | None) -> Path | None:
    project = str(project_id or "").strip().lower()
    candidates: list[Path] = []
    if project in {"apartment_a_new", "apartment_a", "ch_a_2br_s0"}:
        candidates.append(ROOT / "1_input" / "Envelope_apartment_A")
    if project:
        candidates.append(ROOT / "1_input" / f"Envelope_{project}")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _material_rows(source_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in _read_csv_rows(source_dir / "materials_input.csv"):
        name = _clean(row.get("Name"))
        if not name:
            continue
        thickness = _clean(row.get("ForceThickness")) or _clean(row.get("DefaultThickness_m"))
        rows.append(
            {
                "material_name": name,
                "roughness": _clean(row.get("Roughness"), "MediumSmooth"),
                "thickness_m": thickness,
                "conductivity_w_per_mk": _clean(row.get("Conductivity_W_mK")),
                "density_kg_per_m3": _clean(row.get("Density_kg_m3")),
                "specific_heat_j_per_kgk": _clean(row.get("SpecificHeat_J_kgK")),
                "thermal_emittance": _clean(row.get("ThermalAbsorptance"), "0.9"),
                "solar_absorptance": _clean(row.get("SolarAbsorptance"), "0.7"),
                "visible_absorptance": _clean(row.get("VisibleAbsorptance"), "0.7"),
            }
        )
    return rows


def _blank_construction_row(construction_name: str) -> dict[str, str]:
    row = {"construction_name": construction_name}
    for index in range(1, 7):
        row[f"layer_{index}"] = ""
    return row


def _construction_rows(source_dir: Path, simple_glazing_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in _read_csv_rows(source_dir / "construction_input_data.csv"):
        construction_name = _clean(row.get("construction_name")) or _clean(row.get("construction_code"))
        if construction_name:
            grouped.setdefault(construction_name, []).append(row)

    rows: list[dict[str, str]] = []
    for construction_name, group_rows in grouped.items():
        group_rows.sort(key=lambda item: int(float(_clean(item.get("layer_order"), "0"))))
        layers = [_clean(row.get("material_name")) for row in group_rows if _clean(row.get("material_name"))]
        output_row = _blank_construction_row(construction_name)
        for index, layer_name in enumerate(layers[:6], start=1):
            output_row[f"layer_{index}"] = layer_name
        rows.append(output_row)
        if construction_name.startswith("IBST_") and not construction_name.endswith("_Rev"):
            reverse_row = _blank_construction_row(f"{construction_name}_Rev")
            for index, layer_name in enumerate(list(reversed(layers))[:6], start=1):
                reverse_row[f"layer_{index}"] = layer_name
            rows.append(reverse_row)

    for glazing_row in simple_glazing_rows:
        glazing_name = glazing_row["simple_glazing_name"]
        construction_row = _blank_construction_row(simple_glazing_construction_name(glazing_name))
        construction_row["layer_1"] = glazing_name
        rows.append(construction_row)
    return rows


def _simple_glazing_rows(source_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in _read_csv_rows(source_dir / "glazing_input.csv"):
        if _clean(row.get("definition_method")).lower() != "2-simple":
            continue
        name = _clean(row.get("glazing_name"))
        if not name:
            continue
        rows.append(
            {
                "simple_glazing_name": name,
                "u_factor_w_per_m2k": _clean(row.get("u_value")),
                "solar_heat_gain_coefficient": _clean(row.get("solar_transmittance_or_shgc")),
                "visible_transmittance": _clean(row.get("visible_transmittance")),
            }
        )
    return rows


def _merge_rows_by_key(base_rows: list[dict[str, object]], override_rows: list[dict[str, str]], key: str) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for row in [*base_rows, *override_rows]:
        row_key = _clean(row.get(key))
        if not row_key:
            continue
        if row_key not in merged:
            order.append(row_key)
        merged[row_key] = dict(row)
    return [merged[row_key] for row_key in order]


def simple_glazing_construction_name(glazing_name: str) -> str:
    return f"C_{glazing_name}"


@lru_cache(maxsize=None)
def load_project_envelope(project_id: str | None) -> dict[str, Any]:
    source_dir = _project_envelope_dir(project_id)
    if source_dir is None:
        return {"available": False}
    simple_glazing_rows = _simple_glazing_rows(source_dir)
    return {
        "available": True,
        "source_dir": str(source_dir.relative_to(ROOT)).replace("\\", "/"),
        "material_rows": _material_rows(source_dir),
        "construction_rows": _construction_rows(source_dir, simple_glazing_rows),
        "simple_glazing_rows": simple_glazing_rows,
    }


def envelope_construction_for_opening(
    project_id: str | None,
    *,
    opening: dict[str, object],
    surface_type: str,
) -> str:
    envelope = load_project_envelope(project_id)
    if not envelope.get("available"):
        return ""
    normalized_type = _clean(surface_type)
    source_type_code = _clean(opening.get("type_code")).upper()
    if normalized_type == "Window":
        return simple_glazing_construction_name("G_WINDOW_TEMP_008")
    if normalized_type == "GlassDoor":
        return simple_glazing_construction_name("G_LOGIA_TEMP_010")
    if normalized_type == "Door":
        if source_type_code == "DG":
            return "D_MAIN_STEEL_0474"
        if source_type_code == "DWN":
            return "D_WC_UPVC_024"
        if source_type_code.startswith("DN"):
            return "D_BEDROOM_MDF_035"
    return ""


def apply_envelope_to_bundle_tables(
    *,
    project_id: str | None,
    tables: dict[str, list[dict[str, object]]],
    manifest: dict[str, object],
) -> None:
    envelope = load_project_envelope(project_id)
    if not envelope.get("available"):
        return
    tables["Material.csv"] = _merge_rows_by_key(
        list(tables.get("Material.csv", [])),
        list(envelope.get("material_rows", [])),
        "material_name",
    )
    tables["Construction.csv"] = _merge_rows_by_key(
        list(tables.get("Construction.csv", [])),
        list(envelope.get("construction_rows", [])),
        "construction_name",
    )
    tables["WindowMaterial_SimpleGlazingSystem.csv"] = _merge_rows_by_key(
        list(tables.get("WindowMaterial_SimpleGlazingSystem.csv", [])),
        list(envelope.get("simple_glazing_rows", [])),
        "simple_glazing_name",
    )
    manifest["envelope_input"] = {
        "source_dir": envelope.get("source_dir"),
        "material_rows": len(list(envelope.get("material_rows", []))),
        "construction_rows": len(list(envelope.get("construction_rows", []))),
        "simple_glazing_rows": len(list(envelope.get("simple_glazing_rows", []))),
    }
