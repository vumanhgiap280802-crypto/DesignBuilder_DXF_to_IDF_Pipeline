from __future__ import print_function

import argparse
import csv
import datetime
import json
import math
import os
import re
import sys
import traceback


DEFAULT_WORKSPACE_ROOT = r"D:\Design Builder\Xu ly data\DesignBuilder_DXF_to_IDF_Pipeline"
DEFAULT_PROJECT_ID = "apartment_a_new"
DEFAULT_MANIFEST = os.path.join(
    DEFAULT_WORKSPACE_ROOT,
    "5_output",
    DEFAULT_PROJECT_ID,
    "reports",
    "idf_handoff_manifest.json",
)
DEFAULT_DESIGNBUILDER_IN_IDF = r"C:\Users\ASUS\AppData\Local\DesignBuilder\EnergyPlus\in.idf"

HOOK_NAME = "before_energy_idf_generation"
TARGET_ADJACENCY_TEXT = "4-Adiabatic"
AREA_TOLERANCE_M2 = 0.08
ADJACENCY_ATTRIBUTE_NAME = "Adjacency"

ADJACENCY_WRITE_VALUES = [
    "3",
    "3-Adiabatic",
    "Adiabatic",
    "4-Adiabatic",
]

LOG_FIELDS = [
    "timestamp",
    "hook",
    "category",
    "surface_key",
    "surface_label",
    "action",
    "status",
    "value",
    "message",
]

INVENTORY_FIELDS = [
    "surface_key",
    "surface_label",
    "context",
    "is_candidate",
    "matches_manifest_target",
    "target_surface_name",
    "target_zone_name",
    "mapped_zone_name",
    "target_area_m2",
    "surface_area_m2",
    "area_delta_m2",
    "match_reason",
    "match_status",
    "in_idf_surface_name",
    "in_idf_zone_name",
    "in_idf_construction_name",
    "in_idf_boundary_condition",
    "in_idf_area_m2",
    "in_idf_area_delta_m2",
    "in_idf_match_status",
    "adjacency_condition",
    "is_internal_partition",
    "internal_partition_type",
    "surface_type",
    "hard_attributes",
    "title",
    "ssep_object_name",
    "ssep_object_idc",
    "gbxml_surface_type",
    "match_keys",
]


def now_text():
    return datetime.datetime.now().isoformat().split(".")[0]


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_key(value):
    text = clean(value).upper()
    output = []
    for ch in text:
        if ch.isalnum():
            output.append(ch)
    return "".join(output)


def parse_float(value):
    text = clean(value).replace(",", ".")
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def safe_name(value):
    text = clean(value)
    if not text:
        return "unknown"
    output = []
    for ch in text:
        output.append(ch if ch.isalnum() or ch in ("-", "_") else "_")
    return "".join(output)


def log_row(category, surface_key, surface_label, action, status, value="", message=""):
    return {
        "timestamp": now_text(),
        "hook": HOOK_NAME,
        "category": clean(category),
        "surface_key": clean(surface_key),
        "surface_label": clean(surface_label),
        "action": clean(action),
        "status": clean(status),
        "value": clean(value),
        "message": clean(message),
    }


def write_csv(path, rows):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_surface_inventory(path, rows):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, payload):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def read_json(path):
    with open(path, "r") as handle:
        text = handle.read()
    if text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text)


def report_dir(workspace_root, project_id):
    return os.path.join(workspace_root, "5_output", project_id, "reports")


def resolve_workspace_path(workspace_root, path_value):
    path_value = clean(path_value)
    if not path_value:
        return ""
    if os.path.isabs(path_value):
        return path_value
    return os.path.join(workspace_root, path_value)


def get_designbuilder_api():
    return globals().get("api_environment"), globals().get("active_building")


def to_list(iterable):
    if iterable is None:
        return []
    try:
        return list(iterable)
    except Exception:
        items = []
        try:
            for item in iterable:
                items.append(item)
        except Exception:
            return []
        return items


def object_prop(obj, prop_name):
    try:
        return clean(getattr(obj, prop_name, ""))
    except Exception:
        return ""


def object_float_prop(obj, prop_name):
    try:
        value = getattr(obj, prop_name)
    except Exception:
        return None
    return parse_float(value)


def object_label(obj):
    if obj is None:
        return "missing"
    values = []
    for prop_name in ("Name", "IDFName", "IdfName", "DisplayName", "Id", "ID", "id"):
        value = object_prop(obj, prop_name)
        if value and value not in values:
            values.append(value)
    if values:
        return " | ".join(values)
    try:
        return obj.__class__.__name__
    except Exception:
        return "unknown"


def call_get_attribute(target, attribute_name):
    getter = getattr(target, "GetAttribute", None) or getattr(target, "getAttribute", None)
    if getter is None:
        return False, "", "Target object has no GetAttribute."
    try:
        value = getter(attribute_name)
        return True, clean(value), "Read {0}={1}.".format(attribute_name, clean(value))
    except Exception as exc:
        return False, "", str(exc)


def read_surface_attribute(surface, attribute_name):
    ok, value, _message = call_get_attribute(surface, attribute_name)
    if ok:
        return value
    return ""


