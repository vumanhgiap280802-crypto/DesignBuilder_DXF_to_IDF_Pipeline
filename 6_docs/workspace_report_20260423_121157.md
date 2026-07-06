# Workspace Report

- Generated at: 2026-04-23 12:11:57 +07:00
- Workspace root: `DB_IDF_Workspace`
- Report scope: mô tả nhanh cấu trúc, mục đích, trạng thái dữ liệu, và thông tin backup của workspace hiện tại

## 1. Tổng quan

Workspace này là một dự án Python dùng để xử lý dữ liệu phục vụ import vào DesignBuilder/IDF. Theo `pyproject.toml`, tên dự án là `db-idf-workspace`; theo [architecture.md](./architecture.md), luồng chính hiện tại là:

`DXF/DXE -> mapping -> geometry -> surfaces -> walls -> fenestration -> CSV bundle -> rebuilt IDF`

Case đang được cấu hình rõ ràng trong workspace là `apartment_a`, với file điều phối chính:

- `3_scripts/pipeline/run_case_pipeline.py`
- `3_scripts/pipeline/apartment_a_pipeline.py`
- `2_config/projects/apartment_a/pipeline_case.json`

## 2. Thống kê nhanh

- Tổng số file: `202`
- Tổng số thư mục: `68`
- Tổng dung lượng toàn workspace: `175.84 MB`
- Dung lượng nếu không tính thư mục backup cũ `7_archive/backup`: `112.18 MB`

Phân bố top-level hiện tại:

| Thành phần | Loại | File | Thư mục con | Dung lượng | Cập nhật gần nhất |
| --- | --- | ---: | ---: | ---: | --- |
| `1_input` | Dir | 10 | 9 | 39.08 MB | 2026-04-22 09:15 |
| `2_config` | Dir | 8 | 3 | 0.09 MB | 2026-04-23 08:59 |
| `3_scripts` | Dir | 54 | 15 | 1.54 MB | 2026-04-22 11:34 |
| `4_schemas` | Dir | 20 | 8 | 0.14 MB | 2026-04-23 08:29 |
| `5_output` | Dir | 96 | 23 | 71.26 MB | 2026-04-22 17:22 |
| `6_docs` | Dir | 5 | 0 | 0.03 MB | 2026-04-23 09:12 |
| `7_archive` | Dir | 5 | 3 | 63.68 MB | 2026-04-09 16:17 |
| `.gitignore` | File | 1 | 0 | < 0.01 MB | 2026-04-09 16:19 |
| `ANALYSIS_COMPLETE.md` | File | 1 | 0 | 0.01 MB | 2026-04-16 14:48 |
| `pyproject.toml` | File | 1 | 0 | < 0.01 MB | 2026-04-16 14:48 |
| `requirements.txt` | File | 1 | 0 | < 0.01 MB | 2026-04-09 16:30 |

Các đuôi file xuất hiện nhiều nhất:

- `.csv`: `79`
- `.json`: `42`
- `.pyc`: `28`
- `.py`: `24`
- `.md`: `6`
- `.idf`: `5`
- `.zip`: `4`

## 3. Vai trò từng thư mục chính

### `1_input`

Chứa dữ liệu đầu vào cho pipeline:

- Input raw DXF/DXE/IDF
- Dữ liệu clean phục vụ mapping hoặc library
- Có file nguồn đáng chú ý như `Drawing1.dwg`, `1_input/raw/txt (dxf)/Apartment A dxf.txt`, `1_input/raw/dxe/new_block.dxe`

### `2_config`

Chứa cấu hình dùng để điều phối pipeline:

- Catalog thư viện vật liệu/kết cấu: `2_config/library/*.csv`
- Rule tổng quát của workspace: `2_config/workspace_rules.json`
- Config riêng cho case `apartment_a`: geometry policy, naming rules, pipeline case

### `3_scripts`

Chứa mã nguồn Python cho pipeline:

- `parsers/`: parse DXF, DXE
- `context/`: build mapping, join ngữ cảnh DXF-DXE
- `transformers/`: geometry, surface, wall, fenestration
- `writers/`: tạo CSV bundle, rebuild IDF
- `pipeline/`: orchestration chạy toàn luồng
- `workspace_rules/`: script hỗ trợ kiểm soát rule và compliance

