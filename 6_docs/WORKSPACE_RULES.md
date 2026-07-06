# WORKSPACE_RULES.md

## Muc dich

File nay quy dinh vi tri luu file trong `DesignBuilder_DXF_to_IDF_Pipeline` sau khi da chuan hoa ten thu muc thanh:

- `1_input`
- `2_config`
- `3_scripts`
- `4_schemas`
- `5_output`
- `6_docs`
- `7_archive`

Muc tieu la tranh tron lan input, output, tai lieu, script va file luu tru.

## Cap Nhat Layout Theo Project

Operational contract hien tai cua workspace la layout theo `project_id`.
Path chuan hien tai la:

- `1_input/library/...`
- `1_input/<project_id>/raw/...`
- `1_input/<project_id>/clean/...`
- `5_output/<project_id>/normalized/...`
- `5_output/<project_id>/intermediate/...`
- `5_output/<project_id>/csv/...`
- `5_output/<project_id>/idf/...`
- `5_output/<project_id>/reports/...`
- `5_output/_shared/...`
- `5_output/report/...`

Quy uoc moi:

- `txt (dxf)` duoc doi ten thanh `txt_dxf`
- `5_output/projects/<case_id>/...` khong con la layout chinh; layout chinh la `5_output/<project_id>/...`
- `5_output/_shared/` chi dung cho artifact thuc su dung chung giua nhieu project
- `5_output/report/` chi dung cho report cap workspace hoac cross-project; report gan voi mot project cu the van phai vao `5_output/<project_id>/reports/`
- Template case DXF moi duoc giu trong `2_config/projects/_template_dxf_case/` va huong dan setup nam trong `6_docs/dxf_case_setup.md`
- Legacy layout chi con duoc doc theo che do fallback tam thoi va phai phat canh bao deprecated

---

## Dong Bo Rule Va Config

File nguon chuan cua rule la:

- `6_docs/WORKSPACE_RULES.md`

File config may doc duoc duoc sinh ra tu file rule la:

- `2_config/workspace_rules.json`

Quy uoc dong bo:

1. Khong sua truc tiep `2_config/workspace_rules.json`.
2. Khi sua rule, sua trong `6_docs/WORKSPACE_RULES.md`.
3. Sau khi sua rule, chay:
   - `python 3_scripts/workspace_rules/sync_workspace_rules.py`
4. Neu can tu dong cap nhat trong luc dang sua, chay:
   - `python 3_scripts/workspace_rules/sync_workspace_rules.py --watch`

---

## Rule Bat Buoc Cho Script Python Moi

Neu muon cac script Python moi deu ap dung rule, thi quy trinh bat buoc la:

1. Tao script moi bang:
   - `python 3_scripts/workspace_rules/create_python_script.py ten_script.py`
   - hoac chi ro nhom script bang `--subdir`, vi du: `python 3_scripts/workspace_rules/create_python_script.py join_step.py --subdir context`
   - voi script bien doi du lieu trung gian cap cao, dung `--subdir transformers`
2. Khong tao Python script moi bang tay neu chua dua `WorkspaceGuard` vao script.
3. Moi script Python trong `3_scripts/` phai:
   - import `WorkspaceGuard` tu `3_scripts/workspace_rules/workspace_guard.py`
   - khoi tao guard ngay dau file
   - dung guard de kiem tra moi duong dan doc/ghi
4. Logic nghiep vu chinh cua script moi chi duoc dat sau khi phan bootstrap workspace rule da xong:
   - xu ly import rule/bootstrap truoc
   - khoi tao `WorkspaceGuard` truoc
   - sau do moi viet ham/chuc nang nghiep vu chinh
5. Moi thao tac ghi file phai di qua rule check, dac biet la:
   - khong ghi vao `1_input/raw/`
   - chi duoc ghi vao `1_input/clean/` khi dang tao cleaned input duoc workflow yeu cau ro rang va van dong vai tro input cho buoc sau
   - `1_input/library/` chi duoc cap nhat khi dang quan ly shared input library hoac reference input dung chung
   - artifact parser-generated de chuan hoa raw phai ghi vao `5_output/<project_id>/normalized/`
   - mapping, join context, manifest, wall inventory, geometry snapshot, va artifact trung gian cua pipeline phai ghi vao `5_output/<project_id>/intermediate/`
   - bundle CSV phai ghi vao `5_output/<project_id>/csv/`, IDF sau xu ly phai ghi vao `5_output/<project_id>/idf/`, report phai ghi vao `5_output/<project_id>/reports/`
   - khong tao file moi neu chua cho phep ro rang
