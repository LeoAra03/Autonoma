#!/usr/bin/env node
/**
 * check-adr — require a minimum number of recorded decisions.
 *
 * Counts Architecture Decision Records (Markdown files) in docs/ADR and fails
 * when fewer than the required minimum exist. Decisions that were never
 * written down cannot be reviewed, reverted, or learned from.
 *
 * Usage: node tools/check-adr.mjs --min=3 [--dir=docs/ADR]
 */
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const min = Number.parseInt(arg('min', '3'), 10);
const dir = arg('dir', 'docs/ADR');

let count = 0;
try {
  count = readdirSync(dir)
    .filter(name => name.endsWith('.md') && statSync(join(dir, name)).isFile())
    .length;
} catch (err) {
  console.error(`check-adr: cannot read ${dir} (${err.message})`);
  process.exit(1);
}

if (count < min) {
  console.error(`check-adr: ${count} ADR(s) in ${dir}, minimum is ${min}`);
  process.exit(1);
}

console.log(`check-adr: ${count} ADR(s), minimum ${min} met`);
