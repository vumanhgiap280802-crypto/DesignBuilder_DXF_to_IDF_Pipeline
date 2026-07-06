# DXF Case Setup

Rule nay dung cho moi case DXF moi ve sau, lay `noxh_apartment_a_clean` lam mau van hanh, nhung khong khoa cung ten file hay ten case.

## Muc tieu

- Input luon duoc scope theo `project_id`
- Shared library luon tach rieng trong `1_input/library/`
- Output luon duoc scope theo `project_id`
- Ten file duoc dieu khien bang token, khong copy-hardcode tu case cu
- Pipeline khong ghi de vao output shared cua case khac

## Contract bat buoc

1. `project_id`: lower_snake_case, vi du `sample_case`
2. `case_name`: human-readable title case, vi du `Sample Case`
3. `file_slug`: Title_Snake_Case, vi du `Sample_Case`
4. File CAD goc dat trong `1_input/<project_id>/raw/cad/`
5. File parser-readable DXF text ma pipeline doc dat trong `1_input/<project_id>/clean/txt_dxf/`
6. Moi output cua case phai nam duoi `5_output/<project_id>/`

## Lenh scaffold khuyen dung

Thay vi copy tay template, dung:

```powershell
python 3_scripts/tools/scaffold_dxf_case.py --project-id <project_id> --ceiling-height-m <height_m>
```

Lenh nay yeu cau nhap `--ceiling-height-m` de chieu cao zone do con nguoi xac nhan tu dau. Cac gia tri khac se tu sinh:

- `case_name` tu `project_id`, vi du `sample_case` -> `Sample Case`
- `file_slug` tu `project_id`, vi du `sample_case` -> `Sample_Case`
- `source_cad_filename` va `ready_text_filename` mac dinh la `<project_id>.dxf`
- `zone_output_prefix` mac dinh la `<PROJECT_ID>_`

Neu can override, co the dung them:

```powershell
python 3_scripts/tools/scaffold_dxf_case.py --project-id <project_id> --ceiling-height-m <height_m> --case-name "Sample Case" --file-slug Sample_Case --source-cad-filename sample_input.dxf --ready-text-filename sample_input.dxf --zone-output-prefix SAMPLE_
```

## Input layout

Bat buoc:

- `1_input/<project_id>/raw/cad/<source_cad_filename>`
- `1_input/<project_id>/clean/txt_dxf/<ready_text_filename>`

Tuy chon:

- `1_input/<project_id>/raw/txt_dxf/`
  Dung khi can giu text dump raw de doi chieu hoac fallback intake
- `1_input/<project_id>/raw/idf/`
  Dung khi case co IDF reference rieng; shared reference samples cua DesignBuilder nam trong `1_input/library/idf_import/reference/`

Rule quan trong:

- `paths.dxf_input` va `intake.source_ready_text_file` phai tro cung mot file trong `clean/txt_dxf/`
- `ready_text_filename` co the giu ten CAD goc, ten `.dxf`, hoac ten `.txt`; pipeline khong ep mot mau ten duy nhat

## Output layout

Case moi phai ghi vao:

- `5_output/<project_id>/normalized/dxf/`
- `5_output/<project_id>/intermediate/mapping/`
- `5_output/<project_id>/intermediate/geometry/`
- `5_output/<project_id>/intermediate/surfaces/`
- `5_output/<project_id>/intermediate/walls/`
- `5_output/<project_id>/intermediate/fenestration/`
- `5_output/<project_id>/csv/<file_slug>_idf_input_bundle/`
- `5_output/<project_id>/idf/<file_slug>_generated_from_bundle.idf`
- `5_output/<project_id>/reports/`

`5_output/_shared/` chi dung cho artifact dung chung, vi du sample IDF template.
Shared input runtime khong dat trong `2_config/`; noi do chi giu manifest `2_config/library_paths.json`.

## Template config

Template chung nam trong:

- `2_config/projects/_template_dxf_case/pipeline_case.template.json`
- `2_config/projects/_template_dxf_case/naming_rules.template.json`
- `2_config/projects/_template_dxf_case/geometry_policy.template.json`

Token can thay:

- `__PROJECT_ID__`
- `__CASE_NAME__`
- `__FILE_SLUG__`
- `__SOURCE_CAD_FILENAME__`
- `__READY_TEXT_FILENAME__`
- `__ZONE_OUTPUT_PREFIX__`
- `__CEILING_HEIGHT_M__`

## Cach tao case moi

1. Chay `python 3_scripts/tools/scaffold_dxf_case.py --project-id <project_id> --ceiling-height-m <height_m>`
2. Dat CAD goc vao `1_input/<project_id>/raw/cad/`
3. Dat ready DXF text vao `1_input/<project_id>/clean/txt_dxf/`
4. Kiem tra `room_anchor_patterns`, `title_anchor_patterns`, alias room code, va `zone_output_prefix`
5. Chay `python 3_scripts/pipeline/run_case_pipeline.py --project <project_id> --ceiling-height-m <height_m>`

## Khong duoc lam

- Khong tro output cua case moi vao `5_output/csv/`, `5_output/idf/`, `5_output/intermediate/`, hay root shared cua case cu
- Khong copy nguyen `noxh_apartment_a_clean` roi giu lai ten file cu trong config
- Khong dat input operational cua case moi vao `1_input/raw/` neu file do la ban cleaned/ready de parser doc
