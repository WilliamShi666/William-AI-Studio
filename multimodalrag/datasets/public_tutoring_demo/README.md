# Roys Legion Demo Dataset

This directory is the planned public dataset shape for Roys Legion, the multidisciplinary visual tutor in William's AI Studio.

The goal is to let developers rebuild a local Milvus knowledge base from public files instead of downloading raw Milvus, etcd, or MinIO Docker volumes.

## Intended Contents

```text
public_tutoring_demo/
├── README.md
├── manifest.example.json
├── manifest.json              # generated or curated later
├── uploads/                   # approved public source PDFs
├── extraction_results/        # OCR markdown, metadata, and extracted images
├── chunks/                    # chunk JSON/JSONL files ready for import
└── scripts/
    ├── export_from_output.py
    ├── build_manifest.py
    ├── validate_dataset.py
    └── import_milvus.py
```

Large binary assets may be tracked with Git LFS or published as release artifacts after review.

## Why Not Commit Raw Milvus Volumes?

Milvus volumes contain runtime database state, not portable source data. They are harder to review, harder to diff, and can include metadata that is not obvious from the file tree.

The public dataset should be source-of-truth data:

- original public PDFs;
- OCR/extraction outputs;
- extracted images referenced by chunks;
- chunk metadata;
- optional precomputed embeddings;
- import scripts.

Developers can then start Milvus locally and rebuild collections from the dataset.

## Dataset Build Flow

Use `export_from_output.py` to convert Roys Legion backend output into the portable public dataset shape.

Small dry-run example:

```bash
cd multimodalrag/datasets/public_tutoring_demo
export ROYS_LEGION_OUTPUT_DIR=/path/to/roys-legion/backend/output
python scripts/export_from_output.py \
  --uploads-dir "$ROYS_LEGION_OUTPUT_DIR/uploads" \
  --extraction-results-dir "$ROYS_LEGION_OUTPUT_DIR/extraction_results" \
  --limit 2 \
  --subject Mathematics \
  --course "A-Level" \
  --public-release-approved \
  --output manifest.json
```

Targeted export example:

```bash
python scripts/export_from_output.py \
  --uploads-dir /path/to/output/uploads \
  --extraction-results-dir /path/to/output/extraction_results \
  --file-id 9e716d04-7ac8-4d48-a652-2c83bd82b00b \
  --subject Mathematics \
  --course "A-Level" \
  --public-release-approved \
  --output manifest.json
```

The export script copies reviewed source PDFs, OCR markdown/metadata/chunked JSON, extracted images, and writes normalized chunk JSONL files under `chunks/`.

Validate before publishing:

```bash
python scripts/validate_dataset.py --manifest manifest.json
```

## Expected Local Import Flow

1. Start the local Roys Legion Milvus stack.
2. Start the Milvus API service.
3. Review or generate `manifest.json`.
4. Import chunk files with `scripts/import_milvus.py`.
5. Run the Roys Legion chat service against the imported collection.

Example:

```bash
cd multimodalrag/datasets/public_tutoring_demo
python scripts/import_milvus.py \
  --manifest manifest.json \
  --milvus-api-url http://localhost:8000 \
  --collection-name roys_legion_demo
```

Dry-run import without contacting Milvus:

```bash
python scripts/import_milvus.py \
  --manifest manifest.json \
  --collection-name roys_legion_demo \
  --dry-run
```

## Manifest

`manifest.example.json` documents the expected shape. The final `manifest.json` should list every included document, its source file, extraction directory, original OCR output JSON, chunk JSONL file, subject, course, license/provenance, and checksums where practical.

## Current Status

This directory currently contains the dataset tooling and placeholder directories. The large public PDFs/images/chunks are not copied into this repository yet.

Preparation notes:

- The source server has approved public PDFs and extracted images outside this project path.
- The current local raw output size is about 1.1G for uploads and 5.5G for extraction results.
- A curated subset or Git LFS/release-asset strategy should be chosen before copying large assets.
- Raw Milvus Docker volumes remain excluded from normal git. Developers should rebuild Milvus collections from this dataset instead.