6. Kiem tra compliance cua Python scripts bang:
   - `python 3_scripts/workspace_rules/validate_script_compliance.py`

---

## Rule Tong

Nguyen tac chung:

`Input goc vao 1_input, cau hinh vao 2_config, ma chay vao 3_scripts, schema vao 4_schemas, ket qua vao 5_output, tai lieu vao 6_docs, va moi ban cu/backup vao 7_archive.`

Ba cam ket bat buoc:

1. Khong ghi de file nguon trong `1_input/raw/`.
2. `1_input/clean/` chi dung cho ban da lam sach, trich loc, hoac staging da duoc xem la input cho buoc sau; artifact parser-generated de chuan hoa raw phai vao `5_output/<project_id>/normalized/`.
3. `1_input/library/` la shared input library cho catalog, object CSV, va reference input dung chung; khong duoc dung no de dat output final.
4. Khong luu file output final vao `1_input`, `2_config`, `3_scripts`, hoac root workspace.

---

## Quy Tac Theo Thu Muc

### 1. `1_input/`

Chi dung de luu du lieu dau vao cho pipeline, gom ca ban raw bat bien va ban clean da duoc xac nhan se dong vai tro input.

Duoc phep luu:

- File CAD goc `.dwg`, `.dxf`, hoac export CAD tu ben ngoai
- File `.idf` dau vao
- File text dump tu AutoCAD/DXF
- Ban da lam sach, trich loc, hoac staging duoc xac nhan se dung tiep lam input
- Tai lieu tham chieu dau vao neu thuoc nghiep vu du lieu

Khong duoc phep luu:

- File output final
- Report phan tich
- File crosscheck
- Artifact parser-generated normalization, mapping, va file tam khong dong vai tro input

Quy uoc project-scoped va shared library:

- `1_input/<project_id>/raw/`: chua du lieu goc nhan tu ben ngoai cua rieng project do, khong sua tay, khong ghi de
- `1_input/<project_id>/clean/`: chua ban da lam sach, trich loc, hoac staging tu `raw/` nhung van duoc xem la input cho buoc sau
- `1_input/library/`: shared input library dung cho catalog, object CSV, sample/reference IDF, va shared default inputs khong gan rieng mot project
- Khong duoc dat report, package, artifact final, hoac parser output normalization vao `1_input/<project_id>/clean/` hoac `1_input/library/`
- Script chi duoc tao/cap nhat file trong `1_input/<project_id>/clean/` hoac `1_input/library/` khi user yeu cau ro rang hoac workflow quy dinh buoc tao cleaned/shared input
- Parser output dung de chuan hoa raw file phai dat trong `5_output/<project_id>/normalized/`
- Intake DXF cho case moi uu tien 2 diem neo ro rang: `raw/cad/` cho CAD goc va `clean/txt_dxf/` cho parser-readable text ma pipeline doc
- `pipeline_case.json` cua case moi phai tro `intake.source_ready_text_file` va `paths.dxf_input` vao file trong `1_input/<project_id>/clean/txt_dxf/`
- `1_input/<project_id>/raw/txt_dxf/` chi la nhanh tuy chon de giu text dump raw hoac fallback intake, khong phai duong operational bat buoc

Quy uoc con:

- `1_input/<project_id>/raw/cad/`: file CAD goc `.dwg`, `.dxf`, hoac format CAD raw nhan tu ben ngoai cua mot project
- `1_input/<project_id>/raw/idf/`: file IDF goc rieng cua project neu co
- `1_input/<project_id>/raw/txt_dxf/`: text dump hoac DXF dang text phuc vu doi chieu hoac fallback intake
- `1_input/<project_id>/clean/txt_dxf/`: operational parser input root cho DXF text da duoc chot de pipeline doc
- `1_input/library/idf_import/catalogs/`: catalog chuan cho material, construction, va resolver rules
- `1_input/library/idf_import/objects/`: CSV tach theo EnergyPlus object de preload bundle/import data
- `1_input/library/idf_import/legacy/`: input CSV legacy duoc giu lai de fallback co kiem soat
- `1_input/library/idf_import/reference/`: IDF/XML sample va reference import cho DesignBuilder
- `1_input/library/shared/`: shared default inputs khac neu co

