#!/usr/bin/env node
// The disguised Camoufox's config (@playwright/mcp --config) for the machine it runs on (2026-11-10).
//   node camoufox-config.mjs --in <config.json> [--out <file>]     (no --out: rewritten in place)
// Kept: the disguise (CAMOU_CONFIG_*), headless, args and Firefox preferences. Set here: the browser binary and the
// uBlock add-on, found in Camoufox's cache ($CAMOUFOX_DIR, else $XDG_CACHE_HOME/camoufox). The environment handed to
// the browser becomes only the disguise plus this machine's HOME, PATH, DISPLAY, TZ and LANG: the file made on the
// Windows PC carried a whole Claude Code session's environment (its PATH, APPDATA and session tokens included).
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

const arg = (n) => { const i = process.argv.indexOf(n); return i > 0 ? process.argv[i + 1] : undefined; };
const IN = arg('--in');
const OUT = arg('--out') || IN;
if (!IN) { console.error('usage: camoufox-config.mjs --in <config.json> [--out <file>]'); process.exit(2); }
const CACHE = process.env.CAMOUFOX_DIR || join(process.env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'camoufox');
const CHUNK = 2047;   // the size the original was split at

/** The first path under `dir` (depth-first, `depth` levels) whose entry passes `test(name, isDir)`. */
function find(dir, test, depth = 5) {
  let list = [];
  try { list = readdirSync(dir, { withFileTypes: true }); } catch { return null; }
  for (const e of list) if (test(e.name, e.isDirectory())) return join(dir, e.name);
  if (depth > 0) for (const e of list) if (e.isDirectory()) { const f = find(join(dir, e.name), test, depth - 1); if (f) return f; }
  return null;
}

const exe = find(CACHE, (n, d) => !d && ['camoufox-bin', 'camoufox', 'camoufox.exe'].includes(n));
const ubo = find(CACHE, (n, d) => d && n === 'UBO');
if (!exe || !existsSync(exe)) { console.error(`no Camoufox browser under ${CACHE}: run python -m camoufox fetch`); process.exit(1); }

const c = JSON.parse(readFileSync(IN, 'utf8'));
const lo = c.browser.launchOptions;
const env = lo.env || {};
const keys = Object.keys(env).filter((k) => /^CAMOU_CONFIG_\d+$/.test(k)).sort((a, b) => a.split('_')[2] - b.split('_')[2]);
const cfg = keys.length ? JSON.parse(keys.map((k) => env[k]).join('')) : {};
if ('addons' in cfg) cfg.addons = ubo ? [ubo] : [];
const s = JSON.stringify(cfg);
const out = {};
// Split where the original was, never inside a UTF-16 surrogate pair.
for (let i = 0, n = 1; i < s.length; n++) {
  let j = Math.min(i + CHUNK, s.length);
  if (j < s.length && /[\uD800-\uDBFF]/.test(s[j - 1])) j--;
  out[`CAMOU_CONFIG_${n}`] = s.slice(i, j);
  i = j;
}
for (const k of ['HOME', 'PATH', 'DISPLAY', 'TZ', 'LANG']) if (process.env[k]) out[k] = process.env[k];
lo.env = out;
lo.executablePath = exe;
writeFileSync(OUT, JSON.stringify(c, null, 1) + '\n');
console.log(`camoufox config for this machine: ${exe}; add-on ${ubo || 'none'}; ${Object.keys(out).filter((k) => k.startsWith('CAMOU')).length} disguise chunks`);