def call_set_attribute(target, attribute_name, value):
    setter = getattr(target, "SetAttribute", None) or getattr(target, "setAttribute", None)
    if setter is None:
        return False, "Target object has no SetAttribute."
    try:
        result = setter(attribute_name, str(value))
        if result is False:
            return False, "SetAttribute returned False."
        return True, "Set {0}={1}.".format(attribute_name, value)
    except Exception as exc:
        return False, str(exc)


def simple_property_value(target, property_name):
    try:
        value = getattr(target, property_name)
    except Exception as exc:
        return "", str(exc)
    return clean(value), "Read property {0}.".format(property_name)


def hard_attribute_names(surface):
    names = []
    try:
        hard_attributes = getattr(surface, "HardAttributes", None)
    except Exception:
        hard_attributes = None
    for item in to_list(hard_attributes):
        for prop_name in ("Name", "name", "Key", "key"):
            value = object_prop(item, prop_name)
            if value and value not in names:
                names.append(value)
    return names


def first_surface_area(surface):
    for prop_name in ("Area", "GrossArea", "NettArea"):
        value = object_float_prop(surface, prop_name)
        if value is not None:
            return value
    for attr_name in ("SSEPObjectAreaInOP", "Area", "GrossArea", "NettArea"):
        value = parse_float(read_surface_attribute(surface, attr_name))
        if value is not None:
            return value
    return None


def collect_buildings(api_env, active_bldg):
    buildings = []
    seen = set()

    def add_building(building):
        if building is None:
            return
        key = id(building)
        if key in seen:
            return
        seen.add(key)
        buildings.append(building)

    add_building(active_bldg)
    if buildings:
        return buildings
    site = getattr(api_env, "Site", None) if api_env is not None else None
    if site is not None:
        for attr_name in ("Buildings", "buildings"):
            for building in to_list(getattr(site, attr_name, None)):
                add_building(building)
        for method_name in ("GetBuildingIterator", "getBuildingIterator"):
            method = getattr(site, method_name, None)
            if method is None:
                continue
            try:
                for building in to_list(method()):
                    add_building(building)
            except Exception:
                pass
    return buildings


def collect_child_objects(obj, attr_names, method_names):
    children = []
    for attr_name in attr_names:
        try:
            children.extend(to_list(getattr(obj, attr_name, None)))
        except Exception:
            pass
    for method_name in method_names:
        method = getattr(obj, method_name, None)
        if method is None:
            continue
        try:
            children.extend(to_list(method()))
        except Exception:
            pass
    return [child for child in children if child is not None]


def collect_surfaces(api_env, active_bldg):
    surfaces = []
    seen = set()

    def add_surface(surface, context):
        if surface is None:
            return
        key = id(surface)
        if key in seen:
            return
        seen.add(key)
        surfaces.append((surface, context))

    for building in collect_buildings(api_env, active_bldg):
        building_label = object_label(building)
        blocks = collect_child_objects(
            building,
            ("BuildingBlocks", "buildingBlocks", "Blocks", "blocks"),
            ("GetBuildingBlockIterator", "getBuildingBlockIterator"),
        )
        for block in blocks:
            block_label = object_label(block)
            zones = collect_child_objects(
                block,
                ("Zones", "zones"),
                ("GetZoneIterator", "getZoneIterator", "GetZones"),
            )
            for zone in zones:
                zone_label = object_label(zone)
                for surface in collect_child_objects(
                    zone,
                    ("Surfaces", "surfaces"),
                    ("GetSurfaceIterator", "getSurfaceIterator", "GetSurfaces"),
                ):
                    add_surface(surface, "{0} > {1} > {2}".format(building_label, block_label, zone_label))

            for surface in collect_child_objects(
                block,
                ("Surfaces", "surfaces"),
                ("GetSurfaceIterator", "getSurfaceIterator", "GetSurfaces"),
            ):
                add_surface(surface, "{0} > {1}".format(building_label, block_label))

        for surface in collect_child_objects(
            building,
            ("Surfaces", "surfaces"),
            ("GetSurfaceIterator", "getSurfaceIterator", "GetSurfaces"),
        ):
            add_surface(surface, building_label)

    return surfaces


def load_manifest_target_names(manifest_path):
    if not manifest_path or not os.path.isfile(manifest_path):
        return []
    payload = read_json(manifest_path)
    names = []
    for item in payload.get("adiabatic_wall_targets", []):
        name = clean(item.get("surface_name") if isinstance(item, dict) else "")
        if name and name not in names:
            names.append(name)
    return names


def surface_vertices_from_row(row):
    vertices = []
    count = int(parse_float(row.get("number_of_vertices")) or 0)
    for index in range(1, count + 1):
        x = parse_float(row.get("v{0}_x".format(index)))
        y = parse_float(row.get("v{0}_y".format(index)))
        z = parse_float(row.get("v{0}_z".format(index)))
        if x is None or y is None or z is None:
            continue
        vertices.append((x, y, z))
    return vertices


def triangle_area(a, b, c):
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return 0.5 * math.sqrt(cross[0] * cross[0] + cross[1] * cross[1] + cross[2] * cross[2])


def polygon_area(vertices):
    if len(vertices) < 3:
        return None
    area = 0.0
    origin = vertices[0]
    for index in range(1, len(vertices) - 1):
        area += triangle_area(origin, vertices[index], vertices[index + 1])
    return area


def strip_idf_comment(line):
    if "!" in line:
        return line.split("!", 1)[0]
    return line


