# Security Policy

## Supported versions

Security fixes are provided for the latest release tagged on [GitHub Releases](https://github.com/tizerluo/oh-my-cursor/releases). Pin installs to a release tag rather than an unverified `main` checkout.

## Reporting a vulnerability

**Preferred:** Use [GitHub Private Vulnerability Reporting](https://github.com/tizerluo/oh-my-cursor/security/advisories/new) for this repository.

**Fallback:** Email [tizerluo@gmail.com](mailto:tizerluo@gmail.com) with a description, reproduction steps, and impact assessment. Expect a response within 7 business days.

Please do not open public issues for undisclosed security vulnerabilities.

## Scope

This policy covers the oh-my-cursor MAP engine (hooks, install tooling, skills, and tests). Consumer-project code installed via `--project` is out of scope unless the vulnerability is in oh-my-cursor itself.

## Secret handling

MAP merge-gate markers rely on a local HMAC secret. See [docs/security.md](docs/security.md) for the trust contract and `omc doctor --security` verification.
