#!/usr/bin/env node
/**
 * clean-install — prove the project installs from scratch.
 *
 * Removes node_modules, then installs from the lockfile (npm ci) or falls back
 * to npm install when no lockfile exists. Fails when the install does not
 * succeed. Runs in the current directory, so point it at the project root.
 *
 * Usage: node tools/clean-install.mjs
 */
import { existsSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const hasLockfile = existsSync('package-lock.json');

console.log(`clean-install: removing node_modules`);
rmSync('node_modules', { recursive: true, force: true });

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const args = hasLockfile ? ['ci'] : ['install'];
console.log(`clean-install: npm ${args.join(' ')}`);

const result = spawnSync(npm, args, { stdio: 'inherit', shell: process.platform === 'win32' });
if (result.status !== 0) {
  console.error(`clean-install: npm ${args.join(' ')} exited with ${result.status}`);
  process.exit(1);
}
console.log('clean-install: fresh install succeeded');
