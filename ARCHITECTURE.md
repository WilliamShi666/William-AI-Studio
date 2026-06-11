# Architecture

William's AI Studio is a monorepo with three main public product areas and source-first Roys Legion Demo Dataset tooling.

## System Overview

```text
William's AI Studio
├── WilliamManus/      # Roys Alpha
├── cc-flow-src/       # Claude Code UI
├── multimodalrag/     # Roys Legion
├── deploy/            # sanitized deployment examples
├── scripts/           # public hygiene and developer utility scripts
└── multimodalrag/datasets/public_tutoring_demo/  # Roys Legion Demo Dataset tooling
```

## Roys Alpha

Roys Alpha is the main agent platform. It contains a web frontend, a FastAPI backend, agent orchestration code, model/provider integrations, file handling, sandbox-related workflows, and admin/configuration surfaces.

Important areas:

- `WilliamManus/frontend/` - web application;
- `WilliamManus/backend/` - backend APIs and agent runtime;
- `WilliamManus/backend/.env.example` - sanitized local configuration template;
- `WilliamManus/backend/admin/` - admin API surfaces;
- `WilliamManus/backend/utils/` - shared backend utilities including auth/config helpers.

Public development should use local configuration and explicit secrets. Authentication and admin endpoints should fail closed when required secrets are missing or weak.

## Claude Code UI

Claude Code UI is the local visual coding workflow UI and desktop/web app.

Important areas:

- `cc-flow-src/` - source root;
- package metadata and app configuration inside the project directory;
- local development commands documented in `cc-flow-src/README.md`.

The public threat model should treat this as a local developer tool by default. Services should bind to loopback unless explicitly configured otherwise.

## Roys Legion

Roys Legion is a multidisciplinary visual tutor for A-Level/AP style learning. It is not just a generic RAG demo. The core workflow is subject-oriented tutoring with OCR, image-aware document extraction, chunking, Milvus retrieval, and multimodal answers.

Important areas:

- `multimodalrag/frontend/` - tutor UI;
- `multimodalrag/backend/` - OCR, extraction, chunking, retrieval, chat, and debate services;
- `multimodalrag/backend/.env.example` - sanitized local configuration template;
- `multimodalrag/backend/Database/milvus_server/docker-compose.yaml` - local Milvus stack definition;
- `multimodalrag/backend/Information-Extraction/` - PDF/OCR extraction services;
- `multimodalrag/backend/Text_segmentation/` - chunking services;
- `multimodalrag/backend/chat/` - chat and QA services.

## Roys Legion Knowledge-Base Flow

```text
PDF upload
  -> OCR/PDF extraction with MinerU, DeepSeek OCR, or PaddleOCR/PaddleOCR-VL
  -> markdown plus extracted images
  -> chunking and metadata generation
  -> embedding generation
  -> Milvus insert
  -> user question
  -> retrieval of text chunks and image URLs
  -> multimodal tutoring response
```

Images matter because retrieved chunks may reference extracted page images. The full public dataset package should include the images needed to reproduce visual answers.

## Data And Artifact Strategy

Public source should not include raw runtime state or large data blobs in ordinary git history. The source-first candidate includes dataset tooling and placeholder directories; the full public Roys Legion data package should use this reproducible shape:

```text
multimodalrag/datasets/public_tutoring_demo/
├── README.md
├── manifest.json
├── uploads/
├── extraction_results/
├── embeddings.jsonl
└── import_milvus.py
```

Raw Milvus, MinIO, and etcd Docker volumes should be excluded from normal git source. If a prebuilt vector snapshot is useful, publish it later as a reviewed release artifact or Git LFS-backed asset with checksums.

The first public source candidate is expected to include tooling only for the large dataset path. Approved PDFs, extracted images, chunks, metadata, optional embeddings, and checksums should be published later through Git LFS, release artifacts, or external storage after final curation.

## Local Services

Public defaults should assume local development:

- frontend and backend services use `localhost` defaults;
- Milvus can be started from the local Docker Compose definition;
- Redis 8.0.0+ is used by Roys Alpha streaming/pub-sub/message-bus paths and should be started before `./start_william_prod.sh`;
- OCR/model services should be documented with download links or separate artifacts rather than committed model blobs.

Public startup entry points:

| Project | Startup | Stop | Notes |
|---------|---------|------|-------|
| Roys Alpha | `./start_william_prod.sh` from the repository root | `./stop_william_prod.sh` | Start Redis 8.0.0+ first. |
| Roys Legion backend | `./start_all_services.sh` from `multimodalrag/backend/` | `./stop_all_services.sh` | Deploy DeepSeek OCR, PaddleOCR/PaddleOCR-VL, or MinerU when building new knowledge bases from PDFs. |
| Claude Code UI | `pnpm dev` from `cc-flow-src/` | Stop the dev process | Local developer tool; loopback defaults are expected. |

Nginx aggregate entry point:

| Public path | Local service |
|-------------|---------------|
| `/` | Roys Alpha frontend on `127.0.0.1:3000` |
| `/api/` | Roys Alpha backend on `127.0.0.1:8002` |
| `/tutor/` | Roys Legion frontend on `127.0.0.1:5173` |
| `/mr-api/milvus/` | Roys Legion Milvus API on `127.0.0.1:8000` |
| `/mr-api/chunk/` | Roys Legion chunking API on `127.0.0.1:8001` |
| `/mr-api/extraction/` | Roys Legion extraction API on `127.0.0.1:8006` |
| `/mr-api/chat/` | Roys Legion chat API on `127.0.0.1:8501` |
| `/mr-api/debate/`, `/debate-api/` | Roys Legion debate API on `127.0.0.1:8602` |

The aggregate config lives at `deploy/nginx/fusion-agent.conf`. Claude Code UI is intentionally separate from this aggregate Nginx config and runs as a local developer tool through `pnpm dev`.

## Release Boundary

The public repository should be created from a clean export allowlist, not by publishing private git history directly. The release boundary is tracked in:

- `opensource_prep_reports/OPEN_SOURCE_SCOPE.md`;
- `opensource_prep_reports/PUBLIC_EXPORT_ALLOWLIST.md`;
- `opensource_prep_reports/ARTIFACTS.md`;
- `opensource_prep_reports/OCR_RAG_PIPELINE_AND_MODEL_RELEASE.md`.

Before public release, run secret scanning, artifact denylist checks, dependency license/SBOM review, and fresh-clone smoke verification.