def split_idf_fields(lines):
    text = " ".join(strip_idf_comment(line) for line in lines)
    fields = []
    current = []
    for ch in text:
        if ch in (",", ";"):
            fields.append(clean("".join(current)))
            current = []
            if ch == ";":
                break
        else:
            current.append(ch)
    return fields


def comment_zone_name(comment):
    match = re.match(r"Block\s+\d+\s*,\s*([^,]+)\s*,", clean(comment))
    if not match:
        return ""
    return clean(match.group(1))


def idf_surface_from_fields(fields, comment):
    if not fields or clean(fields[0]).lower() != "buildingsurface:detailed":
        return None
    if len(fields) < 11:
        return None
    count = int(parse_float(fields[10]) or 0)
    vertices = []
    start = 11
    for index in range(count):
        offset = start + index * 3
        if offset + 2 >= len(fields):
            break
        x = parse_float(fields[offset])
        y = parse_float(fields[offset + 1])
        z = parse_float(fields[offset + 2])
        if x is None or y is None or z is None:
            continue
        vertices.append((x, y, z))
    return {
        "surface_name": clean(fields[1]),
        "surface_type": clean(fields[2]),
        "construction_name": clean(fields[3]),
        "zone_name": clean(fields[4]),
        "outside_boundary_condition": clean(fields[5]),
        "outside_boundary_condition_object": clean(fields[6]) if len(fields) > 6 else "",
        "area_m2": polygon_area(vertices),
        "comment": clean(comment),
        "comment_zone_name": comment_zone_name(comment),
    }


def load_designbuilder_in_idf_surfaces(path):
    surfaces = []
    path = clean(path)
    if not path or not os.path.isfile(path):
        return surfaces

    current_lines = []
    current_comment = ""
    last_comment = ""
    in_surface = False
    with open(path, "r") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if stripped.startswith("!") and not in_surface:
                last_comment = clean(stripped[1:])
                continue
            content = strip_idf_comment(raw_line).strip()
            if not content:
                continue
            if not in_surface:
                if content.lower().startswith("buildingsurface:detailed"):
                    current_lines = [raw_line]
                    current_comment = last_comment
                    in_surface = True
                    if ";" in raw_line:
                        surface = idf_surface_from_fields(split_idf_fields(current_lines), current_comment)
                        if surface:
                            surfaces.append(surface)
                        current_lines = []
                        in_surface = False
                continue

            current_lines.append(raw_line)
            if ";" in raw_line:
                surface = idf_surface_from_fields(split_idf_fields(current_lines), current_comment)
                if surface:
                    surfaces.append(surface)
                current_lines = []
                current_comment = ""
                in_surface = False
    return surfaces


def surface_type_matches(source_type, target_type):
    source_type = clean(source_type).lower()
    target_type = clean(target_type).lower()
    if not source_type or not target_type:
        return True
    return source_type == target_type


def construction_matches(source_construction, target_construction):
    target_key = normalize_key(target_construction)
    if not target_key:
        return True
    source_key = normalize_key(source_construction)
    return bool(source_key and target_key in source_key)


def in_idf_zone_matches(surface_profile, target_zone_name):
    target_zone_name = clean(target_zone_name)
    if not target_zone_name:
        return True
    if normalize_key(surface_profile.get("comment_zone_name")) == normalize_key(target_zone_name):
        return True
    return context_matches_zone(surface_profile.get("zone_name"), target_zone_name)


def map_designbuilder_in_idf_to_targets(target_profiles, in_idf_surfaces):
    logs = []
    for profile in target_profiles:
        target_name = clean(profile.get("surface_name"))
        target_area = profile.get("area_m2")
        target_zone = clean(profile.get("zone_name"))
        target_type = clean(profile.get("surface_type"))
        target_construction = clean(profile.get("construction_name")) or clean(profile.get("target_construction"))
        matches = []
        if target_area is not None:
            for surface in in_idf_surfaces:
                if not surface_type_matches(surface.get("surface_type"), target_type):
                    continue
                if not construction_matches(surface.get("construction_name"), target_construction):
                    continue
                if not in_idf_zone_matches(surface, target_zone):
                    continue
                surface_area = surface.get("area_m2")
                if surface_area is None:
                    continue
                delta = abs(float(surface_area) - float(target_area))
                if delta <= AREA_TOLERANCE_M2:
                    matches.append((surface, delta))
        matches.sort(key=lambda item: item[1])
        if len(matches) == 1:
            surface, delta = matches[0]
            profile["in_idf_surface_name"] = clean(surface.get("surface_name"))
            profile["in_idf_zone_name"] = clean(surface.get("zone_name"))
            profile["in_idf_construction_name"] = clean(surface.get("construction_name"))
            profile["in_idf_boundary_condition"] = clean(surface.get("outside_boundary_condition"))
            profile["in_idf_area_m2"] = surface.get("area_m2")
            profile["in_idf_area_delta_m2"] = delta
            profile["in_idf_match_status"] = "unique_zone_area_construction"
            logs.append(
                log_row(
                    "in_idf_mapping",
                    "global",
                    target_name,
                    "map_target",
                    "PASS",
                    profile["in_idf_surface_name"],
                    "Matched target to DesignBuilder in.idf surface by zone, area, type, and construction.",
                )
            )
        elif len(matches) > 1:
            profile["in_idf_match_status"] = "ambiguous"
            logs.append(
                log_row(
                    "in_idf_mapping",
                    "global",
                    target_name,
                    "map_target",
                    "WARN",
                    str(len(matches)),
                    "More than one DesignBuilder in.idf surface matched this target.",
                )
            )
        else:
            profile["in_idf_match_status"] = "not_found"
            logs.append(
                log_row(
                    "in_idf_mapping",
                    "global",
                    target_name,
                    "map_target",
                    "WARN",
                    "",
                    "No DesignBuilder in.idf surface matched this target.",
                )
            )
    return logs


