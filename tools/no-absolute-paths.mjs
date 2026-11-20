#!/usr/bin/env node
/**
 * no-absolute-paths — keep repositories portable.
 *
 * Scans JavaScript, TypeScript and JSON files for hard-coded absolute paths
 * (POSIX /home/..., /Users/..., /var/... and Windows C:\...). Absolute paths
 * break clean checkouts on other machines; relative paths survive.
 *
 * Usage: node tools/no-absolute-paths.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'build', 'coverage']);
const PATTERN = /(["'])(?:\/(?:home|Users|var|etc|opt|tmp)\/[^"'\n]*|\/[A-Za-z]:\\[^"'\n]*|[A-Za-z]:\\(?:Users|Program Files|home)\b)[^"'\n]*\1/g;

function walk(current, files = []) {
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(current, entry.name);
    if (entry.isDirectory()) walk(full, files);
    else if (entry.isFile() && /\.(js|ts|jsx|tsx|mjs|cjs|json)$/.test(entry.name)) files.push(full);
  }
  return files;
}

const files = walk('.');
let findings = 0;

for (const file of files) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, index) => {
    const stripped = line.replace(/\/\/.*$/, '').replace(/\/\*.*?\*\//g, '');
    const match = stripped.match(PATTERN);
    if (match) {
      console.error(`no-absolute-paths: ${relative(process.cwd(), file)}:${index + 1}: ${match[0]}`);
      findings++;
    }
  });
}

if (findings > 0) {
  console.error(`no-absolute-paths: ${findings} absolute path(s) found`);
  process.exit(1);
}
console.log(`no-absolute-paths: ${files.length} file(s) scanned, no absolute paths`);
