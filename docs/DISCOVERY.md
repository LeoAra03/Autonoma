# Discovery

`--discover` turns a project into a plan. Instead of writing a roadmap by
hand, you point Autonoma at the chat and it profiles the project, writes a
risk register, picks the playbooks that fit, and generates a ready-to-run
roadmap with briefs.

```powershell
bin\autonoma.cmd --discover -Url "https://site/chat/1"
```

The generated plan lands in `plans/discovered-<timestamp>/`:

```
plans/discovered-20261120-120000/
├─ roadmap                    id|axis|depends|type|title|criterion
├─ RISKS.md                   the risk register
├─ scorecard.baseline.json   starting scorecard
└─ briefs/<phase-id>/01.md    one file per brief
```

## How it profiles the project

Discovery looks at the project's tracked files and checks for stack markers:

| Marker | Matches |
| --- | --- |
| `node` | `package.json` |
| `python` | `pyproject.toml`, `requirements.txt` |
| `go` | `go.mod` |
| `rust` | `Cargo.toml` |
| `dotnet` | `*.csproj`, `*.sln` |
| `web` | `**/*.html`, `**/*.tsx`, `**/*.jsx` |
| `cli` | `**/bin/**`, `**/cmd/**` |
| `data` | `**/*.ipynb`, `**/dbt_project.yml` |

It also records whether the project has a test runner (files matching
`test` or `spec`), CI (`.github/workflows/*`), a license, a README, and which
lockfiles are present.

## The risk register

Discovery writes `RISKS.md` with the risks it can already see, before any work
has happened:

- **HIGH** — no test runner: nothing can be gated on behaviour.
- **HIGH** — no CI: gates will only ever run locally.
- **HIGH** — Node project without a lockfile: the dependency set is not
  reproducible.
- **MEDIUM** — no license file: redistribution is unclear.
- **MEDIUM** — no README: no entry point for a stranger.

The register is the starting conversation, not a verdict. Fixing the risks is
exactly what the generated plan is for.

## Picking playbooks

- **Universal** playbooks (`universal: true`) always apply.
- **Archetype** playbooks apply when any stack marker appears in any of their
  `when` patterns (see [PLAYBOOKS.md](PLAYBOOKS.md)).

The selected playbooks become phases on a linear roadmap — each phase depends
on the previous one — and each playbook's briefs become the phase's brief
files, in order. The baseline scorecard is written alongside, so the first
green has something to be measured against.

## After discovery

Read the generated roadmap and the risk register before you run anything.
Trim phases the project does not need, reorder anything that does not match
the project's reality, and rewrite briefs that do not fit. Discovery gets you
80% of the way; the last 20% is the part where you decide what "professional"
means for this particular project.

Then run it:

```powershell
bin\autonoma.cmd -Url "https://site/chat/1" -Plan plans\discovered-20261120-120000\roadmap
```
