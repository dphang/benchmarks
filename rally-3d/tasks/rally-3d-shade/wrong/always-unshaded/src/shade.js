import { sunPosition } from './sun.js';

// Plausible mistake: never trace at all, just report "no shade".
export function shadeAt(scene, date) {
  const s = sunPosition(date, scene.lat, scene.lng);
  return s.alt <= 0 ? null : 0;
}
