# Support

William's AI Studio is being prepared for public release. Support channels and release guarantees are still being finalized.

## Where To Start

For project orientation, read:

- `README.md` for the public overview;
- `ARCHITECTURE.md` for service boundaries and data flow;
- `CONTRIBUTING.md` for contribution rules;
- `SECURITY.md` for security reporting;
- `opensource_prep_reports/` for current release-readiness evidence.

## Getting Help

After the public repository is created, normal usage questions should use the repository's public issue tracker or discussions area if enabled.

Before the first public release, setup commands and fresh-clone verification are still being finalized. If a documented command does not work, include:

- the product area: Roys Alpha, Claude Code UI, or Roys Legion;
- operating system and runtime versions;
- the command you ran;
- the error output with secrets removed;
- whether you changed any `.env` values.

## Security Issues

Do not file public support requests for secrets, authentication bypasses, private data exposure, or unsafe service exposure. Follow `SECURITY.md`.

## Development Expectations

The public setup is local-first:

- use `localhost` defaults;
- copy `.env.example` to `.env` locally;
- provide your own API keys;
- do not commit logs, generated outputs, database volumes, or model blobs.

Roys Legion full workflow support depends on Milvus plus the OCR/model services documented during open-source preparation.