def load_target_profiles(workspace_root, manifest_path, target_names):
    if not manifest_path or not os.path.isfile(manifest_path):
        return []
    payload = read_json(manifest_path)
    target_by_name = {}
    for item in payload.get("adiabatic_wall_targets", []):
        if not isinstance(item, dict):
            continue
        name = clean(item.get("surface_name"))
        if name:
            target_by_name[name] = dict(item)
    for name in target_names:
        if name and name not in target_by_name:
            target_by_name[name] = {"surface_name": name}

    bundle_dir = resolve_workspace_path(workspace_root, payload.get("bundle_output_dir"))
    surface_csv = os.path.join(bundle_dir, "BuildingSurface_Detailed.csv")
    if os.path.isfile(surface_csv):
        with open(surface_csv, "r") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                name = clean(row.get("surface_name"))
                if name not in target_by_name:
                    continue
                profile = target_by_name[name]
                profile["zone_name"] = clean(row.get("zone_name")) or clean(profile.get("zone_name"))
                profile["surface_type"] = clean(row.get("surface_type")) or clean(profile.get("surface_type"))
                profile["construction_name"] = clean(row.get("construction_name")) or clean(profile.get("construction_name"))
                profile["outside_boundary_condition"] = clean(row.get("outside_boundary_condition")) or clean(
                    profile.get("outside_boundary_condition")
                )
                area = polygon_area(surface_vertices_from_row(row))
                if area is not None:
                    profile["area_m2"] = area

    profiles = []
    for name in target_names:
        profile = target_by_name.get(name)
        if profile:
            profiles.append(profile)
    return profiles


def load_zone_floor_profiles(workspace_root, manifest_path):
    if not manifest_path or not os.path.isfile(manifest_path):
        return []
    payload = read_json(manifest_path)
    bundle_dir = resolve_workspace_path(workspace_root, payload.get("bundle_output_dir"))
    surface_csv = os.path.join(bundle_dir, "BuildingSurface_Detailed.csv")
    profiles = []
    if not os.path.isfile(surface_csv):
        return profiles
    with open(surface_csv, "r") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if clean(row.get("surface_type")).lower() != "floor":
                continue
            zone_name = clean(row.get("zone_name"))
            if not zone_name:
                continue
            area = polygon_area(surface_vertices_from_row(row))
            if area is None:
                continue
            profiles.append({"zone_name": zone_name, "area_m2": area})
    return profiles


def add_unique(values, value):
    value = clean(value)
    if value and value not in values:
        values.append(value)


def zone_match_tokens(zone_name):
    raw = clean(zone_name).upper()
    tokens = []
    if not raw:
        return tokens

    prefix = "APARTMENT_A_NEW_"
    tail = raw[len(prefix) :] if raw.startswith(prefix) else raw
    tail_db = tail.replace("_", "X")
    db_style = raw.replace("_", "X")
    room_number = re.match(r"^([A-Z]+)_(\d+)$", tail)

    add_unique(tokens, tail)
    add_unique(tokens, tail_db)
    if room_number:
        room_code = room_number.group(1)
        number = room_number.group(2)
        add_unique(tokens, raw)
        add_unique(tokens, db_style)
        if number == "01":
            add_unique(tokens, room_code + "X0")
            add_unique(tokens, "APARTMENTXAXNEWX" + room_code + "X0")
        elif number == "02":
            add_unique(tokens, room_code + "X0Q1")
            add_unique(tokens, "APARTMENTXAXNEWX" + room_code + "X0Q1")
    else:
        add_unique(tokens, raw)
        add_unique(tokens, db_style)
        if len(db_style) > 3:
            add_unique(tokens, db_style[:-1])
        if len(tail_db) > 3:
            add_unique(tokens, tail_db[:-1])

    return [normalize_key(token) for token in tokens if normalize_key(token)]


def context_matches_zone(context, zone_name, context_zone_map=None):
    if context_zone_map:
        mapped_zone = clean(context_zone_map.get(context))
        if mapped_zone:
            return normalize_key(mapped_zone) == normalize_key(zone_name)
    context_key = normalize_key(context)
    for token in zone_match_tokens(zone_name):
        if token and token in context_key:
            return True
    return False


def target_match_for_surface(context, surface_area, target_profiles, context_zone_map=None):
    matches = []
    if surface_area is None:
        return matches
    for profile in target_profiles:
        target_area = profile.get("area_m2")
        zone_name = clean(profile.get("zone_name"))
        if target_area is None or not zone_name:
            continue
        if not context_matches_zone(context, zone_name, context_zone_map):
            continue
        delta = abs(float(surface_area) - float(target_area))
        if delta <= AREA_TOLERANCE_M2:
            matches.append((profile, delta))
    matches.sort(key=lambda item: item[1])
    return matches


