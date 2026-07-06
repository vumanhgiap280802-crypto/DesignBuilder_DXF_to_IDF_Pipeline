# Library Standard

## Scope

Working standard nay duoc rut ra tu pipeline hien tai:

- `3_scripts/transformers/wall_logic.py`
- `3_scripts/transformers/fenestration_builder.py`
- `3_scripts/writers/bundle_writer.py`
- `3_scripts/writers/rebuild_idf_from_bundle.py`
- `2_config/library_paths.json`
- `1_input/library/idf_import/**/*`
- `5_output/_shared/idf/Test1_for_DB_import_DBlean_crosscheck.idf`
- `5_output/noxh_apartment_a_clean/csv/NOXH_Apartment_A_clean_idf_input_bundle/*.csv`

Khong co schema ly thuyet tach roi code. Chi nhung field dang duoc resolver, writer, hoac rebuilt IDF dung that moi vao `core`.

## Path Chuan

Shared library cho IDF import cua DesignBuilder dat trong:

- `1_input/library/idf_import/catalogs/constructions/constructions_catalog.csv`
- `1_input/library/idf_import/catalogs/materials/materials_catalog.csv`
- `1_input/library/idf_import/catalogs/resolvers/resolver_rules.csv`
- `1_input/library/idf_import/objects/constructions/Construction.csv`
- `1_input/library/idf_import/objects/materials/Material.csv`
- `1_input/library/idf_import/objects/materials/Material_NoMass.csv`
- `1_input/library/idf_import/objects/materials/Material_AirGap.csv`
- `1_input/library/idf_import/objects/fenestration/WindowMaterial_Glazing.csv`
- `1_input/library/idf_import/objects/fenestration/WindowMaterial_Gas.csv`
- `1_input/library/idf_import/objects/fenestration/WindowProperty_FrameAndDivider.csv`
- `1_input/library/idf_import/legacy/walls/construction_input_3_brick_walls.csv`
- `1_input/library/idf_import/legacy/materials/materials_for_construction_input_3_brick_walls.csv`
- `1_input/library/idf_import/reference/idf/*.idf`

Manifest runtime:

- `2_config/library_paths.json`

Shared sample geometry template van nam trong:

- `5_output/_shared/idf/Test1_for_DB_import_DBlean_crosscheck.idf`

## Phan Lop

- `catalogs/`: input chuan cho resolver va runtime mapping.
- `objects/`: CSV tach theo EnergyPlus object de bundle writer co the preload Material, Construction, Glazing, Gas, va Frame rows ma khong can hardcode.
- `legacy/`: giu lai input wall/material cu de fallback co kiem soat.
- `reference/`: IDF/XML sample dung cho doi chieu va nghien cuu import.
- `shared/`: input dung chung khong thuoc rieng IDF import catalog va duoc giu cho cac reference phi-project neu can.

## IDF Import Objects

Theo DesignBuilder Help, IDF import ho tro cac object nhom vat lieu/ket cau nhu `Material`, `Material:NoMass`, `Material:AirGap`, `Construction`, `WindowMaterial:Glazing`, `WindowMaterial:Gas`, va `WindowProperty:FrameAndDivider`. Trong EnergyPlus, `FenestrationSurface:Detailed` dung vertex cua phan glazing, con `WindowProperty:FrameAndDivider` mo ta frame/divider bao quanh glazing.

Vi vay shared library duoc chia theo 3 lop:

- opaque materials/constructions
- fenestration materials/frame
- legacy/reference inputs

## Runtime Rule

- `wall_logic` uu tien doc catalog tu `1_input/library/idf_import/catalogs/`.
- Neu co object CSV trong `1_input/library/idf_import/objects/`, bundle writer se dung chung cac row do cho `Construction.csv`, `Material.csv`, `WindowMaterial_Glazing.csv`, `WindowMaterial_Gas.csv`, va `WindowProperty_FrameAndDivider.csv`.
- Neu catalog chuan khong ton tai, loader moi fallback sang `1_input/library/idf_import/legacy/`.
- Neu ca catalog va legacy deu khong co, pipeline moi roi ve sample rows hardcoded trong `bundle_writer.py`.

## Freeze

Freeze schema khi pilot on dinh va dong thoi dat du 3 dieu kien:

1. Bundle moi khong con sinh them field/ten object ngoai catalog va object CSV library.
2. Resolver rules khong con phai them key moi de cover case Apartment A.
3. Rebuilt IDF va file crosscheck giu on dinh qua it nhat mot vong import DB/E+ ma khong can sua tay library.
