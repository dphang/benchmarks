# Charts & shareable images — style guide

How figures in this repo (and anything meant to be posted to X) should look. The
goal is a **dry, credible benchmark figure** — something a dev dumped out of a
script — not a glossy AI-generated dashboard card. When in doubt, make it plainer.

## Voice (this is what usually reads "AI")

- **Terse and factual.** One line of context, then let the data talk. No
  "here's what this means", no "isn't it interesting", no balanced editorial
  sentences ("native binaries stay lean; VM runtimes carry their weight" — cut it).
- **Short declaratives.** Avoid strings of em-dash explanations. No hedging.
- **Plain words over jargon on the figure.** Say "memory", not "RSS"; "per turn",
  not "latency". Keep precise terms (RSS, p95, etc.) in the README prose, not on
  the chart.
- **State the setup**, briefly: machine (e.g. `M1 Max / 32 GB`), number of runs,
  what was excluded. Credibility comes from the method line, not adjectives.
- No emoji. No "Figure 1:" academic chrome unless asked.

## Look

- **Flat, not glossy.** No drop shadows, no big rounded "cards", no gradients, no
  glow. Thin hairline rules (1px) to separate sections. It should look utilitarian.
- **Monospace** for labels, numbers, and axis titles (`ui-monospace, "SF Mono",
  "JetBrains Mono", Menlo, monospace`). This is what sells the "real benchmark
  output" feel. A sans title is fine; mono everywhere else.
- **Dark background**, kept flat — reads well on X.
- **One meaningful color split** when there's a story (here: native vs VM runtime),
  with a small legend. Never rainbow; never color just to decorate.
- **Every value legible.** Long bar values sit *outside* the bar; put a value
  inside a bar only for the near-max one (where outside would overflow), and then
  use dark text on the light fill. Define every asterisk in the footnote
  (e.g. `aider* = edit-block agent, per message`).

## Palette (dark)

| role | hex |
|---|---|
| background | `#111110` |
| bar track | `#201f1d` |
| text | `#e8e6df` |
| text muted | `#8a897f` |
| text faint | `#6b6a61` |
| hairline rule | `#35342f` |
| native (series) | `#27b985` |
| VM / interpreted (series) | `#ef7a43` |

## Rendering to PNG

Author an HTML file, render it with headless Chrome at 2× for retina:

```sh
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --default-background-color=00000000 \
  --window-size=1000,548 --screenshot=out.png "file://$PWD/chart.html"
```

- Content ~1000px wide → ~2000px PNG at 2×.
- **Size the window to the content** — no large empty margin below the figure
  (render, look at it, trim the height).
- Near 16:9 is ideal (X crops timeline previews to ~16:9); a bit taller is fine.

## Reference

`agent-overhead/overhead.png` is the canonical example of this style.
