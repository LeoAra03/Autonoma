# ADR 0002: Scoring axes follow ISO/IEC 25010:2023

**Status:** Accepted
**Date:** 2026-11-20

## Context

The scorecard needs a set of quality axes. Two options were available: invent
a set that sounds right, or base it on an existing standard. A self-invented
taxonomy is hard to argue with in a review — "why is security worth more than
compatibility?" has no answer if the weights are just taste.

## Decision

The axes are the nine quality characteristics of ISO/IEC 25010:2023 —
functional suitability, performance efficiency, compatibility, interaction
capability, reliability, security, maintainability, flexibility and safety —
plus two additions the standard does not cover: supply chain (SLSA levels and
OpenSSF Scorecard) and handover. The ISO axes are cited in
`config/scorecard.json` so the scorecard can be argued in a review against a
published standard rather than against a personal preference.

## Consequences

- The scorecard is defensible: the taxonomy and its names come from a
  standard, and the two additions have named sources (SLSA, OpenSSF).
- Floors per axis mean an axis cannot be carried by another's strength.
- The weights remain a project-level decision, but they configure a standard
  structure instead of inventing one.
- Not every project needs all eleven axes at the same depth — that is what
  the stage floors are for.
