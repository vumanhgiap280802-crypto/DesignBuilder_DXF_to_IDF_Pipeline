# Architecture

## Scope

Tai lieu nay mo ta pipeline hien tai sau khi workspace chuyen sang layout theo `project_id`.
Source of truth cho path contract nam trong `3_scripts/utils/path_resolver.py` va `2_config/projects/<project_id>/pipeline_case.json`.

## Entry Point

- Entry point cap workspace la `3_scripts/pipeline/run_case_pipeline.py`.
- Cach chay chinh: `python 3_scripts/pipeline/run_case_pipeline.py --project <project_id> --ceiling-height-m <height_m>`.
- Neu khong truyen `--project`, script se doc `default_project` tu `2_config/default_project.json`.
- `--ceiling-height-m` la bat buoc khi dung hinh/IDF de chieu cao zone do con nguoi cung cap khi chay.
- `3_scripts/pipeline/apartment_a_pipeline.py::run_pipeline(...)` van la implementation tham chieu cho flow chinh.

## Main Flow

Luong chinh cua workspace:

`DXF -> mapping -> geometry -> surfaces -> walls -> fenestration -> CSV bundle -> rebuilt IDF`

Theo layout moi, luong nay duoc scope theo mot project:

1. Doc DXF text tu `1_input/<project_id>/clean/txt_dxf/` va fallback sang `1_input/<project_id>/raw/txt_dxf/` neu can.
2. Parse va chuan hoa DXF bang `parsers/dxf_raw_parser.py`, ghi artifact vao `5_output/<project_id>/normalized/dxf/`.
3. Tao filtered extract va schema JSON cho project.
4. Build mapping artifacts trong `5_output/<project_id>/intermediate/mapping/`.
5. Suy ra geometry trong `5_output/<project_id>/intermediate/geometry/`.
6. Tao surface artifacts trong `5_output/<project_id>/intermediate/surfaces/`.
7. Resolve wall artifacts trong `5_output/<project_id>/intermediate/walls/`.
8. Tao fenestration artifacts trong `5_output/<project_id>/intermediate/fenestration/`.
9. Lap CSV bundle trong `5_output/<project_id>/csv/`.
10. Rebuild IDF cuoi cung vao `5_output/<project_id>/idf/`.

## Input Chinh

- `1_input/<project_id>/raw/cad/`
- `1_input/<project_id>/raw/idf/`
- `1_input/<project_id>/raw/txt_dxf/`
- `1_input/<project_id>/clean/csv/`
- `1_input/<project_id>/clean/idf/`
- `1_input/<project_id>/clean/txt_dxf/`
- `2_config/projects/<project_id>/pipeline_case.json`
- `2_config/projects/<project_id>/naming_rules.json`
- `2_config/projects/<project_id>/geometry_policy.json`

## Output Chinh

- Normalized DXF: `5_output/<project_id>/normalized/dxf/`
- Mapping artifacts: `5_output/<project_id>/intermediate/mapping/`
- Geometry artifacts: `5_output/<project_id>/intermediate/geometry/`
- Surface artifacts: `5_output/<project_id>/intermediate/surfaces/`
- Wall artifacts: `5_output/<project_id>/intermediate/walls/`
- Fenestration artifacts: `5_output/<project_id>/intermediate/fenestration/`
- CSV bundle: `5_output/<project_id>/csv/<bundle_name>/`
- Rebuilt IDF: `5_output/<project_id>/idf/<file>.idf`
- Reports: `5_output/<project_id>/reports/`

Chi nhung artifact dung chung cho nhieu project moi nam trong `5_output/_shared/`.

## Writers

### `bundle_writer.py`

- La bundle assembler.
- Dau vao la mapping, geometry, surface, wall, fenestration, va opening-host artifacts cua mot project.
- Dau ra chinh la bundle CSV duoi `5_output/<project_id>/csv/` va intermediate bundle artifacts duoi `5_output/<project_id>/intermediate/`.

### `rebuild_idf_from_bundle.py`

- La final writer.
- Dau vao la CSV bundle cua mot project.
- Dau ra la file `.idf` cuoi cung trong `5_output/<project_id>/idf/`.

## Legacy Compatibility

- Input legacy `1_input/raw/...`, `1_input/clean/...` van duoc fallback tam thoi neu project layout chua du.
- Output legacy `5_output/normalized/...`, `5_output/intermediate/...`, `5_output/csv/...`, `5_output/idf/...`, `5_output/reports/...`, va `5_output/projects/...` van duoc read fallback tam thoi.
- Moi fallback deu phat `DeprecationWarning`.
- Moi write moi chi duoc ghi vao `1_input/<project_id>/...` hoac `5_output/<project_id>/...`.

## XML / dsbXML / gbXML

- Khong co code nao trong pipeline chinh dung XML/dsbXML/gbXML lam main path orchestration.
- Main path van la `DXF -> intermediate artifacts -> CSV bundle -> rebuilt IDF`.
