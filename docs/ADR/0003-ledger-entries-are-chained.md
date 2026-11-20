# ADR 0003: Ledger entries are chained

**Status:** Accepted
**Date:** 2026-11-20

## Context

Every verdict — every gate result, every phase claim, every stage advance —
needs to be auditable after the fact. A flat list of results can be edited in
place, and an edited green is indistinguishable from a real one.

## Decision

The ledger is an append-only JSON-lines file. Each entry records the hash of
the previous entry and receives its own SHA-256 hash computed over its
content. `--verify-ledger` recomputes the whole chain from the file.

## Consequences

- Hand-editing any entry — or removing, reordering, or inserting one — breaks
  the chain and is detected by the verifier. A broken chain is a red run.
- The artifact directory under `state/gates/` keeps the raw gate output next
  to the ledger, so a verdict can be traced back to the evidence behind it.
- The ledger is deliberately simple: append-only, hash-chained, no database.
  Anyone can read it with a text editor.
- Entries are written after each gate set and each phase verdict, which keeps
  the chain's cost proportional to the work, not to the project size.