---

### 2. `2_config/`

Chi dung de luu file cau hinh va quy tac.

Duoc phep luu:

- File `.json`, `.yaml`, `.yml`, `.toml`, `.env.example`
- Rule phan loai
- Mapping rule
- Cau hinh path, logging, import, prune

Khong duoc phep luu:

- Script chay
- Output report
- Input du lieu thuc te

Quy uoc con:

- `2_config/projects/`: config theo project/case
- `2_config/projects/<case_id>/`: case config, naming rules, geometry policy, va config rieng theo project

Nguyen tac bat buoc cho Apartment A geometry policy:

- `2_config/apartment_a_geometry_policy.json` la nguon config tich luy chinh thuc cho cac thong tin hinh hoc, kich thuoc, target area, rule partition, va cac du lieu da duoc xac nhan dung trong qua trinh tao IDF Apartment A.
- Moi buoc downstream lien quan toi geometry, partition, surface generation, wall resolution, fenestration preparation, hoac bundle build cua Apartment A phai uu tien doc file config nay truoc khi dung hardcode.
- Neu refactor, QA, crosscheck, rebuild, hoac pipeline validation xac nhan duoc thong tin moi on dinh hon va dung hon, thong tin do phai duoc dua nguoc ve `2_config/apartment_a_geometry_policy.json` theo cau truc policy phu hop.
- Khong de thong tin da duoc confirm tiep tuc nam rai rac trong code, report tam, hoac output trung gian ma khong cap nhat vao config chinh thuc.
- Muc tieu la bien `2_config/apartment_a_geometry_policy.json` thanh shared source of truth cho toan bo cac buoc dung IDF Apartment A ve sau.

---

### 3. `3_scripts/`

Chi dung de luu ma thuc thi.

Duoc phep luu:

- Script Python
- Script shell
- Script PowerShell
- Utility script phan tich, prune, compare, extract, test

Khong duoc phep luu:

- File output sinh ra khi chay
- JSON report ket qua
- IDF ket qua
- File du lieu dau vao

Luu y:

- Script co the doc tu `1_input/`, `2_config/`, `4_schemas/`
- `3_scripts/parsers/`: chua raw parser va extractor o muc parser, chi tap trung doc/parse/chuan hoa du lieu goc
- `3_scripts/context/`: chua script xay artifact trung gian da co ngu canh, nam sau parser va truoc cac xu ly nghiep vu sau hon nhu mapping va cac buoc transformer
- `3_scripts/transformers/`: chua script bien doi du lieu trung gian da parse/context hoa thanh mot dang du lieu trung gian co cau truc cao hon, vi du geometry inference, surface generation, wall resolution, fenestration preparation, va cac phep bien doi nghiep vu tuong tu; nhom nay khong doc raw input truc tiep va khong ghi output final CSV/IDF
- Artifact transformer cho geometry, surfaces, walls, va fenestration prep phai nam trong `5_output/<project_id>/intermediate/` theo nhom ro rang, vi du `5_output/<project_id>/intermediate/geometry/`, `5_output/<project_id>/intermediate/surfaces/`, hoac `5_output/<project_id>/intermediate/walls/`
- `3_scripts/pipeline/`: chua script dieu phoi quy trinh chay nhieu buoc lien tiep
- Script entrypoint cap workspace/pipeline co the nam truc tiep duoi `3_scripts/` neu do la buoc chinh duoc downstream goi truc tiep
- Script co the ghi vao `1_input/<project_id>/clean/` neu dang tao cleaned input hoac staging input theo dung rule workspace
- Script co the ghi vao `1_input/library/` khi dang cap nhat shared input library theo yeu cau workspace/user
- Parser-generated normalization phai ghi vao `5_output/<project_id>/normalized/`
- Mapping, join context, geometry snapshot, manifest, wall inventory, va artifact trung gian phai ghi vao `5_output/<project_id>/intermediate/`
- Legacy shared output root chi duoc doc theo fallback; moi write moi phai vao `5_output/<project_id>/...`
- Script chi duoc ghi ket qua final vao `5_output/` hoac `7_archive/` khi can backup

