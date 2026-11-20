# Troubleshooting

The symptoms below are the ones worth knowing. Most of them are the system
working as designed — a red is information, not an error.

## The run will not start

**`unknown site adapter`** — the `site` value has no file in
`config/sites/`. Use `generic` or `arena`, or add an adapter.

**`no plan given`** — pass `-Plan`, or run `--discover` first.

**`plan invalid: cycle at <id>`** — the roadmap has a dependency loop. Break
the cycle: phases are executed in dependency order, and a cycle has no valid
order.

**`ledger chain broken`** — someone edited `ledger.jsonl` by hand. The chain
is the point; treat a broken ledger as a red run. Restore the ledger from
your backup or start a clean state with `--reset`.

## The browser never attaches

**`no chromium browser found`** — Autonoma looks for Chrome, Edge, Brave,
Chromium and Thorium in the standard install locations. Pass the executable
path with `-Browser <path>`, or set `browser.channel` to a specific channel.

**`debug port dead`** — the browser launched but the debug endpoint never
answered on the port. Check for another process holding the port, try a
different `port`, or kill stale browser processes and retry.

**The run keeps reconnecting** — the page crashed or reloaded. The engine
reconnects automatically; if it loops, the chat page may have navigated away
from the chat view. Re-open the chat tab and the run will reattach.

## The conversation stalls

**A CAPTCHA pauses the run** — by design. Solve it in the browser window; the
run beeps, waits, and resumes automatically once the CAPTCHA is gone.

**The editor keeps having text cleared** — the page is showing something in
the composer (a draft, an error message) that the engine reads as its own
text. The engine clears and retries; if it spins for a while, reload the page
and let the run reattach.

**`send failed: not-sent`** — Enter did not submit the message. The engine
falls back to clicking the send button. If it keeps failing, the site
adapter's `sendButton` selector no longer matches the page: update
`config/sites/<site>.json`.

**`card clicked` never happens, or the run is stuck on a card** — the card
budget is spent, or `seco` is on. That is the hold working: a card-gated
interface is being deliberately held back.

## The verdict is never read

**`verdict stuck, forcing read`** — the agent finished but did not print the
exit contract. Re-read the contract in the chat: the last message must end
with `ESTADO:`, `EVIDENCIA:` and `GATES:` lines. The engine forces a read
after `timing.verdictForce` minutes.

**The run treats a green as red** — the contract line was found but has no
evidence. A green without evidence is a red by design: fix the claim, not the
rule.

**`watchdog push`** — the agent has been idle for `timing.watchdog` minutes
with nothing in flight. The engine nudges the plan along. If it happens
repeatedly, the plan's phases are too coarse: split them into smaller parts.

## Gates

**`gate <id> skipped (missing tool: ...)`** — the tool behind the gate is not
installed. Skipped is not passed: it lowers the axis score. Install the tool
(`gitleaks`, `osv-scanner`, `semgrep`, `syft`, …) and re-run.

**`gate <id> timeout`** — the gate ran past its `timeout`. Either the project
is too big for the default, or the gate is stuck. Investigate before raising
the timeout.

**A gate fails but the phase was "green"** — that is the falsifiable verdict
working: the claim does not survive the gates, so the phase is red and the
tree rolls back. Read the gate output in `state/gates/` and fix the finding.

**`score regression`** — the phase's gates passed, but the global score
dropped. Something else measured by the ledger got worse — usually a gate
that became skipped, or a previous green that no longer holds. Check the
telemetry for the axis that moved.

## State problems

**The stage does not advance** — the floors for the next stage are not met.
Read `--score`: every axis in the next stage's floor table must clear its own
threshold, and skipped gates lower the score.

**A phase is parked (`atascada`)** — it went red `limits.redsPerPhase` times
or was blocked. It gets `limits.deepRetries` deep retries; after that it
stays parked until the plan changes. Fix the underlying problem, then
`--reset` if you want to retry the whole run.

**The run is confused after editing the plan** — state is keyed by the plan
path *and its content hash*, so an edited roadmap starts clean. If you edited
the plan without changing content, use `--reset`.

## Logs

Everything the run does is in `%LOCALAPPDATA%\Autonoma\logs\`. Read the tail
of the latest `autonoma-*.log` first; it names the phase, the gate, and the
reason.
