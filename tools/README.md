# Tools

`verify.ps1` is the self-check CI runs before every push: Pester, the script
analyzer, plan integrity, gate definitions and documentation links.

The `*.mjs` helpers are the small, dependency-free command-line checkers the
gates in `config/gates/` call. They are intentionally per-project and short:
they read files, compare numbers, and exit non-zero when the check fails. A
missing input is always a failure — never a silent pass — so a gate can never
borrow a green it did not earn.

| Helper | What it checks |
| --- | --- |
| `coverage-floor.mjs` | Line coverage meets the required floor (`--min=N`). |
| `perf-budget.mjs` | Measured metrics stay within `--budget` limits. |
| `bundle-size.mjs` | Built JS files stay under a byte cap (`--max=N`). |
| `check-doc-sections.mjs` | A document has the required sections (`--require=a,b`). |
| `complexity.mjs` | File-level cyclomatic complexity stays under a cap (`--max=N`). |
| `clean-install.mjs` | The project installs from scratch via the lockfile. |
| `no-absolute-paths.mjs` | No hard-coded absolute paths in source and config. |
| `check-engines.mjs` | The running Node version satisfies `engines.node`. |
| `check-lockfile.mjs` | A valid, consistent lockfile exists. |
| `check-error-handling.mjs` | Entry points declare error handling. |
| `check-restore.mjs` | The runbook documents backup and restore. |
| `check-handover.mjs` | The full handover bundle is present and substantial. |
| `check-adr.mjs` | At least `--min=N` architecture decision records exist. |
| `check-provenance.mjs` | CI provenance reaches the required SLSA level. |

Each helper prints a one-line result on success and a specific reason on
failure, so a red gate tells you exactly what to fix.