---

### 4. `4_schemas/`

Chi dung de luu schema mo ta cau truc du lieu.

Duoc phep luu:

- JSON schema
- CSV schema
- IDF schema
- Tai lieu mo ta field-level structure

Quy uoc con:

- `4_schemas/json/`: schema JSON
- `4_schemas/csv/`: schema CSV
- `4_schemas/idf/`: schema lien quan IDF

Khong duoc phep luu:

- Report tong hop
- File output final
- Input goc

---

### 5. `5_output/`

Day la noi mac dinh de luu ket qua xu ly final, report, artifact normalized, artifact trung gian, va bundle pipeline.

Duoc phep luu:

- IDF da prune
- IDF crosscheck
- Du lieu normalized sinh ra tu parser
- Mapping, join context, geometry snapshot, manifest, inventory, va artifact trung gian cua pipeline
- Report Markdown
- Report JSON
- Bundle CSV sinh ra tu pipeline
- Goi dong goi ket qua ban giao

Quy uoc con:

- `5_output/<project_id>/normalized/dxf/`: artifact normalized muc raw cho DXF
- `5_output/<project_id>/normalized/dxf/csv/`: bang CSV relational duoc materialize tu normalized DXF extract khi can
- `5_output/<project_id>/intermediate/mapping/`: mapping artifacts
- `5_output/<project_id>/intermediate/geometry/`: geometry artifacts
- `5_output/<project_id>/intermediate/surfaces/`: surface artifacts
- `5_output/<project_id>/intermediate/walls/`: artifact wall resolution trung gian nhu `wall_inventory.json` va `wall_resolution.json`
- `5_output/<project_id>/intermediate/fenestration/`: fenestration artifacts
- `5_output/<project_id>/csv/`: chi chua bundle CSV cua project
- `5_output/<project_id>/idf/`: rebuilt IDF cua project
- `5_output/<project_id>/reports/`: report `.md`, `.json` cua project
- `5_output/<project_id>/packages/`: goi dong goi theo project neu co
- `5_output/_shared/idf/`: sample IDF template hoac artifact tham chieu dung chung
- `5_output/report/`: report cap workspace, report tong hop nhieu project, audit/compliance report, hoac report khong gan rieng voi mot `project_id`

Luu y:

- Moi case moi sau buoc chuan bi intake phai mac dinh ghi vao `5_output/<project_id>/...`
- `5_output/_shared/` chi duoc dung cho artifact dung chung, khong phai noi dat ket qua rieng cua tung case
- `5_output/report/` khong thay the `5_output/<project_id>/reports/`; no chi la noi cho report tong hop cap workspace hoac cross-project
- Legacy root nhu `5_output/normalized/`, `5_output/intermediate/`, `5_output/csv/`, `5_output/idf/`, va `5_output/reports/` chi con duoc giu de read fallback tam thoi

Khong duoc phep luu:

- Script
- Config
- Ban backup lich su

---

### 6. `6_docs/`

Chi dung de luu tai lieu doc cho nguoi dung.

Duoc phep luu:

- README
- Workflow guide
- Huong dan import DesignBuilder
- Rule workspace
- Ghi chu nghiep vu va huong dan thao tac

Khong duoc phep luu:

- Output report sinh tu dong
- Input goc
- Script chay

Nguyen tac:

- Tai lieu mo ta he thong luu trong `6_docs/`
- Ket qua do script sinh ra luu trong `5_output/<project_id>/reports/`

---

### 7. `7_archive/`

Chi dung de luu file cu, file backup, file bi loai.

Duoc phep luu:

- File backup truoc khi ghi de
- File legacy con can giu lai
- File rejected
- File tam can cat khoi luong xu ly chinh nhung chua xoa

Quy uoc con:

- `7_archive/backup/`: backup truoc overwrite
- `7_archive/legacy/`: file cu, file tham khao lich su
- `7_archive/rejected/`: file fail validation hoac khong duoc dung

Luu y:

- Backup nen duoc gom theo timestamp hoac ly do ro rang, vi du `7_archive/backup/20260427_153700_before_overwrite/`
- Khi backup ca mot file quan trong, nen giu lai duong dan goc tuong doi trong workspace de de restore
- Neu backup gan voi mot project, ten file hoac thu muc backup nen chua `project_id`
- Khong dung `7_archive/backup/` lam noi luu output final dang active; sau khi restore/can dung tiep thi dua file ve dung thu muc chuc nang

