// Sun position (the SunCalc formulas) and local-time helpers for the 3D view.

const rad = Math.PI / 180;

export interface SunPos {
  /** Elevation above the horizon, radians. */
  alt: number;
  /** Compass bearing toward the sun, radians clockwise from north. */
  bearing: number;
}

export function sunPosition(date: Date, lat: number, lng: number): SunPos {
  const d = date.valueOf() / 86_400_000 - 0.5 + 2_440_588 - 2_451_545;
  const M = rad * (357.5291 + 0.98560028 * d);
  const C = rad * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
  const L = M + C + rad * 102.9372 + Math.PI;
  const e = rad * 23.4397;
  const dec = Math.asin(Math.sin(e) * Math.sin(L));
  const ra = Math.atan2(Math.sin(L) * Math.cos(e), Math.cos(L));
  const H = rad * (280.16 + 360.9856235 * d) + rad * lng - ra;
  const phi = rad * lat;
  const alt = Math.asin(
    Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H),
  );
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
  return { alt, bearing: (az + Math.PI) % (2 * Math.PI) };
}

function tzOffset(t: number, tz: string): number {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
      .formatToParts(t)
      .map((x) => [x.type, x.value]),
  );
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - t;
}

/** The instant that is `minutes` after local midnight on `ymd` (YYYY-MM-DD) in `tz`. */
export function zonedTime(ymd: string, minutes: number, tz: string): Date {
  const [y, m, d] = ymd.split('-').map(Number);
  const guess = Date.UTC(y, m - 1, d, 0, minutes);
  return new Date(guess - tzOffset(guess - tzOffset(guess, tz), tz));
}

/** Today's date (YYYY-MM-DD) in `tz`. */
export function localYmd(date: Date, tz: string): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: tz }).format(date);
}
