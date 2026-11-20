# Architecture

Autonoma is a small, dependency-free director that sits between a chat agent
and a project. The agent does the work; Autonoma decides whether the work
counts.

The design follows one rule: **the director never trusts a claim it cannot
verify with an external command.** Everything else — the scorecard, the
ladder, the ledger, the brakes — is scaffolding around that rule.

## The shape of the system

```
autonoma.ps1          entry point, flags, thin wiring
Autonoma.psm1         module: dot-sources every internal module
config/               scorecard, stages, gates, site adapters
plans/                roadmaps (id|axis|depends|type|title|criterion) + briefs
src/internal/
  Config              defaults, file + env + flag precedence, config loading
  Plan                roadmap parsing, brief loading, dry-run
  Graph               dependency graph: readiness, selection, cycle detection
  State               per-plan run state, atomic saves
  Ledger              append-only, chained ledger of gate and phase verdicts
  Gate                gate discovery, glob matching, execution, timeout, artifacts
  Score               axis scores, global scorecard, floors, regression compare
  Budget              turn / wall-clock / regression brakes
  Prompt              exit contract, prompt chunking, phase and audit prompts
  Verdict             parsing the agent's contract lines, acceptance rules
  Discovery           stack profiling, risk register, plan generation
  Playbook            playbook loading, telemetry, weak-playbook detection
  Handover            handover bundle checks
  Cdp                 raw Chrome DevTools Protocol transport
  Browser             browser discovery and launch with an isolated profile
  Page                page state, composer control, card handling, message send
  Engine              the run loop: poll, send, read verdict, gate, advance
src/js/               browser-side helpers injected into the page
tools/                verify.ps1 self-check + dependency-free gate helpers
playbooks/            universal and archetype work plans
```

## The run loop

The engine is a single loop that polls the chat page and reacts to what it
finds:

1. **Check the brakes.** If a budget is spent (turns, wall clock, or
   regressions), the run stops and the state is saved.
2. **Attach to the page.** Connect to the browser's debug endpoint, or launch
   the browser in an isolated profile if nothing is listening.
3. **Read the page state.** The injected `state.js` helper reports whether
   there is an editor, whether the agent is busy, whether a "continue" card is
   shown, whether a CAPTCHA is present, and the tail of the page text.
4. **Handle the exceptions first.** A CAPTCHA pauses the run with a beep and
   resumes automatically once it is gone. A "continue" card is clicked, or
   held when the card budget is spent or the run is in dry (`seco`) mode.
5. **Wait for quiescence.** The page must stop growing and the agent must
   stop reporting busy for several polls before a verdict is read. This is
   what keeps Autonoma from reading a half-finished answer as a result.
6. **Read the verdict.** The tail of the page is scanned for the exit contract
   (`ESTADO`, `EVIDENCIA`, `GATES`, `HECHO`, `SIN HALLAZGOS`).
7. **Complete the turn.** For multi-part phases, advance or retry the part.
   For finished claims, run the gates for the phase's axis — in a fresh
   process, with a timeout, never in the agent's context.
8. **Advance or roll back.** A blocker gate or a score regression marks the
   phase red and rolls the tree back to the SHA recorded before the run.
   A clean claim updates the ledger, and — if the floors for the next stage
   are met — the stage advances.

The loop repeats until the plan is exhausted, the target stage is reached, or
a brake trips.

## Key decisions

**The verdict is falsifiable.** The agent claims; the gates decide. A green
with no evidence is treated as red. A green whose gates fail is red, and the
tree rolls back. There is no code path that advances on prose alone.

**Skipped is not passed.** A gate whose tool is missing is *skipped*, and
skipped gates are excluded from both the numerator and the denominator of the
axis score. A missing tool lowers coverage; it never earns a pass.

**The ledger is the audit trail.** Every gate result and every phase verdict
is appended to `ledger.jsonl` with the hash of the previous entry. Anyone can
re-verify the chain with `--verify-ledger`; anyone who edits a green by hand
breaks it visibly.

**Floors, not averages.** A stage is not reached by a high global score. Every
axis in the stage's floor table must clear its own threshold, so one strong
axis cannot carry a weak one past the line.

**No LLM in the director.** Autonoma contains no model. It compares numbers
and counts. That is why it is fast, cheap to run, and auditable — and why the
agent on the other end can be a human, a model, or anything that can type.

## Where to look next

- [SCORING.md](SCORING.md) — how the scorecard and the floors work.
- [GATES.md](GATES.md) — gate anatomy and how to add one.
- [OPERATIONS.md](OPERATIONS.md) — configuration, state, logs and telemetry.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — when a run does not behave.
