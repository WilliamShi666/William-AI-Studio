# Roadmap

This roadmap is intentionally conservative for the first public release. It avoids private commitments and focuses on making the source tree useful for local development and review.

## First Public Source Candidate

- Publish a clean source repository without private git history.
- Keep local defaults on `localhost`.
- Include public docs, contribution guidance, issue templates, PR template, and security policy.
- Include Roys Alpha, Claude Code UI, Roys Legion, and Roys Legion Demo Dataset tooling.
- Keep the first source candidate source-first: dataset tooling and placeholders are included, while large PDFs/images/chunks are packaged separately after curation.
- Exclude private env files, logs, runtime volumes, generated outputs, release bundles, and unreviewed model artifacts.
- Provide repeatable public-readiness checks for hygiene, secret scanning, dependency audit, builds, and backend smoke tests.

## Roys Alpha

- Stabilize local backend smoke setup.
- Improve dependency install experience for fresh Python 3.11 environments.
- Keep admin/debug endpoints protected by default.
- Continue replacing private deployment assumptions with local-first configuration.

## Claude Code UI

- Keep the public source buildable from the pnpm workspace.
- Document local-only usage and safe network binding assumptions.
- Rebuild desktop release assets from source instead of committing generated bundles.

## Roys Legion

- Document the full PDF-to-knowledge-base flow using OCR services, extracted images, chunks, manifests, and Milvus import tooling.
- Keep PyMuPDF-family dependencies as explicit AGPL/commercial opt-in paths.
- Provide a practical Roys Legion Demo Dataset import path for developers.
- Package the larger approved dataset through Git LFS, release artifacts, or external storage with checksums after the first source candidate is reviewed.

## Community

- Replace placeholder maintainer handles after the public repository is created.
- Add branch protection and required checks.
- Review public issues and contribution patterns before expanding release channels.
