# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately
through GitHub: **Security → Report a vulnerability** on this repository.

Include what you found, how to reproduce it, and the impact you expect. We aim
to acknowledge a report within 5 business days and to keep you updated until
it is resolved. Once a fix ships, we credit reporters in the advisory unless
you ask us not to.

## Supported versions

Argus is pre-1.0. Only the latest release receives security fixes; there are
no backports, and a fix ships as the next patch from `main`
([releases](docs/specs/releases.spec.md)).

## Scope

Argus is a self-hosted, single-node control plane that runs agent workers.
The intended security model is described in:

- [Security hardening](docs/specs/security-hardening.spec.md)
- [Prompt-data isolation](docs/specs/prompt-data-isolation.spec.md)
- [Codex worker sandbox profile](docs/specs/codex-sandbox-profile.spec.md)

Behaviour those specs list as a non-goal is known and out of scope, but reports
that show it is more dangerous than assumed are welcome.
