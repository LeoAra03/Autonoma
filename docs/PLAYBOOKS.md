# Playbooks

A playbook is a reusable plan of work: a manifest plus a sequence of briefs.
Discovery assembles playbooks into a roadmap for the project in front of you,
so the same quality work is applied the same way every time.

## Anatomy

```
playbooks/
├─ universal/            applies to every project
│  ├─ secrets/manifest.json + 01.md, 02.md
│  ├─ license/manifest.json + 01.md, 02.md
│  ├─ readme/  ci/  tests/  threat-model/  runbook/  handover/
└─ archetypes/           applies when the project matches its markers
   ├─ web-app/  cli/  library/  api/  data/
```

The manifest:

```json
{
  "id": "secrets",
  "axis": "security",
  "universal": true,
  "stages": ["S1", "S2", "S3", "S4"],
  "briefs": ["first brief text", "second brief text"],
  "when": []
}
```

| Field | Meaning |
| --- | --- |
| `id` | Used as the phase id in the generated roadmap. |
| `axis` | The scoring axis the phase feeds. |
| `universal` | `true` for universal playbooks; they always apply. |
| `when` | Marker substrings for archetype playbooks (see [DISCOVERY.md](DISCOVERY.md)). |
| `stages` | The stages the playbook is meant to serve. |
| `briefs` | Ordered prompt texts; one brief is one message part. |

The `*.md` files next to the manifest are the same briefs as plain text — the
engine reads those files when it runs the phase, and discovery copies the
manifest's briefs into a generated plan. Keep both in sync.

## Universal playbooks

These apply to every project, in roughly this order:

| Playbook | Axis | What it does |
| --- | --- | --- |
| `secrets` | security | Find and remove secrets; keep the scanner gate green. |
| `license` | handover | LICENSE, NOTICE, third-party license compatibility. |
| `readme` | handover | A README a stranger can actually use. |
| `ci` | supplychain | CI that runs the full self-check on every change. |
| `tests` | reliability | A real test runner with a real green suite. |
| `threat-model` | safety | Threats, mitigations, and an honest risk register. |
| `runbook` | handover | Operational docs with a tested restore. |
| `handover` | handover | The full handover bundle. |

## Archetype playbooks

These attach to the shape of the project. Discovery matches the project's
stack markers against each manifest's `when` list:

| Playbook | Triggers on | Focus |
| --- | --- | --- |
| `web-app` | `web` (html/tsx/jsx) | Accessibility and keyboard interaction. |
| `cli` | `cli` (bin/cmd directories) | Flags, help text, meaningful exit codes. |
| `library` | node/python/go/rust/dotnet | Public API surface, versioning, changelog. |
| `api` | node/python/go/rust/dotnet | Endpoint contracts, status codes, auth on every route. |
| `data` | `data` (notebooks, dbt) | Data quality guards, pipeline documentation. |

Archetype playbooks can overlap — a Node web app matches `web-app`,
`library` and `api`. Discovery adds them all to the roadmap; trim the plan to
what the project actually needs before you run it.

## Writing a brief

A brief is a prompt. Write it the way you would hand work to a capable
engineer:

- Say what the outcome is, in plain language.
- Say how it will be checked — name the gate.
- Keep it to a size that survives prompt chunking (the engine splits prompts
  at line boundaries and repeats the exit contract at both ends, so a long
  brief is fine, but a rambling one is not).
- Never promise a result the gates cannot verify.

## Telemetry and rework

Every turn lands in `telemetry.jsonl`. `Get-WeakPlaybooks` groups it by phase
and reports the playbooks that fail far more often than they pass (default
threshold: a 60% red rate over at least five attempts). A playbook that keeps
producing reds is a playbook that needs rewriting — the problem is usually the
brief, not the project.
