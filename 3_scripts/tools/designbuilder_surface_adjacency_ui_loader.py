from __future__ import print_function

import sys


TOOL_DIR = r"D:\Design Builder\Xu ly data\DesignBuilder_DXF_to_IDF_Pipeline\3_scripts\tools"
WORKSPACE_ROOT = r"D:\Design Builder\Xu ly data\DesignBuilder_DXF_to_IDF_Pipeline"
PROJECT_ID = "apartment_a_new"
MANIFEST_PATH = r"D:\Design Builder\Xu ly data\DesignBuilder_DXF_to_IDF_Pipeline\5_output\apartment_a_new\reports\idf_handoff_manifest.json"

if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

import apply_designbuilder_surface_adjacency_ui


def share_designbuilder_context(target_module):
    if "api_environment" in globals():
        target_module.api_environment = globals().get("api_environment")
    if "active_building" in globals():
        target_module.active_building = globals().get("active_building")


def before_energy_idf_generation():
    share_designbuilder_context(apply_designbuilder_surface_adjacency_ui)
    return apply_designbuilder_surface_adjacency_ui.run_surface_adjacency_ui_apply(
        workspace_root=WORKSPACE_ROOT,
        project_id=PROJECT_ID,
        manifest_path=MANIFEST_PATH,
        allow_write=True,
    )


before_energy_idf_generation()
