import test from 'node:test';
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { sunPosition } from '../src/sun.js';
import { shadeAt } from '../src/shade.js';

const here = dirname(fileURLToPath(import.meta.url));

test('sun is up at local midday and down at night (Seattle, June)', () => {
  const noon = new Date('2026-06-21T20:00:00Z'); // ~13:00 PDT
  const night = new Date('2026-06-21T08:00:00Z'); // ~01:00 PDT
  const s = sunPosition(noon, 47.6, -122.3);
  assert.ok(s.alt > 0.5, 'sun should be high at midday');
  assert.ok(s.bearing >= 0 && s.bearing < 2 * Math.PI, 'bearing wraps to [0, 2pi)');
  assert.ok(sunPosition(night, 47.6, -122.3).alt < 0, 'sun below horizon at night');
});

test('a flat venue with no obstacles is never shaded', () => {
  const scene = {
    lat: 47.6, lng: -122.3, radius: 110, cell: 2, g: 110,
    courts: [{ sport: 'tennis', cx: 0, cy: 0, theta: 0, pl: 23.77, pw: 10.97, AL: 36.6, AW: 18.3, z: 0 }],
    top: new Array(110 * 110).fill(-32768),
  };
  assert.strictEqual(shadeAt(scene, new Date('2026-06-21T20:00:00Z')), 0);
});

test('a venue buried in tall obstacles is fully shaded', () => {
  const scene = {
    lat: 47.6, lng: -122.3, radius: 110, cell: 2, g: 110,
    courts: [{ sport: 'tennis', cx: 0, cy: 0, theta: 0, pl: 23.77, pw: 10.97, AL: 36.6, AW: 18.3, z: 0 }],
    top: new Array(110 * 110).fill(32767),
  };
  assert.strictEqual(shadeAt(scene, new Date('2026-06-21T20:00:00Z')), 1);
});

test('the sun is down after dark: shadeAt is null', () => {
  const scene = JSON.parse(readFileSync(join(here, '..', 'venues', 'kenmore-inglemoor-hs.json'), 'utf8'));
  assert.strictEqual(shadeAt(scene, new Date('2026-06-21T09:00:00Z')), null);
});
