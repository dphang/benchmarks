# rally-court3d — headless shade (work out the physics)

Rally's court pages have a **3D view** that shows how much of each venue's courts
the trees and buildings shade as the sun moves. Today that runs in the browser
with three.js; we want the same on the server to precompute a shade forecast.

Unlike a straight port, this package does **not** ship the browser code — this is
plain Node (ESM, no three.js, no network), and the geometry is written up below.
Implement it from the description.

## What to implement

### `src/sun.js` — `sunPosition(date, lat, lng) -> { alt, bearing }`

Where the sun is at `date` for a venue at `(lat, lng)`. Use the standard SunCalc
formulas (mean anomaly, equation of the centre, ecliptic longitude, declination,
right ascension, hour angle). `alt` is elevation above the horizon in radians;
`bearing` is the compass bearing toward the sun in radians, clockwise from north.

### `src/shade.js` — `shadeAt(scene, date) -> number | null`

The share of the courts that is in shadow at `date`:

- If the sun is at or below the horizon (`alt <= 0`) return `null`.
- Otherwise return the fraction (0–1) of court sample points in shadow.

A sample point is **in shadow** when, looking from the point **toward the sun**,
an obstacle stands in the way and is **tall enough to block the sun's ray** — a
point at height `h` blocks the sun only if the obstacle's top rises above the
ray, which climbs with the sun's elevation as you move away from the point.
Obstacles whose top is below the ray do not count; neither do points where the
venue ends before any obstacle is reached.

Cover each court's playing area with a grid of sample points; a court's
footprint is `pl` by `pw` metres, centred on `(cx, cy)` and rotated by `theta`.
`src/geometry.js` gives you two helpers:

```
fromFrame(f, a, b)  // court-local (a along length, b across) -> world [x, y]
cellOf(scene, x, y) // obstacle-grid cell index for a world point, or -1 outside
```

## The venue scene

`venues/*.json` are baked from LiDAR:

```
{ name, lat, lng, radius,   // metres
  cell,                     // metres per grid cell
  g,                        // grid width in cells (grid is g x g)
  courts: [ { sport, cx, cy, theta, pl, pw, AL, AW, z } ],  // z = court elevation, m
  top: [ ... g*g ints ... ] // obstacle top height, decimetres; -32768 = none
}
```

Obstacle height in metres is `top[c] / 10`.

## Tests

`test/shade.test.js` has a few cases. `node --test`.
