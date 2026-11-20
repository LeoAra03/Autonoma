#!/usr/bin/env node
/**
 * bundle-size — cap the size of built JavaScript assets.
 *
 * Measures every .js file under the build output directory (default dist/) and
 * fails when any single file exceeds the cap in bytes. A missing build output
 * is a failure, never a silent pass.
 *
 * Usage: node tools/bundle-size.mjs --max=250000 [--dir=dist]
 */
import { readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const max = Number.parseInt(arg('max', '250000'), 10);
const dir = arg('dir', 'dist');

function walk(current, files = []) {
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    const full = join(current, entry.name);
    if (entry.isDirectory()) walk(full, files);
    else if (entry.isFile() && entry.name.endsWith('.js')) files.push(full);
  }
  return files;
}

let files;
try {
  files = walk(dir);
} catch (err) {
  console.error(`bundle-size: no build output in ${dir}/ (${err.message})`);
  process.exit(1);
}

let failures = 0;
for (const file of files) {
  const size = statSync(file).size;
  if (size > max) {
    console.error(`bundle-size: ${relative(process.cwd(), file)} is ${size} bytes (cap ${max})`);
    failures++;
  }
}

if (failures > 0) {
  console.error(`bundle-size: ${failures} file(s) over the cap`);
  process.exit(1);
}
console.log(`bundle-size: ${files.length} file(s), all within ${max} bytes`);