### `4_schemas`

Chứa schema nguồn và schema đầu ra:

- Contracts cho metadata/material/construction/opening
- Schema source cho DXF/DXE
- Schema output cho CSV bundle và IDF reference

### `5_output`

Chứa toàn bộ artifact sinh ra từ pipeline:

- `normalized/`: dữ liệu DXF/DXE đã chuẩn hóa
- `intermediate/`: mapping, geometry, surfaces, walls, fenestration, join
- `csv/`: bundle CSV để import/kiểm tra
- `idf/`: IDF đã rebuild
- `reports/`: báo cáo schema/inventory đã sinh trước đó

Đây hiện là thư mục lớn nhất nếu không tính archive, phản ánh workspace đã được chạy pipeline và đang giữ nhiều artifact trung gian.

### `6_docs`

Chứa tài liệu làm chuẩn vận hành workspace:

- `architecture.md`
- `library_standard.md`
- `naming.md`
- `schema_standard.md`
- `WORKSPACE_RULES.md`

### `7_archive`

Chứa dữ liệu lưu trữ:

- `backup/`: các file zip backup cũ
- `legacy/`: dữ liệu cũ hoặc tham chiếu
- `rejected/`: vùng lưu dữ liệu loại bỏ

## 4. Entry points và output quan trọng

Các file quan trọng để hiểu hoặc chạy workspace:

- Entry point: `3_scripts/pipeline/run_case_pipeline.py`
- Pipeline implementation: `3_scripts/pipeline/apartment_a_pipeline.py`
- Case config: `2_config/projects/apartment_a/pipeline_case.json`
- Library config đang mở trong IDE: `2_config/library/constructions_catalog.csv`
- Output IDF chính: `5_output/noxh_apartment_a_clean/idf/NOXH_Apartment_A_clean_generated_from_bundle.idf`
- Output CSV bundle chính: `5_output/csv/Apartment_A_idf_input_bundle/`

Theo `pipeline_case.json`, output directory của pipeline đã được chuẩn hóa khá đầy đủ cho từng stage: normalized, intermediate, csv bundle, và rebuilt IDF.

## 5. Tình trạng backup trước khi tạo bản mới

Các backup đang tồn tại trong `7_archive/backup`:

| File backup | Dung lượng | Thời gian |
| --- | ---: | --- |
| `workspace_backups_consolidated_20260422_160609.zip` | 5.87 MB | 2026-04-22 16:06 |
| `workspace_backups_consolidated_20260422_131654.zip` | 5.88 MB | 2026-04-22 13:17 |
| `workspace_backups_consolidated_20260422_080817.zip` | 4.95 MB | 2026-04-22 08:09 |
| `workspace_backups_consolidated_20260421_100842.zip` | 46.96 MB | 2026-04-21 10:09 |

Chênh lệch dung lượng cho thấy các bản backup cũ nhiều khả năng đã dùng tập file nguồn khác nhau hoặc có khác biệt về phạm vi nén.

## 6. Ghi chú vận hành

- `pyproject.toml` mô tả workspace như một Python project hoàn chỉnh, dùng các thư viện như `pandas`, `ezdxf`, `eppy`, `pydantic`, `typer`.
- Workspace hiện đã có cả dữ liệu input, config, scripts, schema, output và archive; tức là đây không chỉ là repo mã nguồn mà là một workspace vận hành tương đối đầy đủ.
- `pyproject.toml` đang tham chiếu `6_docs/README.md`, nhưng trong `6_docs` hiện chưa thấy file này. Đây là một điểm nên lưu ý nếu sau này đóng gói tài liệu hoặc publish project metadata.

## 7. Phạm vi backup được tạo kèm report này

Bản backup mới được tạo sau report này nên sẽ bao gồm file report hiện tại. Để tránh lồng backup vào chính nó, phạm vi nén nên bao gồm toàn bộ `DB_IDF_Workspace` ngoại trừ thư mục lưu backup sẵn có:

- Bao gồm: `1_input` đến `7_archive` và các file top-level khác
- Loại trừ: `DB_IDF_Workspace/7_archive/backup`

Điều này giúp giữ nguyên trạng thái làm việc hiện tại nhưng không chèn các file zip lịch sử vào trong zip mới.
