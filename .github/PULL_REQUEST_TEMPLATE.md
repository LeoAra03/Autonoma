## What

A short description of the change: what it does and why.

## How it was verified

Paste the result of the self-check:

```
./tools/verify.ps1
```

If the change affects gates, playbooks, the scorecard or the engine, include
the commands you ran to exercise them.

## Checklist

- [ ] `tools/verify.ps1` is green locally.
- [ ] New or changed gates are documented in `docs/GATES.md` if their
      behaviour changed.
- [ ] The exit contract is untouched (or the change is an ADR explaining
      why it changed).
- [ ] No secrets, absolute paths, or personal data in the diff.
