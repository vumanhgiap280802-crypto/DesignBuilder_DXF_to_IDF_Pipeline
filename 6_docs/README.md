# DesignBuilder_DXF_to_IDF_Pipeline

Workspace nay da chuyen sang layout theo `project_id` cho ca input va output.
Moi pipeline run phai duoc scope theo mot project cu the, tranh tron file giua cac project.

## Pham vi repo GitHub

Repo nay track code, config, schema, docs, shared input library, va sample `apartment_a_new`.
Input/output cua cac case khac khong duoc upload de tranh day du lieu nang hoac chua chot len Git.

Sample Apartment A duoc giu tai:

- `1_input/apartment_a_new/`
- `1_input/Envelope_apartment_A/`
- `5_output/apartment_a_new/`

## Layout hien tai

Input:

```text
1_input/
  library/
    idf_import/
      catalogs/
      objects/
      legacy/
      reference/
    shared/
  <project_id>/
    raw/
      cad/
      txt_dxf/          # tuy chon
      idf/              # tuy chon, neu project co IDF reference rieng
    clean/
      txt_dxf/
    project.json
```

Output:

```text
5_output/<project_id>/
  csv/
  idf/
  intermediate/
  normalized/
  reports/
  packages/              # tuy chon
```

Shared output:

```text
5_output/_shared/
```

Chi dat artifact thuc su dung chung cho nhieu project vao `_shared`, vi du sample template hoac global reference artifact.
Moi artifact gan voi mot project phai nam duoi `5_output/<project_id>/...`.

Workspace-level reports:

```text
5_output/report/
```

Chi dat report cap workspace hoac cross-project vao `5_output/report/`.
Report gan voi mot project cu the van phai nam trong `5_output/<project_id>/reports/`.

Shared input library dat trong `1_input/library/`.
`2_config/library_paths.json` la manifest chot duong dan runtime toi catalog, object CSV, va shared reference input.

## Them mot project moi

Lenh scaffold khuyen dung:

```powershell
python 3_scripts/tools/scaffold_dxf_case.py --project-id <project_id> --ceiling-height-m <height_m>
```

Lenh nay se:

- tao `2_config/projects/<project_id>/` tu template
- tao `1_input/<project_id>/raw/cad/`
- tao `1_input/<project_id>/clean/txt_dxf/`
- tu sinh `case_name`, `file_slug`, ten file input mac dinh, va `zone_output_prefix` tu `project_id`

Neu muon dat project moi lam mac dinh ngay:

```powershell
python 3_scripts/tools/scaffold_dxf_case.py --project-id <project_id> --ceiling-height-m <height_m> --set-default
```

Sau scaffold:

1. Dat CAD goc vao `1_input/<project_id>/raw/cad/`.
2. Dat parser-readable DXF text vao `1_input/<project_id>/clean/txt_dxf/`.
3. Neu can project metadata rieng, dat them `1_input/<project_id>/project.json`.
4. Chinh lai `naming_rules.json` va `geometry_policy.json` neu case moi khong trung quy uoc voi template.

Huong dan chi tiet nam trong `6_docs/dxf_case_setup.md`.

## Chay pipeline cho mot project

Lenh chinh:

```powershell
python 3_scripts/pipeline/run_case_pipeline.py --project <project_id> --ceiling-height-m <height_m>
```

Neu bo qua `--project`, workspace se doc `default_project` tu `2_config/default_project.json`.
`--ceiling-height-m` la bat buoc khi dung hinh/IDF.
Neu ca hai deu thieu, script se fail voi thong bao ro rang.

## Migration tu layout cu

Dry-run:

```powershell
python 3_scripts/tools/migrate_project_layout.py --project <project_id> --mode copy --dry-run
```

Copy du lieu sang layout moi:

```powershell
python 3_scripts/tools/migrate_project_layout.py --project <project_id> --mode copy
```

Move du lieu sang layout moi:

```powershell
python 3_scripts/tools/migrate_project_layout.py --project <project_id> --mode move
```

Nguyen tac migration:

- Script chi map cac path legacy co the xac dinh project mot cach deterministic.
- Artifact legacy khong map chac chan duoc se vao `migration_review.json` de review tay.
- Review report duoc ghi vao `5_output/<project_id>/reports/migration_review.json`.

## Fallback legacy tam thoi

Resolver uu tien layout moi:

- `1_input/<project_id>/...`
- `1_input/library/...` cho shared library/reference
- `5_output/<project_id>/...`

Neu khong tim thay, code co the fallback sang layout cu trong mot so truong hop compatibility va se phat `DeprecationWarning` voi thong diep:

- `deprecated input layout`
- `deprecated output layout`

Moi write moi chi duoc ghi vao layout theo project.

## Ghi chu ve `txt_dxf`

Ten thu muc `txt (dxf)` da duoc doi thanh `txt_dxf`.
Code va config moi phai dung `txt_dxf`.
`txt (dxf)` chi con duoc giu lai trong migration va fallback compatibility.