Khong duoc phep luu:

- Output final dang su dung
- Config dang active

---

## Rule Intake DXF Cho Multi-Case

Phan nay ap dung cho cac case DXF moi duoc them sau baseline `apartment_a`.

Operational standard:

1. Pipeline hien tai doc file DXF o dang parser-readable text gom cac cap `group-code/value`; day la input van hanh chinh thuc cho `paths.dxf_input`.
2. File CAD goc `.dwg`/`.dxf` nhan tu ben ngoai phai duoc luu trong `1_input/<case_id>/raw/cad/`.
3. Ban parser-readable text ma pipeline doc phai luu trong `1_input/<case_id>/clean/txt_dxf/`.
4. Neu can giu text dump raw de doi chieu hoac fallback intake, luu trong `1_input/<case_id>/raw/txt_dxf/`.
5. `pipeline_case.json` cua case moi phai tro ca `intake.source_ready_text_file` va `paths.dxf_input` vao cung mot file trong `clean/txt_dxf/`.
6. Neu fail gate intake, du lieu raw/review cua case do phai duoc dua ra `7_archive/rejected/<case_id>/` hoac cat khoi luong xu ly chinh truoc khi tiep tuc.

### 1. Naming va Path Contract Cho Case Moi

1. `case_id` phai la lower_snake_case, vi du `sample_case`.
2. `case_name` phai la human-readable title case, vi du `Sample Case`.
3. `file_slug` phai la Title_Snake_Case, vi du `Sample_Case`.
4. File CAD goc nen uu tien ten theo mau `{file_slug}.dwg` hoac `{file_slug}.dxf` neu co quyen dat ten lai; neu can giu ten goc tu ben ngoai thi van luu trong `1_input/<case_id>/raw/cad/` nhung phai ro mapping voi `case_id`.
5. Raw parser-readable text neu co nen dat ten theo `file_slug` de de doi chieu, vi du `{file_slug}_dxf_raw.txt`; tuy nhien day la nhanh tuy chon.
6. Ready text duoc pipeline doc co the giu ten `{file_slug}.dxf`, `{file_slug}.txt`, hoac ten goc tu CAD, mien la `intake.source_ready_text_file`, `paths.dxf_input`, va `naming_rules.primary_input_patterns.ready_text_input` cung tro dung mot file.
7. Output case moi phai tro vao `5_output/<case_id>/` va mirror day du `normalized/`, `intermediate/`, `csv/`, `idf/`, `reports/`.
8. Khong duoc tro output case moi vao output shared cua baseline neu khong co ly do ro rang.
9. Template bat dau cho case moi phai copy tu `2_config/projects/_template_dxf_case/`, khong copy-cung config cua case dang active.

### 2. Checklist S04 Readiness Cho DXF Dau Vao

Layer bat buoc:

- Phai co geometry nam tren it nhat mot layer thuoc keep-list hien tai cua pipeline: `TAC - Tuong`, `TAC - Lop hoan thien`, `TAC - Door window`, `TAC - CUA+LC`, `TAC - Betong`, `TAC - Thay`, hoac nested block geometry tren layer `0`
- Phai co it nhat mot nhom text anchor phu hop voi `room_anchor_patterns` hoac `title_anchor_patterns` cua `naming_rules.json` cua case
- Neu case can opening/door/window downstream, phai co annotation hoac insert hop le tren `TAC - CUA+LC`

Layer khuyen nghi:

- `TAC - Dim` de giu dimension text cho mapping review
- `TAC - Door window`
- `TAC - Betong`
- `TAC - Lop hoan thien`
- Layer `0` ben trong block definitions neu CAD dung nested blocks

Layer/entity loai khoi hop dong parser:

- Layer nam ngoai keep-list hien tai van co the duoc raw parser doc, nhung se khong duoc xem la geometry contract cho filtered extract va intake readiness
- Furniture, sanitary, callout, va utility inserts match cac token loai tru nhu `_Dot`, `lavabo`, `giuong`, `DOUBLE-SINK`, `maygiat`, `hat1`, `ref1`, `THANG`, `TRUC`, `Section Callout`, `KH_CLC` khong duoc xem la intake geometry cua case
- Neu geometry chinh cua case nam chu yeu tren layer ngoai keep-list, case do chua pass S04 du pipeline raw parser van doc duoc file

