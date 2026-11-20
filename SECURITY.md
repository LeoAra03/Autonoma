# Security Policy

## Supported versions

Autonoma is a young, dependency-free project. Security fixes are provided for
the current release line.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

## Reporting a vulnerability

Please report suspected security issues **privately**, not as a public issue.

Use GitHub's private vulnerability reporting
([private vulnerability reports](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-vulnerability))
on this repository, or contact the maintainer directly. Do not open a public
issue for a vulnerability until it has been fixed and a release has shipped.

Include as much as you can:

- the version affected,
- the problem and its impact,
- reproduction steps or a proof of concept,
- any suggested fix.

You should receive a response within a few business days. We will acknowledge the
report, confirm the scope, and coordinate a fix and disclosure timeline with you.

## What Autonoma does by design

- **No telemetry.** Nothing is sent anywhere. The tool talks to the local
  Chromium debug endpoint (`127.0.0.1`) and to the chat URL you give it.
- **No credentials stored.** Autonoma never reads or writes keys, tokens, or
  login state for the sites it drives. The browser runs in its own isolated
  profile under `%LOCALAPPDATA%\Autonoma\profile`, separate from your daily
  browser.
- **External gates.** Every quality claim is verified by a command that runs in
  its own process, with a timeout, outside the agent's context. A model cannot
  talk its way past `exit 1`.
- **Append-only ledger.** Every verdict is chained to the previous one and can
  be re-verified with `--verify-ledger`. A hand-edited "green" is visible.
- **Rollback on regression.** A score regression rolls the tree back to the
  SHA recorded before the run.

## What Autonoma does not do

- It is **not** a security scanner, and it is **not** legal advice. "Legal"
  gates mean license compatibility and CVE checks against the dependency
  manifest, nothing more.
- It does **not** bundle secret scanners, SBOM tools, or SAST engines. It calls
  the tools you install. Keep those tools pinned and up to date, because the
  gates are only as good as the tools behind them.
- It cannot protect you from a malicious *target project*. Autonoma drives a
  chat session; treat the projects and URLs you point it at with the same
  care you would a shell.

## Hardening your installation

- Run the latest release.
- Keep the gate tools (`gitleaks`, `osv-scanner`, `semgrep`, `syft`, …) pinned
  in CI so gate results are reproducible.
- Keep `--verify-ledger` in your acceptance routine. A broken chain is a red
  run, by design.
