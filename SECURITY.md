# Security policy

## Supported versions

Security fixes go into the latest release on `main`. Older versions are not
patched; [upgrade](docs/operations.md#upgrade-and-rollback) to receive fixes.

## Report a vulnerability

Do not open a public issue. Report privately through GitHub:
**Security → Report a vulnerability** on the
[TGDrive repository](https://github.com/TechShreyash/TGDrive/security/advisories/new).

Include the affected version, steps to reproduce and the impact. Never include
real bot tokens, session strings, `ENCRYPTION_KEY` values or private files.

## Scope

TGDrive's own code and default deployment files are in scope. Telegram,
MongoDB, Docker and reverse proxies are out of scope unless TGDrive configures
them insecurely. The current protections are described in
[Security and privacy](docs/reference.md#security-and-privacy).
