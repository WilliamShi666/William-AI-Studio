# Contributing

Thank you for your interest in William's AI Studio. The project is still in open-source preparation, so contribution workflows are intentionally conservative until the first public release candidate is verified.

## Project Layout

| Public name | Path | Scope |
|-------------|------|-------|
| Roys Alpha | `WilliamManus/` | Main web/backend agent platform. |
| Claude Code UI | `cc-flow-src/` | Local coding workflow UI and desktop/web app. |
| Roys Legion | `multimodalrag/` | Multidisciplinary visual tutor, OCR, Milvus retrieval, and multimodal chat. |
| Roys Legion Demo Dataset | `multimodalrag/datasets/public_tutoring_demo/` | Planned public teaching dataset and import workflow. |

## Before You Start

Use local development settings by default.

- Copy `.env.example` files to `.env` only in your local checkout.
- Do not commit `.env` files, logs, generated outputs, database volumes, or model blobs.
- Prefer `localhost` defaults for local services.
- Provide your own API keys and model credentials.

## Development Workflow

1. Start from a clean branch.
2. Keep changes scoped to one product area when practical.
3. Update public docs when behavior, setup, configuration, or service boundaries change.
4. Run the smallest relevant verification first, then broader checks when the change affects shared behavior.
5. Record any open-source preparation evidence in `opensource_prep_reports/` when the work changes release readiness.

## Commit Style

Use conventional commit-style summaries:

```text
<type>: <description>
```

Common types:

- `feat` for new features;
- `fix` for bug fixes;
- `docs` for documentation;
- `test` for tests;
- `refactor` for behavior-preserving cleanup;
- `chore` for maintenance.

## Security Rules

Never commit:

- API keys, tokens, passwords, cookies, JWT secrets, or database credentials;
- private `.env` files;
- runtime logs or browser/session traces;
- Milvus, MinIO, etcd, Redis, or database runtime volumes;
- unreviewed generated PDFs, screenshots, uploads, downloads, or model outputs;
- private deployment IPs, hostnames, or credentials.

If you find a real credential, follow `SECURITY.md` and do not paste the value in a public issue or pull request.

## Testing Expectations

Testing commands are still being consolidated for the first public release candidate. Until then:

- run package-local tests or builds for the area you changed;
- run formatting/type checks where configured;
- document any command that could not be run and why;
- avoid relying on Redis unless the specific feature under test requires streaming/pub-sub behavior.

## Dataset Contributions

Roys Legion dataset contributions should use the planned public dataset shape under `multimodalrag/datasets/public_tutoring_demo/`.

Dataset additions should include:

- source/license information;
- manifest metadata;
- extracted images or references needed by retrieved chunks;
- import instructions for Milvus;
- checksums for large artifacts when practical.

Do not commit raw Milvus runtime volumes as ordinary source files.
