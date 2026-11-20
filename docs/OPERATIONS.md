# Operations

Everything you need to run Autonoma day to day: configuration precedence,
every flag, where state lives, and what the runtime files mean.

## Configuration precedence

Lowest to highest:

1. **Built-in defaults** (see `Get-DefaultConfig` in
   [`src/internal/Config.ps1`](../src/internal/Config.ps1)).
2. **`config/autonoma.json`** — copy `config/autonoma.example.json` and edit it.
3. **Environment variables** — `AUTONOMA_URL`, `AUTONOMA_SITE`,
   `AUTONOMA_PLAN`, `AUTONOMA_PROFILE`, `AUTONOMA_PORT`, `AUTONOMA_STAGE`.
4. **Command-line flags** — the final word.

## Configuration reference

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | — | The chat URL to drive. |
| `site` | `generic` | Site adapter: `generic` or `arena`. |
| `port` | `9222` | Chromium remote-debugging port. |
| `plan` | — | Roadmap path, relative to the project root. |
| `targetStage` | `S4` | The stage the ladder aims for. |
| `seco` | `false` | Dry-hold mode: the run polls and sends, but never clicks "continue" cards. |
| `beep` | `true` | Terminal beeps on warnings (disable with `-NoBeep`). |
| `browser.channel` | `auto` | `auto`, `chrome`, `edge`, `brave`, `chromium`, `thorium`. |
| `browser.profile` | isolated dir | Browser profile directory; keep it isolated. |
| `browser.headless` | `false` | Run the browser headless. |
| `browser.args` | `[]` | Extra browser arguments. |
| `timing.quiet` | `10` | Seconds the page must be quiet before a verdict is read. |
| `timing.poll` | `3` | Seconds between page polls. |
| `timing.watchdog` | `8` | Minutes without a turn before the engine pushes a nudge. |
| `timing.verdictForce` | `5` | Minutes a stuck verdict is waited out before it is forced. |
| `timing.sendSettle` | `1600` | Milliseconds to wait after sending before checking the composer. |
| `limits.turns` | `0` | Maximum turns; `0` means unlimited. |
| `limits.cards` | `40` | Maximum "continue" cards clicked. |
| `limits.redsPerPhase` | `3` | Reds before a phase is parked. |
| `limits.deepRetries` | `8` | Deep retries for a parked phase before it stays parked. |
| `limits.cleanAudits` | `3` | Clean perfection rounds required to finish audits. |
| `limits.maxPromptChars` | `9000` | Prompt chunk size. |
| `limits.chunkDelay` | `1200` | Milliseconds between prompt chunks. |
| `limits.wallMinutes` | `0` | Wall-clock budget; `0` means unlimited. |
| `limits.maxRegressions` | `3` | Consecutive regressions that stop the run. |
| `limits.rollbackOnRegression` | `true` | Roll the tree back on a score regression. |
| `contract.header` | `true` | Echo the exit contract at the top of a closing prompt. |
| `audits` | form/route/evidence sweep | Perfection-round titles. |

## Command line

```powershell
bin\autonoma.cmd -Url <url> -Plan <roadmap>    # run the plan
bin\autonoma.cmd --discover -Url <url>         # generate a plan
bin\autonoma.cmd -Plan <roadmap> --dry-run     # print the plan, send nothing
bin\autonoma.cmd -Plan <roadmap> --list        # list phases with deps and parts
bin\autonoma.cmd -Plan <roadmap> --score       # print the current scorecard
bin\autonoma.cmd -Plan <roadmap> --stage       # print the current stage
bin\autonoma.cmd -Plan <roadmap> --verify-ledger  # recompute the ledger chain
bin\autonoma.cmd -Plan <roadmap> --reset       # clear the run state
bin\autonoma.cmd -Plan <roadmap> --headless    # run the browser headless
bin\autonoma.cmd -Version
```

## Where things live

Everything runtime-related stays under `%LOCALAPPDATA%\Autonoma\`:

| Path | Contents |
| --- | --- |
| `state/ledger.jsonl` | The chained ledger. Append-only. |
| `state/telemetry.jsonl` | One line per turn: phase, verdict, cause, score. |
| `state/gates/<phase>-<time>/` | Gate output artifacts, per run. |
| `state/<plan>-<hash>.json` | The run state (stage, phase states, parts, passes). |
| `state/handover.json` | Last handover bundle report. |
| `logs/autonoma-*.log` | Run logs; each log rotates to `.1.log` past 4 MB. |
| `profile/` | The isolated browser profile. |

State is keyed by the plan path *and its content hash*: edit the roadmap and
you get a fresh run, not a half-finished one.

## The exit contract

Every acceptance must end with:

```text
ESTADO: verde | rojo | bloqueado
EVIDENCIA: paths and real command output
GATES: ids of the gates you made pass
```

- `verde` with no evidence is treated as `rojo`.
- `bloqueado` parks the phase — it is for cases where continuing is the wrong
  move, not for failures.
- `HECHO: ...` closes a multi-part phase, and `SIN HALLAZGOS` closes a
  perfection round with nothing left to find.

The contract is repeated at both ends of every closing prompt, so prompt
chunking can never eat it.

## Brakes and incidents

The run stops on:

- `limits.turns` — the turn budget is spent;
- `limits.wallMinutes` — the wall-clock budget is spent;
- `limits.maxRegressions` — too many consecutive score regressions.

Regressions also roll the tree back to the SHA recorded before the run, so a
bad phase cannot leave the project in a worse state than it started. The
incident note is in the telemetry and the log.

## Self-check

`tools/verify.ps1` is the gate before every push — Pester, the script
analyzer, plan integrity, gate definitions and documentation links. It is the
same check CI runs.
