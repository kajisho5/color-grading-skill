# Security Policy

## Reporting a Vulnerability

Please **do not** open a public issue for a security vulnerability.

Report it privately through GitHub's private vulnerability reporting:

1. Go to the [Security tab](../../security) of this repository.
2. Click **Advisories**, then **Report a vulnerability**.
3. Describe the issue, its impact, and steps to reproduce it.

This opens a private discussion with the maintainers so the issue can be assessed and fixed before
any public disclosure. See [GitHub's documentation on private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) for details.

## Scope

This project (`color-grading-skill`) is a local execution tool: it runs no network service and holds
no user accounts or credentials. Its documented security boundary -- what it does and does not
defend against (path handling, subprocess argument construction, accepted input shapes) -- is
described in [docs/security.md](docs/security.md). A report about behavior outside that documented
boundary is still welcome; it helps clarify where the boundary should be.

## Supported Versions

This project does not yet maintain multiple release branches. Security fixes are made against the
latest release.