def infer_context_zone_map(surface_entries, zone_floor_profiles):
    context_zone_map = {}
    for entry in surface_entries:
        if clean(entry.get("surface_type")).lower() != "floor":
            continue
        surface_area = entry.get("surface_area")
        if surface_area is None:
            continue
        matches = []
        for profile in zone_floor_profiles:
            delta = abs(float(surface_area) - float(profile.get("area_m2")))
            if delta <= AREA_TOLERANCE_M2:
                matches.append((profile, delta))
        matches.sort(key=lambda item: item[1])
        if len(matches) == 1:
            context_zone_map[entry.get("context")] = clean(matches[0][0].get("zone_name"))
    return context_zone_map


def surface_match_keys(surface):
    values = []
    for prop_name in ("Name", "IDFName", "IdfName", "DisplayName", "Id", "ID", "id"):
        value = object_prop(surface, prop_name)
        if value:
            values.append(value)
    for attr_name in ("Name", "IDFName", "IdfName", "SurfaceName", "SSEPObjectNameInOP"):
        ok, value, _message = call_get_attribute(surface, attr_name)
        if ok and value:
            values.append(value)
    return values


def surface_matches_in_idf_profile(surface, target_profiles):
    surface_keys = [normalize_key(value) for value in surface_match_keys(surface)]
    matches = []
    for profile in target_profiles:
        in_idf_surface_name = clean(profile.get("in_idf_surface_name"))
        if not in_idf_surface_name:
            continue
        target_key = normalize_key(in_idf_surface_name)
        if target_key and target_key in surface_keys:
            matches.append(profile)
    return matches


def surface_matches_targets(surface, target_names):
    if not target_names:
        return False
    surface_keys = [normalize_key(value) for value in surface_match_keys(surface)]
    target_keys = [normalize_key(value) for value in target_names]
    for surface_key in surface_keys:
        for target_key in target_keys:
            if surface_key and target_key and (surface_key == target_key or target_key in surface_key):
                return True
    return False


def surface_is_candidate(surface):
    label = object_label(surface).lower()
    type_value = object_prop(surface, "Type").lower()
    is_internal = object_prop(surface, "IsInternalPartition").lower()
    if "partition" in label or "partition" in type_value:
        return True
    if "wall" in label or "wall" in type_value:
        return True
    if is_internal in ("true", "1"):
        return True
    return False


def read_adjacency_state(surface):
    state = {}
    value, message = simple_property_value(surface, "AdjacencyCondition")
    state["AdjacencyCondition"] = value
    state["AdjacencyCondition_message"] = message
    value, message = simple_property_value(surface, "IsInternalPartition")
    state["IsInternalPartition"] = value
    state["IsInternalPartition_message"] = message
    value, message = simple_property_value(surface, "InternalPartitionType")
    state["InternalPartitionType"] = value
    state["InternalPartitionType_message"] = message
    return state


def is_adiabatic_value(value):
    value = clean(value).lower()
    return value in ("3", "3-adiabatic", "4", "4-adiabatic", "adiabatic") or "adiabatic" in value


def is_true_text(value):
    return clean(value).lower() in ("true", "1", "yes")


def verify_surface_adiabatic(surface):
    state = read_adjacency_state(surface)
    if is_adiabatic_value(state.get("AdjacencyCondition")):
        return True, "AdjacencyCondition={0}".format(state.get("AdjacencyCondition"))
    adjacency_attr = read_surface_attribute(surface, ADJACENCY_ATTRIBUTE_NAME)
    return False, "AdjacencyCondition={0}; {1}={2}".format(
        state.get("AdjacencyCondition"),
        ADJACENCY_ATTRIBUTE_NAME,
        adjacency_attr,
    )


def try_set_surface_adjacency(surface, logs, surface_key, surface_label, allow_write):
    state = read_adjacency_state(surface)
    logs.append(
        log_row(
            "surface_adjacency_ui",
            surface_key,
            surface_label,
            "ReadState:before",
            "READ_OK",
            "AdjacencyCondition={0}; IsInternalPartition={1}; InternalPartitionType={2}".format(
                state.get("AdjacencyCondition"),
                state.get("IsInternalPartition"),
                state.get("InternalPartitionType"),
            ),
            "HardAttributes={0}".format(";".join(hard_attribute_names(surface))),
        )
    )

    verified, message = verify_surface_adiabatic(surface)
    if verified:
        logs.append(
            log_row(
                "surface_adjacency_ui",
                surface_key,
                surface_label,
                "Verify:before",
                "ALREADY_ADIABATIC",
                TARGET_ADJACENCY_TEXT,
                message,
            )
        )
        return True

    if not allow_write:
        logs.append(
            log_row(
                "surface_adjacency_ui",
                surface_key,
                surface_label,
                "SetAttribute",
                "WRITE_GUARDED",
                TARGET_ADJACENCY_TEXT,
                "Skipped. Re-run with --allow-write inside DesignBuilder to attempt UI Adjacency change.",
            )
        )
        return False

    for value in ADJACENCY_WRITE_VALUES:
        ok, set_message = call_set_attribute(surface, ADJACENCY_ATTRIBUTE_NAME, value)
        logs.append(
            log_row(
                "surface_adjacency_ui",
                surface_key,
                surface_label,
                "SetAttribute:{0}".format(ADJACENCY_ATTRIBUTE_NAME),
                "WRITE_OK" if ok else "WRITE_FAIL",
                value,
                set_message,
            )
        )
        if not ok:
            continue
        update_surface_if_possible(surface, logs, surface_key, surface_label)
        verified, verify_message = verify_surface_adiabatic(surface)
        logs.append(
            log_row(
                "surface_adjacency_ui",
                surface_key,
                surface_label,
                "Verify:{0}".format(ADJACENCY_ATTRIBUTE_NAME),
                "VERIFIED" if verified else "VERIFY_FAIL",
                value,
                verify_message,
            )
        )
        if verified:
            return True

    return False


