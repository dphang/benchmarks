  // ---------- shade tracing against the LiDAR obstacle grid ----------
  const samples = data.courts.map((f) => {
    const out: [number, number][] = [];
    for (let a = -f.pl / 2 + 0.6; a <= f.pl / 2; a += 1.4)
      for (let b = -f.pw / 2 + 0.5; b <= f.pw / 2; b += 1.4) out.push(fromFrame(f, a, b));
    return out;
  });
  const traced = (s: SunPos): number => {
    const ex = Math.sin(s.bearing);
    const ny = Math.cos(s.bearing);
    const rise = Math.tan(s.alt);
    let shaded = 0;
    let n = 0;
    data.courts.forEach((f, ci) => {
      const h0 = f.z + 1;
      const maxD = Math.min(R * 1.5, (maxTop - h0) / rise);
      for (const [px0, py0] of samples[ci]) {
        n++;
        for (let d = 0.5; d < maxD; d += 0.5) {
          const c = cellOf(px0 + ex * d, py0 + ny * d);
          if (c < 0) break;
          if (topDm[c] !== -32768 && topDm[c] / 10 > h0 + d * rise) {
            shaded++;
            break;
          }
        }
      }
    });
    return n ? shaded / n : 0;
  };
