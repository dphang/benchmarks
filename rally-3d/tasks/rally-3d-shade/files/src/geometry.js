// Given helpers: court frame -> world, and obstacle-grid cell lookup.

export const deg = Math.PI / 180;

/** Court-local (a along the length, b across) -> world [x, y]. */
export function fromFrame(f, a, b) {
  return [
    f.cx + Math.cos(f.theta) * a - Math.sin(f.theta) * b,
    f.cy + Math.sin(f.theta) * a + Math.cos(f.theta) * b,
  ];
}

/** Obstacle-grid cell index for a world point, or -1 outside the grid. */
export function cellOf(scene, x, y) {
  const i = Math.floor((x + scene.radius) / scene.cell);
  const j = Math.floor((scene.radius - y) / scene.cell);
  if (i < 0 || j < 0 || i >= scene.g || j >= scene.g) return -1;
  return j * scene.g + i;
}
