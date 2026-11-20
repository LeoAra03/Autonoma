#!/usr/bin/env node
/**
 * check-handover — verify the handover bundle is complete.
 *
 * Looks for the standard handover files anywhere in the tree and fails when any
 * of them is missing or too thin to be useful. Mirrors the handover check the
 * director runs before declaring a project S4.
 *
 * Usage: node tools/check-handover.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const REQUIRED = [
  'ARCHITECTURE.md', 'RUNBOOK.md', 'OWNERS.md', 'ONBOARDING.md',
  'TRANSFER.md', 'SECURITY.md', 'NOTICE', 'SBOM.json'
];

const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'build', 'coverage']);

function find(name) {
  const stack = ['.'];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      if (SKIP_DIRS.has(entry.name)) continue;
      const full = join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (entry.name === name) return full;
    }
  }
  return null;
}

let missing = [];
for (const name of REQUIRED) {
  const file = find(name);
  if (!file) {
    missing.push(`${name} (missing)`);
    continue;
  }
  if (statSync(file).size < 200) {
    missing.push(`${name} (too short)`);
  }
}

if (missing.length > 0) {
  console.error(`check-handover: handover bundle incomplete: ${missing.join(', ')}`);
  process.exit(1);
}

console.log(`check-handover: all ${REQUIRED.length} handover files present and substantial`);
