#!/usr/bin/env node
/**
 * Wrapper de `npm start` para Autonoma.
 *
 * La lógica de instalación vive en `scripts/bootstrap.py` (Python estándar, sin
 * dependencias). Este archivo hace sólo dos cosas: encontrar un Python 3.10+ y
 * delegar en él. Así `npm start`, el .bat y el .sh comparten un único instalador.
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const bootstrap = path.join(here, 'bootstrap.py');
const MINOR = 10;

/** Candidatos en el orden en que Windows/Linux/macOS suelen tener un Python utilizable. */
function candidates() {
  const list = [];
  if (process.env.AUTONOMA_PYTHON) list.push({ cmd: null, args: [], label: 'AUTONOMA_PYTHON' });
  const isWin = process.platform === 'win32';
  if (isWin) for (const name of ['py.exe', 'py']) list.push({ cmd: name, args: ['-3'], label: name });
  for (const name of ['python3', 'python']) list.push({ cmd: name, args: [], label: name });
  return list.filter((entry) => entry.cmd === null || which(entry.cmd));
}

function which(command) {
  const exts = process.platform === 'win32' ? ['.exe', '.cmd', ''] : [''];
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (!dir) continue;
    for (const ext of exts) {
      if (existsSync(path.join(dir, command + ext))) return true;
    }
  }
  return false;
}

function probe(candidate) {
  const cmd = candidate.cmd ?? process.env.AUTONOMA_PYTHON;
  const args = [...candidate.args, '-c', `import sys;sys.exit(0 if sys.version_info[:2]>=(${3},${MINOR}) else 1)`];
  const run = spawnSync(cmd, args, { encoding: 'utf8', timeout: 25000 });
  return !run.error && run.status === 0 ? cmd : null;
}

function findPython() {
  for (const candidate of candidates()) {
    const found = probe(candidate);
    if (found) return found;
  }
  return null;
}

function main() {
  if (!existsSync(bootstrap)) {
    console.error(`autonoma: falta ${bootstrap} (¿ejecutaste esto dentro del repo?)`);
    return 127;
  }
  const python = findPython();
  if (!python) {
    console.error(
      [
        `autonoma: necesito Python 3.${MINOR}+ y no lo encontré en el PATH.`,
        '  Descárgalo de https://www.python.org/downloads/ (en Windows marca "Add python.exe to PATH"),',
        '  o dime cuál usar:  npm start -- --python C:\\Python312\\python.exe',
        '',
        '  Alternativa sin instalar nada: el ejecutable portable Autonoma.exe (ver INSTALL.md).',
      ].join('\n'),
    );
    return 127;
  }
  const run = spawnSync(python, [bootstrap, ...process.argv.slice(2)], { stdio: 'inherit' });
  if (run.error) {
    console.error(`autonoma: no se pudo lanzar ${python}: ${run.error.message}`);
    return 126;
  }
  return run.status === null ? 130 : run.status;
}

process.exit(main());
