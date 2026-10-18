# rally-court3d — headless shade precompute

Rally's court pages have a **3D view** (`/courts/<slug>/3d`) that draws each venue
from LiDAR — ground, tree crowns, buildings, courts — lights it with the real
sun, and traces how much of the courts the trees and buildings shade as the day
goes on. Today that trace runs in the browser with three.js; we want the same
numbers on the server so we can precompute a shade forecast per court.

This package is the headless port. It is plain Node (ESM), no three.js, no
network.

## What to implement

Two modules, both exported as named functions:

### `src/sun.js` — `sunPosition(date, lat, lng) -> { alt, bearing }`

The SunCalc formulas, exactly as the browser uses them (`reference/sun.ts` is the
current implementation — port it). `alt` is elevation above the horizon in
radians, `bearing` is the compass bearing toward the sun in radians, clockwise
from north.

### `src/shade.js` — `shadeAt(scene, date) -> number | null`

How much of the courts a venue's trees and buildings shade at `date`:

- Compute `s = sunPosition(date, scene.lat, scene.lng)`.
- If the sun is at or below the horizon (`s.alt <= 0`) return `null`.
- Otherwise return the **share of court sample points in shadow** (0–1).

The trace, matching the browser (`reference/viewer-shade.ts`):

```
ex = sin(s.bearing);  ny = cos(s.bearing);  rise = tan(s.alt)
```

From each sample point, walk toward the sun in steps of **0.5 m** starting at
`d = 0.5`, up to `maxD = min(scene.radius * 1.5, (maxTop - h0) / rise)` where
`h0 = court.z + 1` and `maxTop` is the tallest obstacle in the venue (metres).
At each step look up the obstacle grid at `(px + ex*d, py + ny*d)`. A point is
**shaded** if that cell holds an obstacle taller than `h0 + d * rise`. Stop the
walk early if the step leaves the grid.

Sample points, per court: `a` from `-pl/2 + 0.6` to `pl/2` and `b` from
`-pw/2 + 0.5` to `pw/2`, both in **1.4 m** steps, mapped to world coordinates by

```
fromFrame(court, a, b) = [ court.cx + cos(theta)*a - sin(theta)*b,
                           court.cy + sin(theta)*a + cos(theta)*b ]
```

The result is the count of shaded samples over the total number of samples across
every court.

### `src/geometry.js` — `fromFrame` and `cellOf` (given)

Cell lookup for the obstacle grid:

```
cellOf(scene, x, y):
  i = floor((x + radius) / cell);  j = floor((radius - y) / cell)
  out of range        -> -1
  otherwise           -> j * g + i
```

## The venue scene

`venues/*.json` are baked exactly like the browser's scenes, but the obstacle
grid is stored plainly instead of base64:

```
{ name, lat, lng, radius,   // metres
  cell,                     // metres per grid cell
  g,                        // grid width in cells (grid is g x g)
  courts: [ { sport, cx, cy, theta, pl, pw, AL, AW, z } ],
  top: [ ... g*g ints ... ] // obstacle top height, decimetres; -32768 = none
}
```

Obstacle heights are in **decimetres** (divide by 10 for metres), same as the
browser's `topDm`. Note `cell` is not always 1 m, so `cellOf` is the general
form of the browser's lookup.

## Tests

`test/shade.test.js` has a few cases. `node --test`.
