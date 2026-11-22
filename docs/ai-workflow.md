# AI Workflow — Rejection Log

Logs items proposed for Autonoma that were rejected, with the decision and rationale. This keeps the
project deterministic and scoped: the director must never grade its own claims, and every verdict
comes from an external CLI gate exiting zero.

| Item | Decision | Rationale | Date |
| --- | --- | --- | --- |
| LLM-based self-grading | Rejected | The agent must never grade its own homework; every verdict comes from an external CLI gate exiting zero. | 2026-11-20 |
| Embedded scoring heuristics | Rejected | Scoring is defined by `config/scorecard.json` and `config/stages.json`, not by the model. | 2026-11-20 |
| `--emit-sarif` SARIF emitter | Rejected (v0.2.0) | Deferred: gate output is already line-based; SARIF emission can be added as a thin reporter layer without changing gate contracts. | 2026-11-21 |
| `verify.ps1 -Platform` switch | Rejected (v0.2.0) | Deferred: keep `verify.ps1` single-platform and deterministic; cross-platform parity is handled at the CI matrix level instead. | 2026-11-21 |

## How to read this log

- **Rejected** items are intentionally out of scope for Autonoma.
- **Rejected (v0.2.0)** items are deferred, not dropped: they are tracked for the next release and
  may be revisited once the core gate contract is stable.
