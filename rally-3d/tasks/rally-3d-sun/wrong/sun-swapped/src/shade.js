import { sunPosition } from './sun.js';
import { fromFrame, cellOf } from './geometry.js';

/** Share of court samples shaded by trees/buildings at `date`, or null if dark. */
export function shadeAt(scene, date) {
  const s = sunPosition(date, scene.lat, scene.lng);
  if (s.alt <= 0) return null;
  const ex = Math.sin(s.bearing);
  const ny = Math.cos(s.bearing);
  const rise = Math.tan(s.alt);
  let maxTop = -32768 / 10;
  for (const v of scene.top) if (v !== -32768 && v / 10 > maxTop) maxTop = v / 10;
  let shaded = 0;
  let n = 0;
  for (const f of scene.courts) {
    const h0 = f.z + 1;
    const maxD = Math.min(scene.radius * 1.5, (maxTop - h0) / rise);
    for (let a = -f.pl / 2 + 0.6; a <= f.pl / 2; a += 1.4)
      for (let b = -f.pw / 2 + 0.5; b <= f.pw / 2; b += 1.4) {
        const [px0, py0] = fromFrame(f, a, b);
        n++;
        for (let d = 0.5; d < maxD; d += 0.5) {
          const c = cellOf(scene, px0 + ex * d, py0 + ny * d);
          if (c < 0) break;
          if (scene.top[c] !== -32768 && scene.top[c] / 10 > h0 + d * rise) {
            shaded++;
            break;
          }
        }
      }
  }
  return n ? shaded / n : 0;
}
