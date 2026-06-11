# Security Policy

William's AI Studio is being prepared for a first public release. This policy defines the public security boundary and the expected process for vulnerability reports.

## Supported Versions

The project has not shipped a stable public release yet.

| Version | Supported |
|---------|-----------|
| Public preparation branch | Security fixes accepted |
| Private development history | Not supported for public use |

After the first public release, this file should be updated with supported release branches or tags.

## Reporting a Vulnerability

Do not open a public issue for vulnerabilities that expose credentials, private data, authentication bypasses, remote code execution, unsafe file access, or service-to-service trust problems.

Until a public security contact is finalized, report vulnerabilities privately to the repository owner or maintainers through their preferred private channel. Include:

- affected path, endpoint, package, or service;
- reproduction steps;
- expected impact;
- whether credentials, user data, model outputs, files, or runtime logs may be exposed;
- any suggested fix, if known.

Maintainers should acknowledge the report privately, triage severity, fix critical issues before public disclosure, and credit reporters when appropriate.

## Public Repository Security Boundary

The public repository must not include:

- real API keys, provider tokens, passwords, cookies, JWT secrets, refresh tokens, or database credentials;
- private `.env` files;
- runtime logs, session captures, browser traces, observability traces, or generated run state;
- private git history;
- local Milvus, MinIO, etcd, Redis, or database runtime volumes;
- unreviewed generated documents, screenshots, uploads, downloads, or model outputs;
- production server IPs, private hostnames, or private deployment credentials.

Sanitized `.env.example` files may include empty values, placeholders, and localhost defaults. Developers must provide their own credentials locally.

## Secrets And Credentials

If a real credential is found in source, docs, examples, tests, logs, or a release artifact:

1. Treat it as compromised.
2. Remove it from the public candidate.
3. Rotate or revoke the credential in the provider dashboard.
4. Search for similar leaks.
5. Record remediation evidence in the open-source preparation reports without copying the secret value.

History rewriting is not the default release strategy. The intended public release path is a clean export repository that does not import private git history.

## Local Development Defaults

Public defaults should be local-first:

- bind development services to `localhost` by default;
- require explicit configuration before listening on public interfaces;
- keep admin and debug endpoints protected;
- fail closed when required secrets such as JWT signing keys are missing or weak.

Redis is primarily used as an internal message bus for streaming and pub/sub behavior. It is not required for every documentation or security validation pass, but any public runtime guide that enables Redis should document its local-only default.

## Roys Legion Data And Models

Roys Legion may publish approved PDFs, extracted images, chunks, metadata, embeddings, and import scripts as the Roys Legion Demo Dataset. Public datasets and optional release artifacts must be reviewed before publication.

Raw local Milvus volumes and model server directories are not automatically safe to publish. Prefer reproducible import scripts, manifests, checksums, and documented model download links over committing raw service state or model blobs.

## Dependency And Supply Chain Review

Before the first public release candidate, run and record:

- third-party secret scanning against the clean public candidate;
- dependency license review and SBOM generation;
- artifact denylist checks;
- fresh-clone install/build/smoke verification for the released projects.

Known dependency and provenance questions should be tracked in `opensource_prep_reports/progress_tracker.md`.

## Security Changes

Security-sensitive changes should include:

- a short risk summary;
- affected services or endpoints;
- verification steps;
- confirmation that no new secrets, logs, or generated artifacts were added.

Do not paste secrets, full tokens, cookies, private request captures, or vulnerable production URLs into commits, issues, pull requests, or preparation reports.
