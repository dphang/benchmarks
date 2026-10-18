import { sunPosition } from './sun.js';
import { fromFrame, cellOf } from './geometry.js';

// Plausible mistake: treat any obstacle in the sunward path as shade, without
// checking whether it is tall enough to block the sun at that distance.
export function shadeAt(scene, date) {
  const s = sunPosition(date, scene.lat, scene.lng);
  if (s.alt <= 0) return null;
  const ex = Math.sin(s.bearing);
  const ny = Math.cos(s.bearing);
  let shaded = 0;
  let n = 0;
  for (const f of scene.courts) {
    for (let a = -f.pl / 2 + 0.6; a <= f.pl / 2; a += 1.4)
      for (let b = -f.pw / 2 + 0.5; b <= f.pw / 2; b += 1.4) {
        const [px0, py0] = fromFrame(f, a, b);
        n++;
        for (let d = 0.5; d < scene.radius * 1.5; d += 0.5) {
          const c = cellOf(scene, px0 + ex * d, py0 + ny * d);
          if (c < 0) break;
          if (scene.top[c] !== -32768) {
            shaded++;
            break;
          }
        }
      }
  }
  return n ? shaded / n : 0;
}
