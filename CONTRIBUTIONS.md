# Contributing to Autonoma

Thanks for taking the time. Autonoma is small on purpose: a deterministic core, external gates, and
no dependencies beyond PowerShell. Keep it that way.

## Ground rules

- **The director must stay deterministic.** The engine compares numbers and counts. If a change
  makes Autonoma judge its own claims, it is the wrong change.
- **Gates stay external.** A check that lives inside the agent's context can be talked out of. A check
  that runs in its own process and exits non-zero cannot.
- **Nothing advances on prose.** If a phase can be "passed" without running a command, the phase is
  not defined well enough yet.

## Development setup

You need PowerShell 5.1 or 7+. `verify.ps1` is written for PowerShell 7, which runs on Windows and
Linux, so you can develop on either platform.

```powershell
git clone https://github.com/LeoAra03/Autonoma
cd Autonoma

# Pester and the analyzer, once per machine
Install-Module Pester -MinimumVersion 5.5.0 -Force -Scope CurrentUser
Install-Module PSScriptAnalyzer -Force -Scope CurrentUser
```

## Before every push

Run the same self-check CI runs:

```powershell
./tools/verify.ps1
```

It runs the Pester suite, the script analyzer, the plan integrity check, the gate definition check
and the documentation link checker. If it is not green, do not push.

## Cross-platform notes

- `verify.ps1` accepts a `-Platform` switch (`Windows` or `Linux`) so path separators, browser
  profiles and gate expectations are explicit per target instead of guessed from the host.
- `--emit-sarif` writes every failed gate as a SARIF result, so failures surface in the same pane
  as code scanning instead of only in the ledger.
- CI gates on `windows-latest`; Linux parity via `pwsh` on `ubuntu-latest` is the target for the
  next release cycle.

## Coding standards

- `Set-StrictMode -Version 2.0` and `$ErrorActionPreference = 'Stop'` in every entry point and module.
- Keep functions small and named for what they do: `Test-`, `Get-`, `Invoke-`, `New-`, `Add-`.
- Fail loudly on invalid input. A silent default that pretends success is worse than an exception.
- Comments explain *why*, not *what*. The code should be readable on its own.
- Keep the append-only ledger append-only, and keep the exit contract lines exactly as specified.

## Adding a gate

1. Add a gate object to the right file under `config/gates/<axis>.json`. Every gate needs `id`,
   `cmd`, `expect`, `severity`, `weight`, `when` and `timeout`.
2. Make the `when` glob match real files — gates that never match are skipped, and skipped gates
   lower the axis score rather than inflating it.
3. If the gate produces an artifact, name it with `artifact` so its hash lands in the ledger.
4. Run `./tools/verify.ps1`. It rejects duplicate gate ids and incomplete definitions.

## Adding a playbook

1. Create `playbooks/<name>/manifest.json` with `id`, `axis`, `stages`, `briefs`, `when` (marker
   substrings for archetype playbooks) and `universal` (for the ones that apply everywhere).
2. Write the briefs as Markdown files in the same directory, ordered `01.md`, `02.md`, … Each brief
   becomes one message part the agent receives.
3. Run `bin\\autonoma.cmd --discover` against a project that matches the playbook and confirm it is
   picked up.

## Pull requests

- One logical change per pull request.
- Include a short description of the problem, the approach, and how it was verified.
- The PR template will remind you to run `tools/verify.ps1` and paste the result.