Acceptance gate:

1. Co day du raw CAD va ready parser-readable text gan cung `case_id`.
2. Parser-readable text phai la file text DXF hop le, co so dong chan theo cap `group-code/value`, va parser `dxf_raw_parser.py` doc duoc.
3. Co room/title anchors hop le theo `naming_rules.json` cua case.
4. Geometry chinh cua case nam tren keep-list layer hien tai; neu khong thi phai review/remap truoc khi gan vao pipeline.
5. `pipeline_case.json` cua case moi phai tro output vao `5_output/<case_id>/...` de tranh ghi de case khac.

---

## Rule Cho Root Workspace

Tai root `DesignBuilder_DXF_to_IDF_Pipeline/`, chi duoc dat:

- File quan ly du an: `pyproject.toml`, `requirements.txt`, `.gitignore`
- File tong hop cap du an: vi du `ANALYSIS_COMPLETE.md`

Khong dat tai root:

- File input nghiep vu
- File output nghiep vu
- Report tam
- File sinh ra boi script sau moi lan chay

---

## Rule Cau Truc IDF De Import Vao DesignBuilder

Phan nay ap dung cho:

- IDF nguon dat trong `1_input/<project_id>/raw/idf/` hoac `1_input/library/idf_import/reference/idf/`
- IDF da lam sach, prune, crosscheck, hoac san sang import dat trong `5_output/<project_id>/idf/`

### 1. Pham vi du lieu ma DesignBuilder import

Theo tai lieu chinh thuc cua DesignBuilder, IDF Import chi nham vao:

- building geometry
- shade geometry
- materials
- constructions
- glazing

Du lieu khac trong IDF co the ton tai nhung khong duoc xem la noi dung import chinh.

Vi vay, trong workspace nay, mot file IDF "ready for DesignBuilder import" phai duoc to chuc quanh geometry + envelope, khong xem HVAC, schedule, output, sizing, loads la thanh phan bat buoc cho import.

Them vao do:

- DesignBuilder Help neu ro tat ca EnergyPlus versions tu `7.2` tro di duoc ho tro cho IDF import.
- Bai viet support cua DesignBuilder cho biet DesignBuilder `v7.3` ho tro IDF luu trong khoang `7.2` den `22.2`.

Rule workspace:

1. File IDF import-ready phai co object `Version`.
2. `Version` phai nam trong dai version ma DesignBuilder dang su dung ho tro.
3. Neu co nhieu muc tieu su dung, uu tien chon version tuong thich ro rang voi ban DesignBuilder dang dung thay vi de mo ho.

### 2. Nhom object duoc DesignBuilder ho tro cho import

Theo danh sach object duoc DesignBuilder cong bo, nhom object nen co trong file import gom:

- General:
  - `Site:Location`
  - `Building`
  - `Zone`

- Detailed surfaces:
  - `BuildingSurface:Detailed`
  - `Wall:Detailed`
  - `RoofCeilingDetailed`
  - `Floor:Detailed`
  - `FenestrationSurface:Detailed`
  - `Shading:Site:Detailed`
  - `Shading:Building:Detailed`
  - `Shading:Zone:Detailed`

- Simple surfaces:
  - `Wall:Exterior`
  - `Wall:Interzone`
  - `Wall:Adiabatic`
  - `Wall:Underground`
  - `Roof`
  - `Ceiling:Interzone`
  - `Ceiling:Adiabatic`
  - `Floor:GroundContact`
  - `Floor:Interzone`
  - `Floor:Adiabatic`
  - `Window`
  - `Window:Interzone`
  - `Door`
  - `Door:Interzone`
  - `GlazedDoor`
  - `GlazedDoor:Interzone`

- Materials and constructions:
  - `Material`
  - `Material:NoMass`
  - `Material:AirGap`
  - `Construction`
  - `WindowMaterial:SimpleGlazingSystem`
  - `WindowMaterial:Glazing`
  - `WindowProperty:FrameAndDivider`
  - `WindowMaterial:Gas`
  - `WindowMaterial:GasMixture`

### 3. Gioi han import can dua vao rule workspace

