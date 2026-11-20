# Scoring

Autonoma measures a project along eleven axes and turns the measurements into
a stage ladder. The axes are not invented: ten of them come from
ISO/IEC 25010:2023, and the remaining two — supply chain and handover — are
the dimensions a professional project is judged on once the code itself is
sound.

## The axes

| Axis | Weight | ISO/IEC 25010 characteristic |
| --- | --- | --- |
| security | 15 | security |
| reliability | 12 | reliability |
| functional | 12 | functional suitability |
| maintainability | 11 | maintainability |
| performance | 9 | performance efficiency |
| interaction | 9 | interaction capability |
| supplychain | 8 | — (SLSA + OpenSSF Scorecard) |
| flexibility | 8 | flexibility (portability + adaptability) |
| safety | 6 | safety |
| compatibility | 5 | compatibility |
| handover | 5 | — |

Weights live in [`config/scorecard.json`](../config/scorecard.json). They say
how much each axis contributes to the global score — security counts more
than compatibility because a broken security claim is a broken project, no
matter how compatible it is.

## How an axis score is computed

Each axis is a weighted ratio of its gate results:

```
axis score = 100 × (weight of passed gates) / (weight of all gates that ran)
```

Three details matter:

- **Skipped gates are excluded entirely.** A gate whose tool is not installed
  contributes to neither the numerator nor the denominator. It lowers the
  *coverage* of the axis — the set of claims that were actually checked — and
  that is the point. You cannot claim S3 while `osv-scanner` is missing.
- **An axis with no gates at all scores null**, and a null axis counts as 0
  when floors are evaluated. Unmeasured is not exempt.
- **A gate that fails earns nothing.** There is no partial credit: a gate
  either passed, failed, or was skipped.

## The global score

```
global score = Σ (axis score × axis weight / 100)
```

The global score is a convenience number for trend-following, not a license
to advance: the stage ladder is governed by floors, not by the average.

## Floors, not averages

[`config/stages.json`](../config/stages.json) defines the ladder and, for each
stage, the minimum score an axis must reach before the project can advance:

| Stage | Floors (partial) |
| --- | --- |
| S1 | functional 20, reliability 30, maintainability 20, handover 10 |
| S2 | functional 50, reliability 50, security 40, maintainability 40, flexibility 40, handover 25 |
| S3 | functional 70, reliability 75, security 70, performance 60, interaction 60, supplychain 60, maintainability 65, flexibility 55, handover 50 |
| S4 | every measured axis at 75–90, handover at 90 |

To advance from a stage, the **global score must pass every floor in the next
stage's table**. A high average cannot carry a weak axis past its floor, and a
`blocker` gate failure holds the phase red regardless of the score.

## Regression detection

After a phase claims green and its gates pass, Autonoma recomputes the
scorecard and compares it to the scorecard before the phase. If the global
score dropped — beyond `tolerance` (default 0, i.e. any drop counts) — the
phase is marked red, the tree rolls back to the SHA recorded before the run,
and the regression counter rises. `limits.maxRegressions` stops the whole run
after enough consecutive regressions, and an incident note is written.

This is what the ladder means in practice: a phase cannot make the project
*worse* and still count as done.

## Telemetry

Every turn appends one line to `telemetry.jsonl` in the state directory: the
phase, the verdict, the cause and the score. That history is what
`Get-WeakPlaybooks` reads to find playbooks that fail far more often than they
pass — the ones worth rewriting before the next project.
