#!/usr/bin/env node
/**
 * check-doc-sections — require that a document has the sections we promised.
 *
 * Parses level-2 headings (## ...) of a Markdown file and fails when any of the
 * required sections is missing. A required name matches a heading when the
 * heading text contains it (case-insensitive), so "install" matches
 * "## Installation".
 *
 * Usage: node tools/check-doc-sections.mjs --file=README.md --require=install,usage
 */
import { readFileSync } from 'node:fs';

function arg(name, fallback = '') {
  const hit = process.argv.find(a => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

const file = arg('file', 'README.md');
const required = arg('require', '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);

let text;
try {
  text = readFileSync(file, 'utf8');
} catch (err) {
  console.error(`check-doc-sections: cannot read ${file} (${err.message})`);
  process.exit(1);
}

const headings = text
  .split('\n')
  .filter(line => /^##\s+/.test(line))
  .map(line => line.replace(/^##\s+/, '').toLowerCase());

const missing = required.filter(name => !headings.some(h => h.includes(name)));
if (missing.length > 0) {
  console.error(`check-doc-sections: ${file} is missing sections: ${missing.join(', ')}`);
  process.exit(1);
}

console.log(`check-doc-sections: ${file} has all required sections (${required.join(', ')})`);
