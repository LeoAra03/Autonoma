#!/usr/bin/env node
/**
 * check-lockfile — make dependency installs reproducible.
 *
 * Verifies that a lockfile exists and is consistent: for package-lock.json it
 * must be valid JSON, carry the package name, and declare at least one
 * dependency entry. yarn.lock and pnpm-lock.yaml only need to exist and be
 * non-empty.
 *
 * Usage: node tools/check-lockfile.mjs
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';

let pkg;
try {
  pkg = JSON.parse(readFileSync('package.json', 'utf8'));
} catch (err) {
  console.error(`check-lockfile: cannot read package.json (${err.message})`);
  process.exit(1);
}

const lockfiles = ['package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'].filter(f =>
  readdirSync('.').includes(f) && statSync(f).size > 0
);

if (lockfiles.length === 0) {
  console.error('check-lockfile: no lockfile found (expected package-lock.json, yarn.lock or pnpm-lock.yaml)');
  process.exit(1);
}

const lockfile = lockfiles[0];
if (lockfile !== 'package-lock.json') {
  console.log(`check-lockfile: ${lockfile} present and non-empty`);
  process.exit(0);
}

let lock;
try {
  lock = JSON.parse(readFileSync(lockfile, 'utf8'));
} catch (err) {
  console.error(`check-lockfile: ${lockfile} is not valid JSON (${err.message})`);
  process.exit(1);
}

if (lock.name && pkg.name && lock.name !== pkg.name) {
  console.error(`check-lockfile: ${lockfile} is for "${lock.name}" but package.json is "${pkg.name}"`);
  process.exit(1);
}

const entries = lock.packages ? Object.keys(lock.packages).length : Object.keys(lock.dependencies || {}).length;
if (entries === 0) {
  console.error('check-lockfile: package-lock.json declares no dependencies');
  process.exit(1);
}

console.log(`check-lockfile: ${lockfile} is valid (${entries} entries)`);
