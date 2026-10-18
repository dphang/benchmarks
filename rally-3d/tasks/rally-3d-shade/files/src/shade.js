import { sunPosition } from './sun.js';
import { fromFrame, cellOf } from './geometry.js';

// Share of court samples shaded by trees/buildings at `date`, or null when the
// sun is down. Port the trace in reference/viewer-shade.ts. Not implemented yet.

export function shadeAt(scene, date) {
  throw new Error('shadeAt is not implemented');
}
