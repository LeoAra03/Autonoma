# Legal

Autonoma ships under the MIT license — see [`LICENSE`](../LICENSE). That is
the only legal statement in this repository, and it is deliberately short.

## What the "legal" gates actually check

The handover and security gates include license and vulnerability checks.
These are **technical** checks, not legal advice:

- **License compatibility** — the `license` playbook and the handover check
  verify that the project's LICENSE file exists, that third-party assets are
  listed in NOTICE, and that the dependency set does not obviously conflict
  with the project's chosen license.
- **Known vulnerabilities** — the dependency scan (`osv-scanner`) checks
  lockfiles against public vulnerability databases. It reports what the
  databases report; it does not judge risk, exploitability, or whether a
  finding matters for your deployment.

Neither check is a substitute for a human decision. If a dependency's license
is ambiguous, if a finding lands in a component you never ship, or if your
product has regulatory obligations, that is a conversation with the people
who own the risk — not a gate.

## Running Autonoma against other projects

Autonoma is not affiliated with any site it drives, and it never stores login
state or credentials: it runs in an isolated browser profile and talks to the
debug endpoint on `127.0.0.1`. When you point it at a service, use the same
care you would use with any tool that types on your behalf, and make sure the
work you ask it to do is work you are allowed to do there.

## Bundled third-party content

Autonoma has no runtime dependencies and bundles no third-party code. The gate
tools it calls (`gitleaks`, `osv-scanner`, `semgrep`, `syft`, the Node.js
helper scripts in `tools/`) are external commands you install yourself; each
of them has its own license, which you accept when you install it.
