# Naming Standard

Workspace source of truth cho S03 la code hien tai trong `run_case_pipeline.py`, `apartment_a_pipeline.py`, `mapping_builder.py`, `geometry_inference.py`, `surface_builder.py`, `wall_logic.py`, `fenestration_builder.py`, `bundle_writer.py`, va config `2_config/cases.json` + `2_config/case_defaults.json`.

## 1. Case Naming

- `case_id`: `noxh_apartment_a_clean`
  Quy uoc la lower_snake_case. Day la khoa may-doc duoc dung trong `2_config/cases.json` va project scoping.
- `case_name`: `NOXH Apartment A Clean DXF`
  Quy uoc la human-readable title case.
- `file_slug`: `NOXH_Apartment_A_clean`
  Quy uoc hien tai la Title_Snake_Case. Workspace dang dung slug nay cho output file/directory chinh:
  - `NOXH_Apartment_A_clean_filtered_extract.txt`
  - `NOXH_Apartment_A_clean_filtered_extract_schema.json`
  - `NOXH_Apartment_A_clean_idf_input_bundle/`
  - `NOXH_Apartment_A_clean_generated_from_bundle.idf`

## 2. Canonical Room Code

Pipeline hien tai khong luu field `room_code` rieng. Canonical room code thuc te la `zone_key`.

Canonicalization rule duoc khoa theo code:

1. Lay `zone_name` goc, neu co dau `:` thi bo phan truoc dau `:`.
2. ASCII-normalize.
3. Uppercase.
4. Thay `+` bang `_`.
5. Collapse moi ky tu khong phai chu/so thanh `_`, roi collapse `_` lien tiep.
6. Alias:
   - `PKXPB` -> `PK_PB`
   - `LOGIA` -> `LOGIA`
7. Chuan hoa phong `PN01`, `PN 01`, `PN_01` thanh `PN_01`.
8. Chuan hoa phong `WC01`, `WC 01`, `WC_01` thanh `WC_01`.

Current canonical room codes trong workspace:

- `PK_PB`
- `PN_01`
- `PN_02`
- `WC_01`
- `WC_02`
- `LOGIA`

Neu hai room label khac nhau canonicalize ve cung mot room code, pipeline phai fail thay vi suffix them `_2`. Quy tac nay duoc dat ra de tranh hai object khac nhau dung cung mot ID logic.

## 3. Zone ID

- `zone_id` trong pipeline hien tai la `zone_output_name_by_key[zone_key]`
- Pattern chot: `APARTMENT_A_<canonical_room_code>`

Vi du:

- `APARTMENT_A_PN_01`
- `APARTMENT_A_WC_02`
- `APARTMENT_A_LOGIA`

`floor_id` hien chua ton tai thanh object rieng. Convention du phong neu can bo sung sau nay:

- `FLOOR_<seq:02d>` cho tang/toa do tong quat
- khong dua vao pipeline hien tai neu workspace chua su dung

## 4. Surface, Wall, Opening, Fenestration IDs

### Surface Names

`surface_builder.py` dang sinh ten surface tu `zone_id`:

- Floor: `{zone_id}_FLOOR_{seq:02d}`
- Roof: `{zone_id}_ROOF_{seq:02d}`
- Wall: `{zone_id}_WALL_{seq:02d}`

`seq` duoc dem rieng theo tung zone, nen uniqueness dua tren cap `(zone_id, surface_type, seq)`.

### Physical Wall ID

`wall_logic.py` dang sinh inventory wall ID:

- `WALL_{seq:03d}`

Seq duoc cap sau khi sort theo `surface_name` va dedupe interzone pair, nen on dinh theo current pipeline.

### Opening ID

`mapping_builder.py` dang sinh:

- `OPENING_{seq:03d}`

Seq duoc cap theo thu tu `opening_groups` sau khi group/sort opening annotations. Trong current workspace, opening annotations deu co `annotation_owner_handle`, nen opening ID dang on dinh theo handle da sort.

### Fenestration Name

`fenestration_builder.py` dang sinh:

- `{zone_output_prefix_no_trailing_underscore}_{opening_id}_{surface_type_token}`
- neu la cap interzone thi them suffix `_ADJ` cho mat doi dien

Vi du:

- `APARTMENT_A_OPENING_003_DOOR`
- `APARTMENT_A_OPENING_003_DOOR_ADJ`
- `APARTMENT_A_OPENING_010_GLASSDOOR`

## 5. File Naming

