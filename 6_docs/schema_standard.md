# Schema Standard

## Vai tro

- `source schema`: mo ta cau truc du lieu nguon ma parser doc truc tiep hoac parser-normalized source ma downstream tiep tuc dua vao. Trong workspace nay la `4_schemas/source/dxf/`.
- `contract schema`: mo ta metadata handoff ma pipeline hien tai thuc su truyen qua cac buoc. Trong workspace nay la `4_schemas/contracts/metadata_schema.json`.
- `output/reference schema`: mo ta field order va object shape cua bundle CSV va IDF reference ma writer/rebuild dang xuat hoac doi chieu. Trong workspace nay la `4_schemas/output/csv_bundle/` va `4_schemas/output/idf_reference/`.

## Working Standard Hien Tai

Working standard nay duoc rut ra tu flow dang chay cua case DXF active:

`2_config/projects/noxh_apartment_a_clean/pipeline_case.json`
-> `3_scripts/pipeline/run_case_pipeline.py`
-> `3_scripts/pipeline/apartment_a_pipeline.py`
-> `3_scripts/parsers/dxf_raw_parser.py`
-> `3_scripts/context/mapping_builder.py`
-> `3_scripts/transformers/geometry_inference.py`
-> `3_scripts/transformers/surface_builder.py`
-> `3_scripts/transformers/wall_logic.py`
-> `3_scripts/transformers/fenestration_builder.py`
-> `3_scripts/writers/bundle_writer.py`
-> `3_scripts/writers/rebuild_idf_from_bundle.py`

File `metadata_schema.json` chi dua vao `core` nhung field dang duoc downstream tieu thu de giu flow nay chay end-to-end. Field nao chi phuc vu QA, traceability, symbol hint, review, hoac chua on dinh cho da-case thi duoc de o `extension` va/hoac `provisional`.

## Core Va Extension

- `core`: field dang co downstream consumer thuc te trong pipeline DXF hien tai, hoac la writer default bat buoc de xuat bundle/IDF hien tai.
- `extension`: field co trong artifact hien tai nhung chua phai contract bat buoc cua flow end-to-end.
- `provisional`: field dang ton tai nhung chua nen freeze lam contract on dinh vi ten, vai tro, hoac muc do su dung chua du chac.

## Freeze Sau Pilot

1. Khong doi ten, doi meaning, hoac doi path schema contract neu chua cap nhat `4_schemas/contracts/metadata_schema.json` va `4_schemas/registry/schema_manifest.json`.
2. Moi thay doi contract phai doi chieu voi artifact that trong `5_output/intermediate/`, `5_output/csv/`, va `5_output/idf/` cua case pilot.
3. Field moi nen vao `extension` truoc; chi nang len `core` sau khi co downstream consumer that su dung no trong pipeline chinh.
4. Writer defaults trong `bundle_writer.py` chi nen freeze sau khi import DesignBuilder/IDF on dinh cho pilot hien tai.

File nay la ghi chu uu tien cho bo tri schema theo vai tro. Neu mot so tai lieu tong quat cu van con nhac den grouping `json/csv/idf`, hay uu tien layout trong file nay va trong `4_schemas/registry/schema_manifest.json`.
