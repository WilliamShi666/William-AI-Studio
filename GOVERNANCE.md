# Governance

William's AI Studio is prepared as a maintainer-led open-source project. This file describes how decisions are made for the first public release phase.

## Maintainers

The initial maintainer group is controlled by the repository owner. Replace the placeholder owner entries in `CODEOWNERS` after the public GitHub organization, repository, and maintainer handles are finalized.

Maintainers are responsible for:

- reviewing pull requests;
- triaging issues;
- protecting the security boundary described in `SECURITY.md`;
- keeping release artifacts aligned with `RELEASE_CHECKLIST.md`;
- deciding whether optional dependencies, datasets, model artifacts, or generated assets are safe to publish.

## Decision Process

For routine changes, maintainers can merge after review and required checks pass.

For high-impact changes, maintainers should document the decision in the pull request or a public design note. High-impact changes include:

- authentication, authorization, secrets, or admin endpoints;
- public dataset contents or provenance;
- dependency/license strategy;
- package names, release channels, or branding;
- Docker, network binding, sandbox, or local-control-plane behavior;
- changes that require a new migration or data import path.

## Release Gates

The first public release candidate must pass the gates in `RELEASE_CHECKLIST.md`.

Formal release actions require explicit owner approval:

- creating the clean public git repository;
- making the first public commit;
- adding a public remote;
- pushing to a public host;
- publishing release archives, Docker images, package artifacts, or dataset artifacts.

## Security

Security reports should follow `SECURITY.md`. Do not discuss live credentials, private data, or exploitable details in public issues.

If a real secret or private artifact is discovered in a candidate, stop the release process, remove the artifact, rotate or revoke the credential where applicable, and record sanitized evidence in `opensource_prep_reports/`.
