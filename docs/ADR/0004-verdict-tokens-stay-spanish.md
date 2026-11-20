# ADR 0004: Verdict tokens stay in Spanish

**Status:** Accepted
**Date:** 2026-11-20

## Context

The exit contract the agent must print uses Spanish keywords —
`ESTADO`, `EVIDENCIA`, `GATES`, `HECHO`, `SIN HALLAZGOS` — and the site
adapters match them with regexes. It would be cleaner to use English tokens
(`STATUS`, `EVIDENCE`, `DONE`, `NO FINDINGS`).

## Decision

Keep the Spanish tokens exactly as specified.

## Consequences

- The contract is stable and already exercised by the adapters and the test
  suite. Changing the tokens would silently break every adapter, every brief
  and every agent already trained to answer in that format.
- The tokens are protocol keywords, not prose: they are matched with anchored
  regexes and are independent of the language of the surrounding briefs,
  which remain in English.
- Any future change to the contract should be a versioned, additive change
  (a new token), never a rename of these five.
