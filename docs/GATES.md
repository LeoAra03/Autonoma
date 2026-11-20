# Gates

A gate is the smallest unit of proof in Autonoma: one command, one
expectation, one severity, one weight. Gates are the reason a claim can be
falsified — they run in their own process, with a timeout, outside the agent's
context, and they either exit zero or they do not.

## Anatomy

Gates live in [`config/gates/`](../config/gates/), one file per axis. Each
file declares its axis and a list of gates:

```json
{
  "axis": "security",
  "gates": [
    {
      "id": "sec-secrets",
      "cmd": "gitleaks detect --no-banner --redact --exit-code 1",
      "expect": { "exit": 0 },
      "severity": "blocker",
      "weight": 35,
      "when": ["**/*"],
      "timeout": 180
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `id` | Unique identifier. Duplicate ids fail the self-check. |
| `cmd` | The command to run. The first token must be an executable on `PATH`. |
| `expect` | What counts as success: `exit` (default 0), and optionally a `json` check. |
| `severity` | `blocker`, `major` or `minor`. A failed blocker fails the phase outright. |
| `weight` | How much the gate contributes to its axis score. |
| `when` | Globs of files that must exist for the gate to apply. |
| `timeout` | Seconds. A gate that runs longer is killed and marked failed. |
| `artifact` | Optional: a filename the gate writes its output to, recorded by hash in the ledger. |

## How a gate is selected and run

1. **Discovery.** `Get-GateDefinition` loads every gate in `config/gates/`.
2. **Applicability.** A gate applies when at least one file in the project
   matches one of its `when` globs. Globs support `**`, `*`, `?` and `{a,b}`
   brace expansion, and are matched against the project's tracked files (the
   git index when there is one).
3. **Execution.** `Invoke-Gate` runs the command in a fresh `powershell.exe`
   process with the project root as its working directory, stdout and stderr
   captured to the artifact directory, and a hard timeout.
4. **Verdict.** The gate passes when the exit code matches `expect.exit` and,
   if present, the JSON check passes. Otherwise it fails. If the executable is
   not on `PATH`, the gate is **skipped** — which is a different verdict, and
   it lowers the axis score rather than faking a pass.

## Tokens

Two tokens are substituted in `cmd` before the gate runs:

- `{lockfile}` — replaced with the project's first existing lockfile
  (`package-lock.json`, `poetry.lock`, `Cargo.lock`, `go.sum`).
- `{artifact}` — replaced with the full path of the artifact file the gate
  writes.

## JSON expectations

Some gates check the *content* of the output, not just the exit code:

```json
"expect": { "exit": 0, "json": { "path": "results", "max": 0 } }
```

The tool walks the dotted path into the gate's JSON output and fails when the
value at that path holds more than `max` items. `osv-scanner` uses this to
fail on any known-vulnerability finding.

## Severity

| Severity | Effect |
| --- | --- |
| `blocker` | Fails the phase regardless of the score. The phase is red and the tree rolls back. |
| `major` | Fails the gate and lowers the axis score; the phase can still pass if no blocker failed. |
| `minor` | A softer signal: the gate result still counts against the axis score. |

## The gate helpers

Several gates call the small dependency-free Node scripts in
[`tools/`](../tools/README.md). They read files, compare numbers, and exit
non-zero when the check fails. A missing input — a coverage report, a metrics
file, a build output — is always a failure, never a silent pass, so a gate can
never borrow a green it did not earn.

## Adding a gate

1. Pick the axis file in `config/gates/`; add a new gate object.
2. Make sure `when` matches real files. A gate that never matches is always
   skipped, and skipped gates count against you.
3. If the gate produces output worth auditing, set `artifact`.
4. Run `./tools/verify.ps1` — it rejects duplicate ids and incomplete
   definitions.
5. Watch the gate run once with the log at debug level to confirm the exit
   code and the JSON check behave as intended.
