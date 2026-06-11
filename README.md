# William's AI Studio

William's AI Studio is an open-source monorepo for AI agent workflows, a local Claude Code UI, and a multidisciplinary visual tutoring system.

This repository is published as a source-first open-source release. Local secrets, runtime logs, generated traces, build outputs, database volumes, and unreviewed data artifacts are intentionally excluded from the public source tree.

## Projects

| Public name | Path | Purpose |
|-------------|------|---------|
| Roys Alpha | `WilliamManus/` | Main web/backend agent platform for model conversations, agent runs, files, sandboxed work, and workflow orchestration. |
| Claude Code UI | `cc-flow-src/` | Local visual coding workflow UI and desktop/web app around Claude Code-style agent workflows. |
| Roys Legion | `multimodalrag/` | A-Level/AP multidisciplinary visual tutor with OCR, PDF-to-markdown processing, chunking, Milvus retrieval, image serving, and multimodal chat. |
| Roys Legion Demo Dataset | `multimodalrag/datasets/public_tutoring_demo/` | Source-first dataset tooling, manifests, placeholder directories, and Milvus import scripts. Full public PDFs/images/chunks are packaged separately after curation. |

Directory names are kept stable for the first cleanup pass. Public docs use the product names above.

## Upstream Attribution

Roys Alpha (`WilliamManus/`) is a secondary development based on [`kortix-ai/suna`](https://github.com/kortix-ai/suna). Suna is currently published under the Elastic License 2.0, so Suna-derived portions of Roys Alpha remain subject to the applicable upstream license terms and notices. Original William's AI Studio additions are provided under the repository's Apache-2.0 license unless a file or third-party notice says otherwise.

## Current Status

This is the initial source-first public release.

Completed so far:

- Apache-2.0 root license added.
- Public export allowlist and denylist drafted.
- A dry-run public candidate was generated locally and scanned for high-signal secrets and fixed IPs.
- `.env.example` files and several public test/config defaults were sanitized.
- Hardcoded public server IP defaults were replaced with `localhost` in known public runtime paths.
- Unsafe JWT defaults and the local `/admin/env-vars` exposure were remediated and runtime-checked.

Still required before publishing additional public artifacts:

- Provider credential revocation/rotation evidence outside the repository.
- Continued legal/provenance review for optional dependencies and upstream-derived areas.
- Full Roys Legion demo dataset packaging if a dataset-complete launch is added later.

## Local Development Model

The public setup assumes local development by default.

- Use Python 3.11+ for backend development. Roys Alpha currently depends on `browser-use==0.7.4`, which does not install on Python 3.10.
- Use Node.js 20+ for frontend and Claude Code UI development.
- Use Docker for Milvus and other local service dependencies.
- Use Redis 8.0.0+ for Roys Alpha streaming/pub-sub/message-bus paths.
- Services should default to `localhost`.
- Copy sanitized examples before running services, for example `.env.example` to `.env`.
- Fill API keys yourself. Real provider keys are not included.
- Runtime logs, `.env` files, build output, worktrees, vector database volumes, and generated extraction output should not be committed.

## Quick Start Overview

Detailed setup guides are still being consolidated. Use this as the current orientation map. Do not use `start_fusion.sh`; it is a private-prep convenience launcher and is excluded from the public source candidate.

The monorepo can be run in two modes:

- direct local service mode: open each service on its own local port;
- Nginx aggregate mode: run the services locally, then use `deploy/nginx/fusion-agent.conf` as the single browser entry point for Roys Alpha and Roys Legion.

Claude Code UI is a separate local developer control plane. It is not proxied by the current Nginx aggregate config.

### Roys Alpha

Source:

```bash
cd /path/to/williams-ai-studio
```

Key areas:

- `WilliamManus/frontend/` - web frontend
- `WilliamManus/backend/` - FastAPI backend and agent services
- `WilliamManus/backend/.env.example` - sanitized backend configuration template

Typical local flow:

```bash
python --version  # should be 3.11+

cd WilliamManus/backend
cp .env.example .env
# Fill required local values and API keys.
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cd ../frontend
npm install
npm run build
```

Runtime:

```bash
# Start Redis first. Redis 8.0.0+ is recommended.
redis-server

# From the repository root:
cd /path/to/williams-ai-studio
./start_william_prod.sh
./stop_william_prod.sh
```

The Roys Alpha helper scripts use tmux sessions and expect frontend build artifacts for production-style startup. For lightweight backend checks, use the smoke-mode commands documented in the preparation reports.

### Claude Code UI

Source:

```bash
cd cc-flow-src
npm install -g @anthropic-ai/claude-code
claude
pnpm install
pnpm dev
```

Claude Code UI needs the Claude Code CLI installed and authenticated on the same machine because it reads and drives local Claude Code sessions.

Expected local ports are documented in `cc-flow-src/README.md`.

### Roys Legion

Source:

```bash
cd multimodalrag
```

Key areas:

- `multimodalrag/frontend/` - Vite/React tutor UI
- `multimodalrag/backend/` - OCR, chunking, Milvus, chat, and debate services
- `multimodalrag/backend/.env.example` - sanitized backend configuration template
- `multimodalrag/backend/Database/milvus_server/docker-compose.yaml` - Milvus stack definition

Typical local flow:

```bash
python --version  # 3.11+ recommended for the public monorepo

cd multimodalrag/backend
cp .env.example .env
# Fill required local values and API keys.
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cd ../frontend
npm install
npm run build
```

Runtime:

```bash
cd multimodalrag/backend
./start_all_services.sh
./stop_all_services.sh
```

Roys Legion depends on a working Milvus service and OCR/model services for the full PDF-to-tutor workflow. If you want to process new PDFs and expand the knowledge base, deploy at least one supported OCR/PDF extraction path: DeepSeek OCR, PaddleOCR/PaddleOCR-VL, or MinerU.

Dependency note: the default Roys Legion backend install intentionally does not install PyMuPDF, PyMuPDF4LLM, or pdf4llm. Those packages are used only by optional fast PDF/PDF-input paths and are available through AGPL or commercial license terms. Install `multimodalrag/backend/requirements-pdf-agpl.txt` or `multimodalrag/backend/Information-Extraction/deepseekocr/requirements-pdf-agpl.txt` only if your use case complies with AGPL obligations or you have an appropriate commercial license.

### Nginx Aggregate Entry Point

The production-style local aggregate config is:

```text
deploy/nginx/fusion-agent.conf
```

After Roys Alpha, Roys Legion, Redis, and the required local backend services are running, install that config into your local Nginx site configuration and reload Nginx. The public example assumes `localhost`.

```bash
sudo cp deploy/nginx/fusion-agent.conf /etc/nginx/conf.d/williams-ai-studio.conf
sudo nginx -t
sudo systemctl reload nginx
```

Default aggregate routes:

| Browser path | Proxied local service |
|--------------|----------------------|
| `/` | Roys Alpha frontend on `127.0.0.1:3000` |
| `/api/` | Roys Alpha backend on `127.0.0.1:8002` |
| `/tutor/` | Roys Legion frontend on `127.0.0.1:5173` |
| `/mr-api/milvus/` | Roys Legion Milvus API on `127.0.0.1:8000` |
| `/mr-api/chunk/` | Roys Legion chunking API on `127.0.0.1:8001` |
| `/mr-api/extraction/` | Roys Legion extraction API on `127.0.0.1:8006` |
| `/mr-api/chat/` | Roys Legion chat API on `127.0.0.1:8501` |
| `/mr-api/debate/` and `/debate-api/` | Roys Legion debate API on `127.0.0.1:8602` |

`deploy/nginx/fusion-agent-dev.conf` is a narrower development example for Roys Alpha only. It is not the full three-project aggregate entry point.

## Roys Legion Data And Milvus

Roys Legion is not only a generic RAG demo. Its main use case is multidisciplinary visual tutoring:

- students choose a model and subject;
- PDFs are processed by OCR/PDF extraction services;
- markdown and image references are chunked;
- chunks are embedded and stored in Milvus;
- retrieved image URLs are served back to the chat UI;
- the assistant answers with subject-specific, image-supported explanations.

The source-first public candidate makes the workflow reproducible through docs and import tooling, but it does not commit the full public teaching dataset as ordinary git files. The large PDFs, extracted images, chunk files, metadata, and optional embeddings are packaged later through Git LFS, release artifacts, or external storage after final curation and checksum generation.

Planned public data shape:

```text
multimodalrag/
└── datasets/
    └── public_tutoring_demo/
        ├── README.md
        ├── manifest.json
        ├── uploads/
        ├── extraction_results/
        ├── embeddings.jsonl
        └── import_milvus.py
```

Raw Milvus Docker volumes are not the primary public data format. Developers should be able to rebuild Milvus from the public dataset and import scripts. An optional reviewed Milvus snapshot may be published later as a release artifact if it is useful.

Current source-first status:

- `multimodalrag/datasets/public_tutoring_demo/` contains the dataset README, manifest example, placeholder directories, and export/validate/import scripts.
- Full approved public PDFs/images/chunks are not included in the first source candidate.
- Developers can use the included scripts to export, validate, and import an approved dataset package once it is published or generated locally.
- Raw Milvus, MinIO, and etcd runtime volumes remain excluded from normal git source.

## OCR And Model Services

Roys Legion uses OCR/PDF extraction services such as:

- MinerU
- DeepSeek OCR
- PaddleOCR / PaddleOCR-VL

These model services are not ordinary source files and should not be committed as large model blobs in this repository. Public setup should use documented model download links, checksums, or separate model artifacts.

Some OCR/PDF paths use optional PDF rendering/extraction packages:

- default backend installs avoid PyMuPDF-family dependencies;
- fast PyMuPDF4LLM extraction requires `multimodalrag/backend/requirements-pdf-agpl.txt`;
- DeepSeek OCR PDF input support requires `multimodalrag/backend/Information-Extraction/deepseekocr/requirements-pdf-agpl.txt`;
- these optional files are separated because PyMuPDF/MuPDF licensing is AGPL or commercial.

The OCR-to-knowledge-base workflow is documented in `opensource_prep_reports/OCR_RAG_PIPELINE_AND_MODEL_RELEASE.md` during the preparation phase.

## Project Structure

```text
.
├── WilliamManus/      # Roys Alpha web frontend, FastAPI backend, agent runtime, and workflow orchestration
├── cc-flow-src/       # Claude Code UI local visual coding workflow app
├── multimodalrag/     # Roys Legion visual tutor, OCR/RAG backend, frontend, Milvus tooling, and dataset tooling
├── deploy/            # Sanitized deployment examples
└── scripts/           # Public candidate hygiene and developer utility scripts
```

## Public Export Policy

The public repository should be created from a clean export allowlist, not by publishing the current private git history directly.

Denied by default:

- `.env` files and credentials
- runtime logs and session artifacts
- generated traces/evals
- temporary probe files
- build outputs and release bundles
- `node_modules`
- Milvus runtime volumes
- unreviewed generated PDFs/images/media
- non-allowlisted Markdown docs, including private handoffs, research notes, generated skill docs, copied third-party docs, and VibeCoding methodology drafts
- private handoff/debug documents

See `opensource_prep_reports/PUBLIC_EXPORT_ALLOWLIST.md` and `opensource_prep_reports/ARTIFACTS.md` for the current release boundary.

## License

This project is licensed under the Apache License 2.0. See `LICENSE`.

Dependency license review is still required before the first public release candidate.