def update_surface_if_possible(surface, logs, surface_key, surface_label):
    for method_name in ("UpdateTileAttributes",):
        method = getattr(surface, method_name, None)
        if method is None:
            continue
        try:
            method()
            logs.append(log_row("surface_adjacency_ui", surface_key, surface_label, method_name, "PASS", "", "Method completed."))
        except Exception as exc:
            logs.append(log_row("surface_adjacency_ui", surface_key, surface_label, method_name, "WARN", "", str(exc)))


def summarize(rows, api_available, allow_write):
    counts = {}
    for row in rows:
        status = clean(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    if not api_available:
        overall = "DRY_RUN"
    elif counts.get("VERIFIED", 0) or counts.get("ALREADY_ADIABATIC", 0):
        overall = "PASS"
    elif allow_write and counts.get("WRITE_FAIL", 0):
        overall = "WARN"
    elif counts.get("WRITE_GUARDED", 0):
        overall = "READY_TO_WRITE"
    else:
        overall = "WARN"
    return overall, counts


def run_surface_adjacency_ui_apply(
    workspace_root=DEFAULT_WORKSPACE_ROOT,
    project_id=DEFAULT_PROJECT_ID,
    manifest_path=DEFAULT_MANIFEST,
    designbuilder_in_idf_path=DEFAULT_DESIGNBUILDER_IN_IDF,
    target_names=None,
    allow_write=False,
):
    workspace_root = os.path.abspath(workspace_root)
    target_names = list(target_names or [])
    for name in load_manifest_target_names(manifest_path):
        if name not in target_names:
            target_names.append(name)
    target_profiles = load_target_profiles(workspace_root, manifest_path, target_names)
    zone_floor_profiles = load_zone_floor_profiles(workspace_root, manifest_path)

    logs = []
    in_idf_surfaces = load_designbuilder_in_idf_surfaces(designbuilder_in_idf_path)
    logs.append(
        log_row(
            "in_idf_source",
            "global",
            "DesignBuilder EnergyPlus in.idf",
            "load_surfaces",
            "PASS" if in_idf_surfaces else "WARN",
            str(len(in_idf_surfaces)),
            designbuilder_in_idf_path if in_idf_surfaces else "No BuildingSurface:Detailed rows loaded from path.",
        )
    )
    if target_profiles and in_idf_surfaces:
        logs.extend(map_designbuilder_in_idf_to_targets(target_profiles, in_idf_surfaces))

    api_env, active_bldg = get_designbuilder_api()
    api_available = api_env is not None or active_bldg is not None
    logs.append(
        log_row(
            "designbuilder_api",
            "global",
            "global",
            "detect",
            "PASS" if api_available else "DRY_RUN",
            "",
            "DesignBuilder API is available." if api_available else "DesignBuilder API is not available in this process.",
        )
    )
    logs.append(
        log_row(
            "surface_target",
            "global",
            "manifest/arguments",
            "load_targets",
            "PASS" if target_names else "WARN",
            ";".join(target_names),
            "Targets are loaded from manifest plus --target-surface arguments.",
        )
    )
    logs.append(
        log_row(
            "surface_target",
            "global",
            "target_profiles",
            "load_target_profiles",
            "PASS" if target_profiles else "WARN",
            str(len(target_profiles)),
            "Profiles include target zone and calculated IDF surface area when available.",
        )
    )

    matched_count = 0
    verified_count = 0
    candidate_count = 0
    inventory_rows = []
    if api_available:
        surfaces = collect_surfaces(api_env, active_bldg)
        logs.append(
            log_row(
                "surface_scan",
                "global",
                "DesignBuilder surfaces",
                "collect",
                "PASS" if surfaces else "WARN",
                str(len(surfaces)),
                "Collected surfaces from active building/site hierarchy.",
                )
            )
        surface_entries = []
        for index, (surface, context) in enumerate(surfaces, start=1):
            surface_label = object_label(surface)
            surface_key = "surface_{0}".format(index)
            candidate = surface_is_candidate(surface)
            state = read_adjacency_state(surface)
            surface_area = first_surface_area(surface)
            surface_type = object_prop(surface, "Type")
            direct_name_match = surface_matches_targets(surface, target_names)
            in_idf_profile_matches = surface_matches_in_idf_profile(surface, target_profiles)
            surface_entries.append(
                {
                    "surface": surface,
                    "context": context,
                    "surface_label": surface_label,
                    "surface_key": surface_key,
                    "candidate": candidate,
                    "state": state,
                    "surface_area": surface_area,
                    "surface_type": surface_type,
                    "direct_name_match": direct_name_match,
                    "in_idf_profile_matches": in_idf_profile_matches,
                }
            )

        context_zone_map = infer_context_zone_map(surface_entries, zone_floor_profiles)
        for context, zone_name in sorted(context_zone_map.items()):
            logs.append(
                log_row(
                    "zone_context",
                    "global",
                    zone_name,
                    "map_context_by_floor_area",
                    "PASS",
                    context,
                    "DesignBuilder context mapped to source zone by matching floor area.",
                )
            )

        target_surface_counts = {}
        for entry in surface_entries:
            state = entry["state"]
            candidate = entry["candidate"]
            direct_name_match = entry["direct_name_match"]
            in_idf_profile_matches = entry["in_idf_profile_matches"]
            profile_eligible = candidate and (
                not is_true_text(state.get("IsInternalPartition"))
                or is_adiabatic_value(state.get("AdjacencyCondition"))
            )
            profile_matches = (
                target_match_for_surface(entry["context"], entry["surface_area"], target_profiles, context_zone_map)
                if profile_eligible and not in_idf_surfaces
                else []
            )
            if len(in_idf_profile_matches) == 1:
                matched_profile = in_idf_profile_matches[0]
                entry["matched_profile"] = matched_profile
                entry["match_reason"] = "in_idf_surface_name"
                target_name = clean(matched_profile.get("surface_name"))
                target_surface_counts[target_name] = target_surface_counts.get(target_name, 0) + 1
            elif len(in_idf_profile_matches) > 1:
                entry["matched_profile"] = in_idf_profile_matches[0]
                entry["match_reason"] = "ambiguous_in_idf_surface_name"
            elif direct_name_match:
                entry["matched_profile"] = None
                entry["match_reason"] = "direct_name"
            elif len(profile_matches) == 1:
                matched_profile = profile_matches[0][0]
                entry["matched_profile"] = matched_profile
                entry["match_reason"] = "zone_area"
                target_name = clean(matched_profile.get("surface_name"))
                target_surface_counts[target_name] = target_surface_counts.get(target_name, 0) + 1
            elif len(profile_matches) > 1:
                entry["matched_profile"] = profile_matches[0][0]
                entry["match_reason"] = "ambiguous_zone_area"
            else:
                entry["matched_profile"] = None
                entry["match_reason"] = ""

        for entry in surface_entries:
            surface = entry["surface"]
            context = entry["context"]
            surface_label = entry["surface_label"]
            surface_key = entry["surface_key"]
            candidate = entry["candidate"]
            state = entry["state"]
            surface_area = entry["surface_area"]
            matched_profile = entry["matched_profile"]
            direct_name_match = entry["direct_name_match"]
            match_reason = entry["match_reason"]
            matched = bool(direct_name_match)
            target_surface_name = ""
            target_zone_name = ""
            mapped_zone_name = clean(context_zone_map.get(context))
            target_area = None
            area_delta = None
            match_status = ""
            in_idf_surface_name = ""
            in_idf_zone_name = ""
            in_idf_construction_name = ""
            in_idf_boundary_condition = ""
            in_idf_area = None
            in_idf_area_delta = None
            in_idf_match_status = ""
            if matched_profile:
                target_surface_name = clean(matched_profile.get("surface_name"))
                target_zone_name = clean(matched_profile.get("zone_name"))
                target_area = matched_profile.get("area_m2")
                in_idf_surface_name = clean(matched_profile.get("in_idf_surface_name"))
                in_idf_zone_name = clean(matched_profile.get("in_idf_zone_name"))
                in_idf_construction_name = clean(matched_profile.get("in_idf_construction_name"))
                in_idf_boundary_condition = clean(matched_profile.get("in_idf_boundary_condition"))
                in_idf_area = matched_profile.get("in_idf_area_m2")
                in_idf_area_delta = matched_profile.get("in_idf_area_delta_m2")
                in_idf_match_status = clean(matched_profile.get("in_idf_match_status"))
                if surface_area is not None and target_area is not None:
                    area_delta = abs(float(surface_area) - float(target_area))
                if match_reason == "in_idf_surface_name" and target_surface_counts.get(target_surface_name, 0) == 1:
                    matched = True
                    match_status = "unique_in_idf_surface_name"
                elif match_reason == "in_idf_surface_name":
                    match_status = "duplicate_in_idf_surface_name"
                elif match_reason == "zone_area" and target_surface_counts.get(target_surface_name, 0) == 1:
                    matched = True
                    match_status = "unique_zone_area"
                elif match_reason == "zone_area":
                    match_status = "duplicate_zone_area"
                else:
                    match_status = match_reason
            elif direct_name_match:
                match_status = "direct_name"

            inventory_rows.append(
                {
                    "surface_key": surface_key,
                    "surface_label": surface_label,
                    "context": context,
                    "is_candidate": "yes" if candidate else "no",
                    "matches_manifest_target": "yes" if matched else "no",
                    "target_surface_name": target_surface_name,
                    "target_zone_name": target_zone_name,
                    "mapped_zone_name": mapped_zone_name,
                    "target_area_m2": "" if target_area is None else "{0:.3f}".format(float(target_area)),
                    "surface_area_m2": "" if surface_area is None else "{0:.3f}".format(float(surface_area)),
                    "area_delta_m2": "" if area_delta is None else "{0:.3f}".format(float(area_delta)),
                    "match_reason": match_reason,
                    "match_status": match_status,
                    "in_idf_surface_name": in_idf_surface_name,
                    "in_idf_zone_name": in_idf_zone_name,
                    "in_idf_construction_name": in_idf_construction_name,
                    "in_idf_boundary_condition": in_idf_boundary_condition,
                    "in_idf_area_m2": "" if in_idf_area is None else "{0:.3f}".format(float(in_idf_area)),
                    "in_idf_area_delta_m2": "" if in_idf_area_delta is None else "{0:.3f}".format(float(in_idf_area_delta)),
                    "in_idf_match_status": in_idf_match_status,
                    "adjacency_condition": state.get("AdjacencyCondition", ""),
                    "is_internal_partition": state.get("IsInternalPartition", ""),
                    "internal_partition_type": state.get("InternalPartitionType", ""),
                    "surface_type": entry["surface_type"],
                    "hard_attributes": ";".join(hard_attribute_names(surface)),
                    "title": read_surface_attribute(surface, "Title"),
                    "ssep_object_name": read_surface_attribute(surface, "SSEPObjectNameInOP"),
                    "ssep_object_idc": read_surface_attribute(surface, "SSEPObjectIDCInOP"),
                    "gbxml_surface_type": read_surface_attribute(surface, "gbXMLSurfaceType"),
                    "match_keys": ";".join(surface_match_keys(surface)),
                }
            )
            if candidate:
                candidate_count += 1
            if not matched:
                if match_status == "duplicate_zone_area":
                    logs.append(
                        log_row(
                            "surface_match",
                            surface_key,
                            surface_label,
                            "match",
                            "SKIP_DUPLICATE_MATCH",
                            context,
                            "Target {0} matched more than one DesignBuilder surface at same zone/area.".format(
                                target_surface_name
                            ),
                        )
                    )
                elif match_status == "duplicate_in_idf_surface_name":
                    logs.append(
                        log_row(
                            "surface_match",
                            surface_key,
                            surface_label,
                            "match",
                            "SKIP_DUPLICATE_MATCH",
                            context,
                            "Target {0} matched more than one DesignBuilder API surface by in.idf surface name.".format(
                                target_surface_name
                            ),
                        )
                    )
                continue
            matched_count += 1
            logs.append(
                log_row(
                    "surface_match",
                    surface_key,
                    surface_label,
                    "match",
                    "MATCH_TARGET",
                    context,
                    "Surface selected for UI Adjacency attempt. target={0}; reason={1}; area={2}".format(
                        target_surface_name or "direct_name",
                        match_status or match_reason,
                        "" if surface_area is None else "{0:.3f}".format(float(surface_area)),
                    ),
                )
            )
            if try_set_surface_adjacency(surface, logs, surface_key, surface_label, allow_write):
                verified_count += 1

    overall, counts = summarize(logs, api_available, allow_write)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = "designbuilder_surface_adjacency_ui_apply_{0}_{1}".format(safe_name(project_id), stamp)
    out_dir = report_dir(workspace_root, project_id)
    csv_path = os.path.join(out_dir, base_name + ".csv")
    json_path = os.path.join(out_dir, base_name + ".json")
    inventory_path = os.path.join(out_dir, base_name + "_surface_inventory.csv")
    write_csv(csv_path, logs)
    write_surface_inventory(inventory_path, inventory_rows)
    write_json(
        json_path,
        {
            "created_at": now_text(),
            "overall_status": overall,
            "workspace_root": workspace_root,
            "project_id": project_id,
            "manifest_path": manifest_path,
            "designbuilder_in_idf_path": designbuilder_in_idf_path,
            "designbuilder_in_idf_surface_count": len(in_idf_surfaces),
            "target_names": target_names,
            "allow_write": bool(allow_write),
            "api_available": bool(api_available),
            "candidate_surface_count": candidate_count,
            "matched_surface_count": matched_count,
            "verified_surface_count": verified_count,
            "zone_floor_profile_count": len(zone_floor_profiles),
            "target_adjacency": TARGET_ADJACENCY_TEXT,
            "attribute_name": ADJACENCY_ATTRIBUTE_NAME,
            "value_candidates": ADJACENCY_WRITE_VALUES,
            "status_counts": counts,
            "csv_report": csv_path,
            "surface_inventory_report": inventory_path,
        },
    )
    return overall, csv_path, json_path, counts


def before_energy_idf_generation():
    return run_surface_adjacency_ui_apply(allow_write=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Apply DesignBuilder surface UI Adjacency=4-Adiabatic for target surfaces.")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE_ROOT)
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--designbuilder-in-idf", default=DEFAULT_DESIGNBUILDER_IN_IDF)
    parser.add_argument("--target-surface", action="append", default=[])
    parser.add_argument("--allow-write", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        overall, csv_path, json_path, counts = run_surface_adjacency_ui_apply(
            workspace_root=args.workspace,
            project_id=args.project,
            manifest_path=args.manifest,
            designbuilder_in_idf_path=args.designbuilder_in_idf,
            target_names=args.target_surface,
            allow_write=args.allow_write,
        )
    except Exception:
        out_dir = report_dir(os.path.abspath(args.workspace), args.project)
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        error_path = os.path.join(out_dir, "designbuilder_surface_adjacency_ui_apply_error_{0}.txt".format(stamp))
        with open(error_path, "w") as handle:
            handle.write(traceback.format_exc())
        print("status=FAIL")
        print("error={0}".format(error_path))
        return 1

    print("status={0}".format(overall))
    print("csv={0}".format(csv_path))
    print("json={0}".format(json_path))
    print("counts={0}".format(counts))
    return 0 if overall in ("PASS", "READY_TO_WRITE", "DRY_RUN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
