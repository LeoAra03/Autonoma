#!/usr/bin/env node
/**
 * perf-budget — enforce a performance budget against measured metrics.
 *
 * Reads a budget file (JSON: { "metrics": { "<name>": <limit>, ... } }) and a
 * metrics file produced by the project's own perf harness (default
 * perf-metrics.json next to the budget file). Every measured metric must be at
 * or below its limit. A missing metrics file is a failure, never a silent pass.
 *
 * Usage: node tools/perf-budget.mjs --budget=config/perf-budget.json [--metrics=perf-metrics.json]
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const budgetFile = arg('budget', 'config/perf-budget.json');
const metricsFile = arg('metrics', join(dirname(budgetFile), 'perf-metrics.json'));

let budget;
try {
  budget = JSON.parse(readFileSync(budgetFile, 'utf8'));
} catch (err) {
  console.error(`perf-budget: cannot read budget ${budgetFile} (${err.message})`);
  process.exit(1);
}

const limits = budget.metrics;
if (!limits || Object.keys(limits).length === 0) {
  console.error('perf-budget: budget file declares no metrics');
  process.exit(1);
}

let metrics;
try {
  metrics = JSON.parse(readFileSync(metricsFile, 'utf8'));
} catch (err) {
  console.error(`perf-budget: no metrics at ${metricsFile} — run the perf harness first (${err.message})`);
  process.exit(1);
}

let failures = 0;
for (const [name, limit] of Object.entries(limits)) {
  const value = metrics[name];
  if (value === undefined) {
    console.error(`perf-budget: metric "${name}" was never measured`);
    failures++;
    continue;
  }
  if (value > limit) {
    console.error(`perf-budget: ${name} ${value} exceeds budget ${limit}`);
    failures++;
  } else {
    console.log(`perf-budget: ${name} ${value} (budget ${limit})`);
  }
}

if (failures > 0) {
  console.error(`perf-budget: ${failures} metric(s) over budget`);
  process.exit(1);
}
console.log('perf-budget: all metrics within budget');
