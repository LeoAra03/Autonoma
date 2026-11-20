#!/usr/bin/env node
/**
 * check-restore — verify that disaster recovery is actually documented.
 *
 * Checks that the given runbook exists, is long enough to be real, and talks
 * about both backing up and restoring. A restore path that has never been
 * written down does not exist.
 *
 * Usage: node tools/check-restore.mjs --file=docs/RUNBOOK.md
 */
import { readFileSync, statSync } from 'node:fs';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const file = arg('file', 'docs/RUNBOOK.md');

let text;
try {
  text = readFileSync(file, 'utf8');
} catch (err) {
  console.error(`check-restore: cannot read ${file} (${err.message})`);
  process.exit(1);
}

if (statSync(file).size < 200) {
  console.error(`check-restore: ${file} is too short to document a restore`);
  process.exit(1);
}

const lower = text.toLowerCase();
const hasBackup = /back(up|up\b)/.test(lower);
const hasRestore = /restor(e|ation)/.test(lower);

if (!hasBackup || !hasRestore) {
  console.error(`check-restore: ${file} must document backup and restore`);
  process.exit(1);
}

console.log(`check-restore: ${file} documents backup and restore`);