Output chinh cua case `noxh_apartment_a_clean` phai bam `file_slug = NOXH_Apartment_A_clean`:

- Normalized extract: `5_output/noxh_apartment_a_clean/normalized/dxf/NOXH_Apartment_A_clean_filtered_extract.txt`
- Schema: `5_output/noxh_apartment_a_clean/normalized/dxf/NOXH_Apartment_A_clean_filtered_extract_schema.json`
- CSV bundle dir: `5_output/noxh_apartment_a_clean/csv/NOXH_Apartment_A_clean_idf_input_bundle/`
- Rebuilt IDF: `5_output/noxh_apartment_a_clean/idf/NOXH_Apartment_A_clean_generated_from_bundle.idf`

Input raw co the giu ten goc cua nguon, vi du `Apartment A dxf.txt`. Day khong phai naming contract cua output pipeline.

## 6. S03 Decisions

- `case_id`, `case_name`, `file_slug` duoc khoa ro rang.
- `room_code` canonical trong workspace duoc dinh nghia la `zone_key`.
- `zone_id` phai duoc sinh tu prefix + canonical room code, khong duoc dua truc tiep vao raw text token.
- Khong dung suffix `_2`, `_3` de chua chay collision room code. Collision canonical room code phai duoc xem la loi du lieu/rule va fail som.
- Surface/wall/opening/fenestration naming giu nguyen pipeline hien tai, chi dong bo canonical zone naming de tranh `PN01` va `PN_01` tao ra hai variant ID khac nhau.

## 7. Multi-Case Intake Naming

S04 khong sua baseline `apartment_a`; no chi them contract cho case moi.

Case moi phai theo 3 token chinh:

- `case_id`: lower_snake_case, vi du `sample_case`
- `case_name`: title case, vi du `Apartment A S04`
- `file_slug`: Title_Snake_Case, vi du `Apartment_A_S04`

Contract file input cho case moi:

- Raw CAD uu tien ten `{file_slug}.dwg` hoac `{file_slug}.dxf` neu co the dat ten lai
- Raw parser-readable text neu co: `{file_slug}_dxf_raw.txt`
- Ready parser-readable text duoc pipeline doc co the la `{file_slug}.dxf`, `{file_slug}.txt`, hoac ten goc tu CAD

Path contract cho intake:

- `1_input/<case_id>/raw/cad/`
- `1_input/<case_id>/raw/txt_dxf/` (tuy chon)
- `1_input/<case_id>/clean/txt_dxf/`

Operational rule:

- `dxf_filename` hoac cap `raw_cad_filename`/`ready_text_filename` trong `2_config/cases.json` phai tro dung file trong layout input
- Raw CAD giu vai tro archive nguon, khong phai parser input truc tiep
- `raw/txt_dxf/` chi la nhanh fallback/doi chieu, khong phai operational input bat buoc

## 8. Multi-Case Output Naming

Case moi khong duoc ghi vao output shared cua baseline. Output root chuan la:

- `5_output/<case_id>/normalized/`
- `5_output/<case_id>/intermediate/`
- `5_output/<case_id>/csv/`
- `5_output/<case_id>/idf/`
- `5_output/<case_id>/reports/`

Contract ten file output cho case moi van bam `file_slug`:

- Extract: `{file_slug}_filtered_extract.txt`
- Extract schema: `{file_slug}_filtered_extract_schema.json`
- Bundle dir: `{file_slug}_idf_input_bundle/`
- Rebuilt IDF: `{file_slug}_generated_from_bundle.idf`

Artifact writer trung gian van giu ten file noi bo hien tai, nhung phai nam duoi `5_output/<case_id>/intermediate/` de tranh ghi de case cu.

## 9. Operational Input Standard

Theo `dxf_raw_parser.py`, parser hien tai doc file text chua cac cap `group-code/value` cua DXF. No khong doc truc tiep file `.dwg` binary.

Operational standard cho multi-case vi vay la:

1. Luu CAD goc vao `1_input/<case_id>/raw/cad/`
2. Neu can giu text dump raw de doi chieu, dat vao `1_input/<case_id>/raw/txt_dxf/`
3. Dat ban parser-readable text da chot de pipeline doc vao `1_input/<case_id>/clean/txt_dxf/`
4. Khai bao `dxf_filename` hoac `ready_text_filename` trong `2_config/cases.json` cho ban trong `clean/txt_dxf/`
5. Khong khoa cung ten file; ten input hop le neu ca config va file thuc te khop nhau
