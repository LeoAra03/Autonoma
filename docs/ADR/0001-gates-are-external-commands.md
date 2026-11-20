# ADR 0001: Gates are external commands

**Status:** Accepted
**Date:** 2026-11-20

## Context

The director has to decide whether an agent's claim that a phase is done is
true. The claim is made in the agent's context — a conversation, possibly with
a language model at the other end. Anything that judges that claim from the
same context can be influenced by the same context.

## Decision

A gate is a command line check executed in a fresh process, with a timeout,
outside the agent's context. A claim survives only if the gates exit as
expected. There is no code path that advances a phase on prose alone.

## Consequences

- The verdict is falsifiable and auditable: anyone can re-run the gate and
  check the exit code.
- A missing tool is reported as *skipped*, never as *passed*, so an
  uninstalled scanner lowers the score instead of faking a green.
- Gates are only as good as the tools behind them, which is why
  `tools/verify.ps1` and the CI job exist: the gate definitions themselves are
  checked for completeness and duplicates.
- The cost is that Autonoma is not a drop-in for every project: a stack with
  no runner has no gates, and the first playbook has to build one.
