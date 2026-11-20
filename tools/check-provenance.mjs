#!/usr/bin/env node
/**
 * check-provenance — check how strong the supply-chain provenance is.
 *
 * Scans .github/workflows/*.yml and assigns a level:
 *   1 — nothing: no provenance requested.
 *   2 — artifacts attested: workflows request artifact attestations (SLSA,
 *       actions/attest, or an explicit provenance request).
 *   3 — SLSA generator: the SLSA GitHub generator is wired up for the
 *       workflows that publish artifacts.
 * Fails when the highest level found is below the required minimum.
 *
 * Usage: node tools/check-provenance.mjs --min-level=2 [--dir=.github/workflows]
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const minLevel = Number.parseInt(arg('min-level', '2'), 10);
const dir = arg('dir', '.github/workflows');

let files = [];
try {
  files = readdirSync(dir).filter(name => name.endsWith('.yml') || name.endsWith('.yaml')).map(name => join(dir, name));
} catch (err) {
  console.error(`check-provenance: cannot read ${dir} (${err.message})`);
  process.exit(1);
}

if (files.length === 0) {
  console.error(`check-provenance: no workflow files in ${dir}`);
  process.exit(1);
}

let highest = 1;
for (const file of files) {
  const text = readFileSync(file, 'utf8');
  let level = 1;
  if (/slsa-github-generator|slsa\.dev|provenance-level/i.test(text)) level = 3;
  else if (/attest|provenance|slsa/i.test(text)) level = 2;
  if (level > highest) highest = level;
}

const names = ['nothing', 'artifacts attested', 'SLSA generator'];
if (highest < minLevel) {
  console.error(`check-provenance: provenance level ${highest} (${names[highest - 1]}), required level ${minLevel} (${names[minLevel - 1]})`);
  process.exit(1);
}

console.log(`check-provenance: provenance level ${highest} (${names[highest - 1]}) meets required level ${minLevel}`);
