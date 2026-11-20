#!/usr/bin/env node
/**
 * check-error-handling — a lightweight guard on entry points.
 *
 * Every file listed in package.json main/bin/exports (that exists on disk)
 * must contain at least one try/catch block or a .catch() call. Entry points
 * that swallow nothing cannot survive a bad start. It is a heuristic, not a
 * proof — it exists to catch the obvious cases, and it fails closed.
 *
 * Usage: node tools/check-error-handling.mjs
 */
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

let pkg;
try {
  pkg = JSON.parse(readFileSync('package.json', 'utf8'));
} catch (err) {
  console.error(`check-error-handling: cannot read package.json (${err.message})`);
  process.exit(1);
}

const entries = new Set();
for (const field of ['main', 'browser']) {
  if (typeof pkg[field] === 'string') entries.add(pkg[field]);
}
if (typeof pkg.bin === 'string') entries.add(pkg.bin);
else if (pkg.bin && typeof pkg.bin === 'object') entries.add(...Object.values(pkg.bin));
if (pkg.exports) {
  const walk = obj => {
    for (const value of Object.values(obj)) {
      if (typeof value === 'string') entries.add(value);
      else if (value && typeof value === 'object') walk(value);
    }
  };
  walk(pkg.exports);
}

let findings = 0;
for (const entry of entries) {
  const file = resolve(dirname('.'), entry);
  if (!existsSync(file) || !statSync(file).isFile()) continue;
  const source = readFileSync(file, 'utf8');
  if (!/try\s*\{|\.catch\s*\(/.test(source)) {
    console.error(`check-error-handling: entry ${entry} has no try/catch or .catch()`);
    findings++;
  }
}

if (findings > 0) {
  console.error(`check-error-handling: ${findings} entry point(s) without error handling`);
  process.exit(1);
}
console.log(`check-error-handling: ${entries.size} entry point(s) inspected, all handled`);
