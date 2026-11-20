#!/usr/bin/env node
/**
 * coverage-floor — enforce a minimum coverage percentage.
 *
 * Reads an Istanbul/c8 coverage summary (coverage/coverage-summary.json) and
 * fails when the total line-coverage percentage is below the required floor.
 * A missing coverage report is a failure, never a silent pass.
 *
 * Usage: node tools/coverage-floor.mjs --min=60 [--file=coverage/coverage-summary.json]
 */
import { readFileSync } from 'node:fs';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const min = Number.parseFloat(arg('min', '0'));
const file = arg('file', 'coverage/coverage-summary.json');

let summary;
try {
  summary = JSON.parse(readFileSync(file, 'utf8'));
} catch (err) {
  console.error(`coverage-floor: no coverage report at ${file} (${err.message})`);
  process.exit(1);
}

const total = summary.total;
if (!total || typeof total.lines.pct !== 'number') {
  console.error('coverage-floor: coverage summary has no total.lines.pct');
  process.exit(1);
}

const pct = total.lines.pct;
if (pct < min) {
  console.error(`coverage-floor: ${pct}% lines covered, floor is ${min}% (${total.lines.covered}/${total.lines.total})`);
  process.exit(1);
}

console.log(`coverage-floor: ${pct}% lines covered (floor ${min}%)`);
