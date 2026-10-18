// Hidden grader for rally-3d-sun: behaviour, not exact numbers.
// The physics is graded against an independent reference with generous
// tolerances, plus sunny/shaded/night sanity cases.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ws = process.cwd();
const { sunPosition } = await import(join(ws, 'src', 'sun.js'));
const { shadeAt } = await import(join(ws, 'src', 'shade.js'));

// ---- independent reference ------------------------------------------------
const rad = Math.PI / 180;
function refSun(date, lat, lng) {
  const d = date.valueOf() / 86_400_000 - 0.5 + 2_440_588 - 2_451_545;
  const M = rad * (357.5291 + 0.98560028 * d);
  const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
  const L = M + C + rad * 102.9372 + Math.PI;
  const e = rad * 23.4397;
  const dec = Math.asin(Math.sin(e) * Math.sin(L));
  const ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
  const H = rad * (280.16 + 360.9856235 * d) + rad * lng - ra;
  const phi = rad * lat;
  const alt = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
  return { alt, bearing: (az + Math.PI) % (2 * Math.PI) };
}
function fromFrame(f, a, b) {
  return [f.cx + Math.cos(f.theta) * a - Math.sin(f.theta) * b, f.cy + Math.sin(f.theta) * a + Math.cos(f.theta) * b];
}
function cellOf(scene, x, y) {
  const f = (v) => Math.floor(v);
  const i = f((x + scene.radius) / scene.cell), j = f((scene.radius - y) / scene.cell);
  if (i < 0 || j < 0 || i >= scene.g || j >= scene.g) return -1;
  return j * scene.g + i;
}
function refShade(scene, date) {
  const s = refSun(date, scene.lat, scene.lng);
  if (s.alt <= 0) return null;
  const ex = Math.sin(s.bearing), ny = Math.cos(s.bearing), rise = Math.tan(s.alt);
  let maxTop = -Infinity;
  for (const t of scene.top) if (t !== -32768 && t / 10 > maxTop) maxTop = t / 10;
  let shaded = 0, n = 0;
  for (const f of scene.courts) {
    const h0 = f.z + 1, maxD = Math.min(scene.radius * 1.5, (maxTop - h0) / rise);
    for (let a = -f.pl / 2 + 0.6; a <= f.pl / 2; a += 1.4)
      for (let b = -f.pw / 2 + 0.5; b <= f.pw / 2; b += 1.4) {
        n++;
        const [px, py] = fromFrame(f, a, b);
        for (let dd = 0.5; dd < maxD; dd += 0.5) {
          const c = cellOf(scene, px + ex * dd, py + ny * dd);
          if (c < 0) break;
          if (scene.top[c] !== -32768 && scene.top[c] / 10 > h0 + dd * rise) { shaded++; break; }
        }
      }
  }
  return n ? shaded / n : 0;
}

// ---- checks ---------------------------------------------------------------
const fails = [];
const ok = (name, cond, detail) => { if (!cond) fails.push(name + (detail ? ' — ' + detail : '')); };
const wrap = (a) => Math.abs(((a + Math.PI) % (2 * Math.PI)) - Math.PI);

const iso = (h) => new Date(Date.UTC(2026, 5, 21, h + 7)).toISOString(); // h = PDT hour
const flat = { lat: 47.6, lng: -122.3, radius: 110, cell: 2, g: 110,
  courts: [{ sport: 'tennis', cx: 0, cy: 0, theta: 0, pl: 23.77, pw: 10.97, AL: 36.6, AW: 18.3, z: 0 }],
  top: new Array(110 * 110).fill(-32768) };
const buried = { ...flat, top: new Array(110 * 110).fill(32767) };

const run = (fn, ...a) => { try { return fn(...a); } catch (e) { return { err: String(e.message || e) }; } };
const num = (v) => typeof v === 'number' && Number.isFinite(v);

for (const [name, date, want] of [['noon', iso(13), 0], ['night', iso(2), null]]) {
  const got = run(shadeAt, flat, new Date(date));
  ok(`flat venue is unshaded (${name})`, want === null ? got === null : got === want, `got ${got}`);
}
ok('enclosed venue is fully shaded', run(shadeAt, buried, new Date(iso(13))) === 1, `got ${run(shadeAt, buried, new Date(iso(13)))}`);

for (const h of [9, 13, 18]) {
  const date = new Date(iso(h));
  const r = refSun(date, 47.6, -122.3), g = run(sunPosition, date, 47.6, -122.3);
  if (!g || typeof g.alt !== 'number') { ok(`sun position (${h}:00)`, false, `got ${JSON.stringify(g)}`); continue; }
  ok(`sun altitude (${h}:00 PDT)`, Math.abs(g.alt - r.alt) < 0.05, `got ${g.alt.toFixed(3)}, want ${r.alt.toFixed(3)}`);
  ok(`sun bearing (${h}:00 PDT)`, wrap(g.bearing - r.bearing) < 0.15, `got ${g.bearing.toFixed(2)}, want ${r.bearing.toFixed(2)}`);
}

for (const v of ['kenmore-inglemoor-hs', 'bellevue-crossroads-park', 'kirkland-peter-kirk-park']) {
  const scene = JSON.parse(readFileSync(join(ws, 'venues', v + '.json'), 'utf8'));
  const vals = {};
  for (const h of [6, 13, 20]) {
    const date = new Date(iso(h));
    const want = refShade(scene, date), got = run(shadeAt, scene, date);
    vals[h] = got;
    ok(`${v} @ ${h}:00 matches physics`, num(got) && Math.abs(got - want) <= 0.25, `got ${typeof got === 'number' ? got.toFixed(2) : got}, want ${want.toFixed(2)}`);
  }
  if (num(vals[6]) && num(vals[13])) ok(`${v}: morning is shadier than midday`, vals[6] > vals[13] + 0.25, `${vals[6]} vs ${vals[13]}`);
  if (num(vals[20]) && num(vals[13])) ok(`${v}: evening is shadier than midday`, vals[20] > vals[13] + 0.25, `${vals[20]} vs ${vals[13]}`);
}

if (fails.length) { console.log(`${fails.length} check(s) failed`); for (const f of fails) console.log('  ' + f); process.exit(1); }
console.log('all behaviour checks passed');