Theo tai lieu chinh thuc cua DesignBuilder:

1. Khong phai moi noi dung trong IDF deu duoc import.
2. Zone multipliers, surface multipliers, opening multipliers khong duoc ho tro.
3. Geometry tao bang relative coordinates khong duoc ho tro.
4. Shade surfaces chi ho tro shading plane surfaces.
5. Neu thickness cua material khong duoc khai bao hoac bang `0`, DesignBuilder ap mac dinh `0.05 m`.

Tu do, workspace nay ap dung cac quy dinh bo sung sau:

1. IDF import-ready phai bo hoac prune cac nhom object khong phuc vu geometry/envelope nhu:
   - `Schedule:*`
   - `People`
   - `Lights`
   - `OtherEquipment`
   - `ZoneInfiltration:*`
   - `ZoneVentilation:*`
   - `ZoneHVAC:*`
   - `Output:*`
   - `Sizing:*`
   - cac object simulation phu tro khac
2. Khong duoc dua vao file import-ready bat ky multiplier nao khac gia tri mac dinh an toan.
3. Geometry phai dung he toa do tuyet doi, khong dua vao relative coordinate options.
4. Material phai co thickness ro rang neu la vat lieu can chieu day vat ly; khong duoc dua vao default `0.05 m` tru khi co chu dich ro rang.
5. Neu co shading, chi luu shading plane surfaces thuoc cac object shading detailed duoc ho tro.

### 4. Cau truc IDF chuan cua workspace cho muc dich import

DesignBuilder khong cong bo mot thu tu object bat buoc duy nhat. Tuy nhien, workspace nay chuan hoa file IDF import-ready theo thu tu sau de doc, kiem tra, va so sanh on dinh hon:

1. `Version`
2. `Site:Location`
3. `Building`
4. `GlobalGeometryRules`
5. `Zone`
6. `Material*` va `WindowMaterial*`
7. `Construction`
8. `BuildingSurface:Detailed`
9. `FenestrationSurface:Detailed`
10. `Shading:*:Detailed` neu co

Luu y:

- Thu tu tren la quy uoc cua workspace, khong phai yeu cau bat buoc duoc DesignBuilder cong bo.
- `GlobalGeometryRules` duoc workspace xem la thanh phan nen co de lam ro quy tac geometry, nhat la khi can tranh relative coordinate issues.
- Workspace uu tien `Detailed` objects thay vi `Simple surfaces` de giu duoc geometry ro rang va de crosscheck on dinh hon.

### 5. Rule luu file IDF lien quan den import

1. IDF goc nhan tu ben ngoai luu trong `1_input/<project_id>/raw/idf/` neu thuoc rieng project, hoac trong `1_input/library/idf_import/reference/idf/` neu la shared reference/sample.
2. IDF sau prune, crosscheck, hoac da dat chuan import luu trong `5_output/<project_id>/idf/`.
3. Khong ghi de truc tiep file goc trong `1_input/<project_id>/raw/idf/` hoac `1_input/library/idf_import/reference/idf/`.
4. Report kiem tra IDF import luu trong `5_output/<project_id>/reports/`.
5. Schema mo ta cau truc IDF luu trong `4_schemas/idf/` hoac `4_schemas/json/` tuy dinh dang.

### 6. Nguon tham khao chinh thuc

- DesignBuilder Help - IDF Import:
  - https://designbuilder.co.uk/helpv7.0/Content/IDFImport.htm
- DesignBuilder Support - 3D CAD Interoperability:
  - https://support.designbuilder.co.uk/support/solutions/articles/103000181362-3d-cad-interoperability
- DesignBuilder Help - Imported Surfaces:
  - https://designbuilder.co.uk/helpv8.0/Content/ImportedSurfaces.htm
- DesignBuilder Help - EnergyPlus Version Compatibility:
  - https://designbuilder.co.uk/helpv7.0/Content/EnergyPlus_Version_Compatibility.htm

---

## Rule Dat Ten Va Ghi De

1. Uu tien cap nhat in-place cho file output co vai tro on dinh.
2. Neu can ghi de file output quan trong, tao backup truoc trong `7_archive/backup/`.
3. Khong tao ban sao `_new`, `_final_v2`, `_fixed` neu chua duoc yeu cau ro rang.
4. Ten file phai phan anh dung loai du lieu va stage xu ly.

