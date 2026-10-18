// Dev tool: generate hidden/expected.json from the reference solution.
//   node hidden/gen_expected.mjs
// Builds a reference workspace (files/, with ref/src overlaid) in a temp dir and
// records shadeAt() for every (venue, instant) case.
import { mkdtempSync, cpSync, writeFileSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const taskRoot = join(dirname(fileURLToPath(import.meta.url)), '..');

// Instants (UTC) that land across the day in Seattle (America/Los_Angeles).
const TIMES = [
  '2026-06-21T15:00:00Z', '2026-06-21T17:00:00Z', '2026-06-21T19:00:00Z',
  '2026-06-21T21:00:00Z', '2026-06-21T23:00:00Z', '2026-06-22T01:00:00Z',
  '2026-03-14T18:00:00Z', '2026-03-14T21:00:00Z', '2026-10-05T17:00:00Z',
  '2026-10-05T20:00:00Z', '2026-06-21T08:00:00Z', '2026-12-01T20:00:00Z',
];

const ref = mkdtempSync(join(tmpdir(), 'rally-ref-'));
cpSync(join(taskRoot, 'files', 'src'), join(ref, 'src'), { recursive: true });
cpSync(join(taskRoot, 'ref', 'src'), join(ref, 'src'), { recursive: true });
const venuesDir = join(taskRoot, 'files', 'venues');
const { shadeAt } = await import(join(ref, 'src', 'shade.js'));

const cases = [];
for (const file of readdirSync(venuesDir).sort()) {
  const scene = JSON.parse(readFileSync(join(venuesDir, file), 'utf8'));
  for (const iso of TIMES) {
    cases.push({ venue: file, date: iso, want: shadeAt(scene, new Date(iso)) });
  }
}
const out = join(taskRoot, 'hidden', 'expected.json');
writeFileSync(out, JSON.stringify({ tol: 0.01, cases }, null, 0) + '\n');
console.log(`wrote ${cases.length} cases to ${out}`);
