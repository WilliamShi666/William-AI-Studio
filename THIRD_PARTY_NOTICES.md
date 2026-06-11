# Third-Party Notices

This file is a public draft for William's AI Studio. It is not complete until the final public release candidate has a reviewed SBOM and package-manager license reports.

## Project License

Original William's AI Studio additions are licensed under the Apache License 2.0. See `LICENSE`.

Some source areas include code, structure, or implementation ideas derived from upstream projects and remain subject to their upstream licenses and notices.

### Suna / kortix-ai

Roys Alpha (`WilliamManus/`) is a secondary development based on [`kortix-ai/suna`](https://github.com/kortix-ai/suna).

At the time of this preparation pass, the upstream Suna repository publishes its license as Elastic License 2.0. Suna-derived portions of Roys Alpha should therefore be treated as subject to the applicable upstream Suna license terms, not relicensed solely by the root Apache-2.0 license.

Upstream repository:

<https://github.com/kortix-ai/suna>

Upstream license file:

<https://github.com/kortix-ai/suna/blob/main/LICENSE>

## Dependency Notices

This repository uses third-party open-source dependencies across Roys Alpha, Claude Code UI, and Roys Legion. The final dependency list should be generated from the release candidate SBOM.

Current local SBOM precheck:

- Candidate: `/tmp/williams-ai-studio-public-candidate-20260610-oss-dry-run-v6p-clean-20260611-deps-nginx`
- SBOM format: CycloneDX JSON
- SBOM path in preparation environment: `/tmp/sbom-v6p-clean-20260611-deps-nginx.cdx.json`
- Component count: `2112`

The SBOM artifact is not committed in this draft pass.

## High-Priority License Notes

### PyMuPDF / MuPDF

Roys Legion includes optional code paths that can use PyMuPDF, PyMuPDF4LLM, and pdf4llm for fast PDF extraction or PDF rendering.

PyMuPDF and MuPDF are distributed under AGPL or commercial license terms. To keep the default Apache-2.0 public install path clear of automatic AGPL dependency installation, these packages are excluded from the default backend requirements and are listed only in opt-in files:

- `multimodalrag/backend/requirements-pdf-agpl.txt`
- `multimodalrag/backend/Information-Extraction/deepseekocr/requirements-pdf-agpl.txt`

Install those optional files only if the use case complies with AGPL obligations or an appropriate commercial license is available.

Official licensing reference:

<https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright>

### sharp / libvips

The local SBOM and npm lockfiles include `sharp` platform packages and `@img/sharp-libvips-*` packages. The libvips packages are listed with `LGPL-3.0-or-later`.

These dependencies should be included in the final SBOM and notices, especially if generated bundles or desktop/server artifacts include native binaries.

### jszip

The npm lockfile includes `jszip` with the license expression:

```text
(MIT OR GPL-3.0-or-later)
```

The project should document the selected MIT side of the dual license in the final notices if jszip remains in the release candidate.

## Model And Dataset Artifacts

OCR/model services such as MinerU, DeepSeek OCR, and PaddleOCR/PaddleOCR-VL should not be treated as ordinary source dependencies unless their model files or service code are included in the public release.

Roys Legion Demo Dataset assets must include their own manifest, provenance, and license notes before publication.

## Finalization Checklist

Before public release:

- regenerate the SBOM from the final clean public candidate;
- run npm/pnpm and Python license tooling where possible;
- review GPL/AGPL/LGPL and commercial-license dependencies;
- add any required copyright notices;
- regenerate SBOM and license reports from the final candidate and confirm default installs do not pull PyMuPDF-family dependencies;
- document optional PyMuPDF-family dependencies as AGPL/commercial opt-in dependencies;
- update this file with final dependency notice coverage.
