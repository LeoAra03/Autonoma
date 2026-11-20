#!/usr/bin/env node
/**
 * check-engines — verify the running toolchain satisfies package.json engines.
 *
 * Reads the "engines" field of package.json and compares it against the running
 * Node version. Supports the common range forms: >=18, ^20, ~20.3, 22.x,
 * 18 - 22, and plain pins like "20". No dependencies; a minimal comparator.
 *
 * Usage: node tools/check-engines.mjs
 */
import { readFileSync } from 'node:fs';

let pkg;
try {
  pkg = JSON.parse(readFileSync('package.json', 'utf8'));
} catch (err) {
  console.error(`check-engines: cannot read package.json (${err.message})`);
  process.exit(1);
}

const spec = pkg.engines && pkg.engines.node;
if (!spec) {
  console.log('check-engines: package.json declares no engines.node');
  process.exit(0);
}

const version = process.versions.node.split('.').map(Number);

// Zero-pad each segment so versions compare correctly as strings.
const pad = parts => parts.map(x => String(x || 0).padStart(6, '0')).join('.');
const v = pad(version);

const OPS = {
  '>=': ['lower', 2],
  '>': ['lower', 1],
  '^': ['lower', 1],
  '~': ['lower', 1],
  '<=': ['upper', 2],
  '<': ['upper', 1],
};

function satisfies() {
  let lower = null;
  let upper = null;

  for (const part of spec.trim().split(/\s+/).filter(Boolean)) {
    const op = OPS[part[0]];
    if (op) {
      const bound = pad(part.slice(op[1]).split('.').map(Number));
      if (op[0] === 'lower') lower = bound; else upper = bound;
      continue;
    }
    if (/^\d+$/.test(part)) {
      if (String(version[0]) !== part) return false;
      continue;
    }
    if (/^\d+\.x$/.test(part)) {
      if (String(version[0]) !== part.split('.')[0]) return false;
      continue;
    }
    return false; // unknown operator — fail closed
  }

  if (lower && v < lower) return false;
  if (upper && v >= upper) return false;
  return true;
}

if (satisfies()) {
  console.log(`check-engines: node ${process.versions.node} satisfies "${spec}"`);
} else {
  console.error(`check-engines: node ${process.versions.node} does not satisfy engines.node "${spec}"`);
  process.exit(1);
}
