#!/usr/bin/env node
/**
 * complexity — keep cyclomatic complexity under a cap.
 *
 * A pragmatic estimator: for each JavaScript/TypeScript file, V(G) is 1 plus
 * the number of decision points (if, for, while, switch, case, catch, &&,
 * ||, ?? and ?) found outside strings, comments and regex literals. Literal
 * text is stripped with regular expressions first, so keywords that merely
 * appear inside a string or a comment are not counted. It is deliberately
 * conservative and file-level: it tells you which files to look at, not a
 * formal measure.
 *
 * Usage: node tools/complexity.mjs --max=20 [--glob=src]
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';

function arg(name, fallback) {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const max = Number.parseInt(arg('max', '20'), 10);
const root = arg('glob', '.');

const DECISION = /(\bif\b|\bfor\b|\bwhile\b|\bswitch\b|\bcase\b|\bcatch\b|&&|\|\||\?\?|\?)/g;

// Order matters: comments and strings go first so their contents never reach
// the decision scan or the regex-literal stripper.
const STRIP = [
  /\/\*[\s\S]*?\*\//g,                                       // block comments
  /\/\/[^\n]*/g,                                             // line comments
  /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`/g,  // strings
  /\/(?:[^/\\\n[]|\\.|\[[^\]]*\])+\/[a-z]*/g,               // regex literals
];

function stripLiterals(source) {
  let out = source;
  for (const re of STRIP) out = out.replace(re, ' ');
  return out;
}

function walk(current, files = []) {
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git') continue;
    const full = join(current, entry.name);
    if (entry.isDirectory()) walk(full, files);
    else if (entry.isFile() && /\.(js|ts|jsx|tsx|mjs|cjs)$/.test(entry.name)) files.push(full);
  }
  return files;
}

const files = walk(root);
let failures = 0;

for (const file of files) {
  const code = stripLiterals(readFileSync(file, 'utf8'));
  let score = 1;
  const matches = code.match(DECISION);
  if (matches) score += matches.length;
  if (score > max) {
    console.error(`complexity: ${relative(process.cwd(), file)} scores ${score} (cap ${max})`);
    failures++;
  }
}

if (failures > 0) {
  console.error(`complexity: ${failures} file(s) over the cap`);
  process.exit(1);
}
console.log(`complexity: ${files.length} file(s) inspected, all within cap ${max}`);
