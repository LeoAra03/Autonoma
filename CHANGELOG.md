# Changelog

All notable changes to Autonoma are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-22

### Added

- **Delivery governance framing**: README repositioned as infrastructure automation with external
  verification and supply-chain evidence; ISO/IEC 25010:2023 plus the five Azure Well-Architected
  pillars on one scorecard.
- **docs/frameworks.md**: axis-to-framework mapping with the rule that an axis without a mapping
  does not count toward framework conformance.
- **docs/ai-workflow.md**: rejection log for proposed-but-declined work (LLM self-grading, embedded
  heuristics, `--emit-sarif`, `verify.ps1 -Platform`).
- **demo.ps1 / demo.sh**: entry points to run the self-check on Windows PowerShell and Linux pwsh.
- **CONTRIBUTIONS.md**: extended with cross-platform notes (`-Platform`, `--emit-sarif`, pwsh on
  Linux).

### Changed

- README top block now covers purpose, "Why it exists", "What it is and what it is not" and the
  two-framework scorecard; badges updated.

## [1.0.0] - 2026-11-20

### Added

- **Deterministic work director** (`src/autonoma.ps1`, `src/Autonoma.psm1`) that drives a web chat
  agent through a dependency-ordered plan of quality work.
- **Stage ladder** (S0–S4) with per-axis floors, configured in `config/stages.json`.
- **Scorecard on ISO/IEC 25010:2023**: eleven axes (nine standard characteristics plus supply chain
  and handover), weighted in `config/scorecard.json`. Skipped gates are excluded from the score, so
  a missing tool can never be claimed as a pass.
- **Gate engine**: gates are plain commands with an expectation, a severity and a weight, matched by
  glob in `config/gates/`. A `blocker` fails the phase regardless of the total score.
- **Tamper-evident ledger**: `ledger.jsonl` chains every entry to the previous hash and is checked
  with `--verify-ledger`.
- **Budgets and brakes**: `limits.turns`, `limits.wallMinutes`, `limits.maxRegressions`, with
  automatic rollback on score regression. The SHA of the tree is recorded on the first turn, so
  every rollback has a real anchor to return to.
- **Discovery mode** (`--discover`): profiles the target project, writes a risk register, picks the
  applicable playbooks and generates a roadmap plus briefs.
- **Site adapters** for generic chat UIs and the Arena editor, configured in `config/sites/`.
- **Browser driving over raw CDP**: isolated profile, captcha pause-and-resume, keyboard-first
  message sending, "continue" card handling.
- **Playbook library**: universal playbooks (secrets, license, README, CI, tests, threat model,
  runbook, handover) and archetype playbooks (web app, CLI, library, API, data).
- **Self-check suite**: Pester tests, `tools/verify.ps1`, and a CI workflow that runs them on every
  push and pull request.
- **Documentation**: `docs/` covering architecture, scoring, gates, playbooks, discovery,
  operations, troubleshooting and legal, plus an ADR series.
