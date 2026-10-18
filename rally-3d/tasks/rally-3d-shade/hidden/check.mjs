// Hidden grader: the agent's shadeAt() against the reference numbers.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const ws = process.cwd();
const { shadeAt } = await import(join(ws, 'src', 'shade.js'));
const { tol, cases } = JSON.parse(readFileSync(join(here, 'expected.json'), 'utf8'));

const cache = new Map();
const sceneOf = (file) => {
  if (!cache.has(file)) cache.set(file, JSON.parse(readFileSync(join(ws, 'venues', file), 'utf8')));
  return cache.get(file);
};

const bad = [];
for (const c of cases) {
  let got;
  try {
    got = shadeAt(sceneOf(c.venue), new Date(c.date));
  } catch (e) {
    bad.push({ ...c, got: `threw: ${e.message}` });
    continue;
  }
  const ok = c.want === null ? got === null : got !== null && Math.abs(got - c.want) <= tol;
  if (!ok) bad.push({ ...c, got });
}

if (bad.length) {
  console.log(`${bad.length}/${cases.length} cases wrong`);
  for (const b of bad.slice(0, 8))
    console.log(`  ${b.venue} @ ${b.date}: got ${typeof b.got === 'number' ? b.got.toFixed(4) : b.got}, want ${b.want === null ? 'null' : b.want.toFixed(4)}`);
  process.exit(1);
}
console.log(`all ${cases.length} cases match within ${tol}`);