---

## Rule Khong Tao File Moi Neu Chua Duoc Yeu Cau

1. Mac dinh khong tao file moi, thu muc moi, hoac bien the file moi neu chua co yeu cau ro rang.
2. Khi co the, uu tien cap nhat file hien co thay vi sinh them file khac ten.
3. Khong tu y tao cac file dang:
   - `_new`
   - `_v2`
   - `_fixed`
   - `_final_final`
   - `copy`
4. Chi tao file moi trong cac truong hop sau:
   - Nguoi dung yeu cau tao file moi
   - Pipeline bat buoc sinh artifact dau ra vao `5_output/`
   - Can tao backup trong `7_archive/backup/` truoc khi ghi de
   - Can tao file schema, report, package theo dung chuc nang da duoc giao
5. Neu khong chac nen cap nhat file cu hay tao file moi, mac dinh chon cap nhat file cu hoac dung lai de xin yeu cau ro hon.

---

## Rule Ap Dung Ngay Cho Workspace Hien Tai

Theo trang thai workspace hien tai:

- `construction_input_3_brick_walls.csv` va `materials_for_construction_input_3_brick_walls.csv` phai nam trong `1_input/library/idf_import/legacy/`
- `Test1_for_DB_import.idf` va `Test1_for_DB_import_DBlean.idf` phai nam trong `1_input/library/idf_import/reference/idf/`
- Case config dang active phai nam trong `2_config/projects/noxh_apartment_a_clean/`, gom `pipeline_case.json`, `naming_rules.json`, va `geometry_policy.json`
- Scaffold intake multi-case duoc phep them trong `2_config/projects/<case_id>/` mien la khong ghi de case dang active
- Raw DXF normalized artifacts cua case dang active phai nam trong `5_output/noxh_apartment_a_clean/normalized/dxf/`
- `NOXH_Apartment_A_clean_filtered_extract.txt` phai nam trong `5_output/noxh_apartment_a_clean/normalized/dxf/`
- Script raw parser phai nam trong `3_scripts/parsers/`
- Script build mapping semantic trung gian phai la `3_scripts/context/mapping_builder.py`
- `3_scripts/context/` duoc dung cho script/helper context sau parser
- Script dieu phoi chay nhieu buoc lien tiep phai nam trong `3_scripts/pipeline/`
- Schema JSON phai nam trong `4_schemas/json/`
- Mapping artifacts nhu `zone_candidates.json`, `opening_candidates.json`, `dimension_annotations.json`, `mapping_payload.json`, va `mapping_summary.json` phai nam trong `5_output/noxh_apartment_a_clean/intermediate/mapping/`
- `bundle_manifest.json`, `Wall_Inventory.csv`, va `wall_positions.csv` phai nam trong output intermediate cua project tuong ung
- File `Test1_for_DB_import_DBlean_crosscheck.idf` phai nam trong `5_output/_shared/idf/`
- Cac report phan tich va crosscheck phai nam trong `5_output/<project_id>/reports/`
- `5_output/<project_id>/packages/` la noi danh rieng cho packaged delivery neu co
- Intake DXF multi-case moi phai uu tien `1_input/<case_id>/raw/cad/` va `1_input/<case_id>/clean/txt_dxf/`
- Case scaffold `<case_id>` phai tro output vao `5_output/<case_id>/`
- File cu nhu `apartment_A_raw_extract.txt` phai nam trong `7_archive/legacy/`

---

## Kiem Tra Nhanh

Truoc khi tao file moi, tu hoi 3 cau:

1. File nay la input, config, script, schema, output, docs, hay archive?
2. File nay co phai ket qua sinh ra boi script khong?
3. File nay co dang lam ban workspace bi tron lan vai tro khong?

Neu tra loi dung vai tro thi dat vao dung thu muc danh so tuong ung.

---

## Ket Luat

Workspace nay phai duoc van hanh theo mot huong duy nhat:

- `1_input` cho du lieu nguon
- `2_config` cho cau hinh
- `3_scripts` cho ma chay
- `4_schemas` cho schema
- `5_output` cho ket qua
- `6_docs` cho tai lieu
- `7_archive` cho luu tru

Moi file moi deu phai chon dung vai tro truoc khi duoc tao.
