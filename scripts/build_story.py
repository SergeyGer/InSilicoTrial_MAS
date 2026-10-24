#!/usr/bin/env python
"""Render a scrollytelling summary of one demo run.

The page tells the same story the dashboard tells, but as a short narrative: the
question, the cohort, the exposure and effect, the safety signal, the estimate and the
verdict. Every number comes from the run manifest, so the page cannot drift away from
the run it describes.

The output is a single self-contained HTML file: inline CSS, inline JavaScript, inline
SVG, no external fonts, scripts or images. That keeps it usable from `file://`, on
GitHub Pages, and inside an air-gapped review.

Usage:
    python scripts/build_story.py --manifest artifacts/pages/RUN-*/run_manifest.json \\
        --out _site/story.html --dashboard-url https://sergeyger.github.io/InSilicoTrial_MAS/
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
from typing import Any

#: Sticky-card synchronisation: no framework, no dependencies, degrades without JS.
SCRIPT = """  (function () {
    var steps = Array.prototype.slice.call(document.querySelectorAll('.step[data-step]'));
    if (!steps.length || !('IntersectionObserver' in window)) return;
    var value = document.getElementById('visual-value');
    var unit = document.getElementById('visual-unit');
    var chart = document.getElementById('visual-chart');
    var progress = document.getElementById('visual-progress');
    var counter = document.getElementById('visual-counter');
    var total = steps.length;
    var active = null;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting || entry.target === active) return;
        active = entry.target;
        value.textContent = active.dataset.value;
        unit.textContent = active.dataset.unit;
        chart.innerHTML = active.dataset.chart || '';
        var index = steps.indexOf(active) + 1;
        if (progress) progress.style.width = (index / total * 100).toFixed(1) + '%';
        if (counter) counter.textContent = index + ' / ' + total;
      });
    }, { rootMargin: '-45% 0px -45% 0px' });
    steps.forEach(function (step) { observer.observe(step); });
  })();"""


def _get(payload: dict[str, Any], key: str, default: Any = 0.0) -> Any:
    value = payload.get(key, default)
    return default if value is None else value


def _mmhg(value: float) -> str:
    return f"{value:+.1f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _bar_width(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0.0
    return max(2.0, min(100.0, abs(value) / maximum * 100.0))


def build_story(manifest: dict[str, Any], *, dashboard_url: str, repo_url: str) -> str:
    """Return the HTML page for a run manifest."""
    overview = manifest.get("analysis_overview") or {}
    screening = manifest.get("screening") or {}
    allocation = manifest.get("allocation") or {}
    replay = manifest.get("replay") or {}
    provenance = manifest.get("provenance") or {}
    config = manifest.get("config") or {}

    control = str(overview.get("control_arm") or "placebo")
    protocol_id = str(overview.get("protocol_id") or "the protocol")
    endpoint = str(overview.get("primary_endpoint") or "the primary endpoint").replace("_", " ")

    enrolled = int(_get(screening, "enrolled", _get(overview, "n_patients")))
    screened = int(_get(screening, "screened", enrolled))
    failures = int(_get(screening, "screen_failures", max(0, screened - enrolled)))
    failure_rate = float(_get(screening, "screen_failure_rate", 0.0))
    reasons = screening.get("failure_reasons") or {}

    effect_high = float(_get(overview, "effect_high_dose_vs_placebo"))
    effect_low = float(_get(overview, "effect_low_dose_vs_placebo"))
    change_control = float(_get(overview, f"sbp_change_{control}"))
    change_low = float(_get(overview, "sbp_change_low_dose"))
    change_high = float(_get(overview, "sbp_change_high_dose"))
    responders_high = float(_get(overview, "responder_rate_high_dose"))
    responders_low = float(_get(overview, "responder_rate_low_dose"))
    responders_control = float(_get(overview, f"responder_rate_{control}"))
    grade3_high = float(_get(overview, "grade3_rate_high_dose"))
    adverse_events = int(_get(overview, "n_adverse_events"))
    grade3_patients = int(_get(overview, "grade3_plus_patients"))
    stopping = int(_get(overview, "stopping_rules_triggered"))
    alerts = int(_get(overview, "safety_alerts"))

    max_effect = max(abs(effect_high), abs(effect_low), 1e-9)
    max_change = max(abs(change_control), abs(change_low), abs(change_high), 1e-9)
    max_responder = max(responders_high, responders_low, responders_control, 1e-9)

    run_id = str(manifest.get("run_id") or "")
    seed = replay.get("seed", "")
    digest = str(replay.get("protocol_digest") or "")
    llm_mode = str(provenance.get("llm_mode") or config.get("llm_mode") or "off")
    engine = str(manifest.get("engine") or config.get("engine") or "")

    reasons_text = ", ".join(
        f"{str(name).replace('_', ' ')} ({count})" for name, count in reasons.items()
    ) or "none recorded"

    # The verdict is derived, not asserted: it reports the numbers and their direction.
    if abs(effect_high) > abs(effect_low) and responders_high >= 0.5 and stopping == 0:
        verdict = (
            f"The high dose separates from {control} by {abs(effect_high):.1f} mmHg with a "
            f"{_pct(responders_high)} responder rate, and no stopping rule fired. On this synthetic "
            "cohort it is the arm worth taking into a real dose-finding study."
        )
    elif abs(effect_low) > abs(effect_high):
        verdict = (
            f"The low dose separates from {control} more than the high dose and no stopping rule "
            f"fired; the readout would send the low dose forward and revisit the high-dose exposure."
        )
    else:
        verdict = (
            "The arms separate from the control, but the readout does not support choosing a dose "
            "on this cohort alone — the design would be revised before a real study."
        )

    bars_exposure = "".join(
        f'<div class="bar"><span class="bar-label">{html.escape(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{_bar_width(value, max_change):.1f}%"></span></span>'
        f'<span class="bar-value">{_mmhg(value)} mmHg</span></div>'
        for label, value in (
            (f"{control} (control)", change_control),
            ("low dose", change_low),
            ("high dose", change_high),
        )
    )
    bars_effect = "".join(
        f'<div class="bar"><span class="bar-label">{html.escape(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{_bar_width(value, max_effect):.1f}%"></span></span>'
        f'<span class="bar-value">{_mmhg(value)} mmHg</span></div>'
        for label, value in (("low vs control", effect_low), ("high vs control", effect_high))
    )
    bars_responders = "".join(
        f'<div class="bar"><span class="bar-label">{html.escape(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{_bar_width(value, max_responder):.1f}%"></span></span>'
        f'<span class="bar-value">{_pct(value)}</span></div>'
        for label, value in (
            (f"{control} (control)", responders_control),
            ("low dose", responders_low),
            ("high dose", responders_high),
        )
    )

    steps = [
        {
            "id": "screening",
            "eyebrow": "Step 1",
            "title": "The cohort",
            "value": f"{enrolled}",
            "unit": f"of {screened} screened enrolled",
            "chart": "",
            "text": (
                f"The protocol was applied to {screened} synthetic patients. {failures} failed "
                f"screening ({_pct(failure_rate)}) — {html.escape(reasons_text)} — which is the kind "
                "of attrition a real study meets before the first dose is given."
            ),
        },
        {
            "id": "allocation",
            "eyebrow": "Step 2",
            "title": "Who got what",
            "value": f"{int(_get(allocation, 'high_dose'))}",
            "unit": "patients on the high dose",
            "chart": "",
            "text": (
                "Randomisation is balanced by a seeded generator: "
                + ", ".join(
                    f"{key.replace('_', ' ')} {value}"
                    for key, value in sorted(allocation.items(), key=lambda item: str(item[0]))
                )
                + ". The same seed always produces the same allocation."
            ),
        },
        {
            "id": "exposure",
            "eyebrow": "Step 3",
            "title": "Exposure and effect",
            "value": _mmhg(change_high),
            "unit": f"mmHg {endpoint} on the high dose",
            "chart": f'<div class="chart">{bars_exposure}</div>',
            "text": (
                f"Mean change in {endpoint} over {int(_get(config, 'epochs', 0))} epochs: "
                f"{_mmhg(change_control)} on {control}, {_mmhg(change_low)} on the low dose and "
                f"{_mmhg(change_high)} on the high dose. Responder rates: {_pct(responders_control)}, "
                f"{_pct(responders_low)}, {_pct(responders_high)}."
            ),
        },
        {
            "id": "safety",
            "eyebrow": "Step 4",
            "title": "Safety",
            "value": f"{adverse_events}",
            "unit": "adverse events recorded",
            "chart": "",
            "text": (
                f"{grade3_patients} patients reached grade 3 or above; the grade-3 rate on the high "
                f"dose is {_pct(grade3_high)}. Stopping rules triggered: {stopping}. Safety alerts: "
                f"{alerts}. This is what an independent board would look at first."
            ),
        },
        {
            "id": "estimate",
            "eyebrow": "Step 5",
            "title": "The estimate",
            "value": _mmhg(effect_high),
            "unit": "mmHg versus control, high dose",
            "chart": f'<div class="chart">{bars_effect}</div>',
            "text": (
                f"The high dose separates from {control} by {abs(effect_high):.1f} mmHg, the low dose "
                f"by {abs(effect_low):.1f} mmHg. The readout also carries intervals, a stratified "
                "analysis and multiplicity control — see the dashboard for those."
            ),
        },
        {
            "id": "verdict",
            "eyebrow": "Step 6",
            "title": "The verdict",
            "value": _mmdh_short(effect_high),
            "unit": "the signal that survived",
            "chart": f'<div class="chart">{bars_responders}</div>',
            "text": html.escape(verdict),
        },
    ]

    steps_html = "\n".join(
        f"""      <section class="step" data-step="{step['id']}" data-value="{html.escape(step['value'])}"
               data-unit="{html.escape(step['unit'])}" data-chart="{html.escape(step['chart'])}">
        <p class="eyebrow">{html.escape(step['eyebrow'])} · {html.escape(step['title'])}</p>
        <h2>{html.escape(step['title'])}</h2>
        <p class="figures"><strong>{html.escape(step['value'])}</strong> {html.escape(step['unit'])}</p>
        <p>{step['text']}</p>
      </section>"""
        for step in steps
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Would this trial have worked? · {html.escape(protocol_id)}</title>
<meta name="description" content="A six-step reading of one in-silico trial run: cohort, exposure, effect, safety and verdict.">
<style>
  :root {{
    --ink: #16202a; --muted: #5b6b7b; --line: #d8e0e8; --bg: #f7f9fb; --panel: #ffffff;
    --accent: #1c7c54; --accent-soft: #e6f2ec; --warn: #8a5300; --warn-soft: #fff4e0;
    --shadow: 0 12px 32px rgba(22, 32, 42, .08);
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0; background: var(--bg); color: var(--ink);
    font: 17px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  a {{ color: var(--accent); }}
  .wrap {{ max-width: 1120px; margin: 0 auto; padding: 0 24px; }}
  header.hero {{ padding: 12vh 0 10vh; }}
  .kicker {{ text-transform: uppercase; letter-spacing: .12em; font-size: 13px; color: var(--muted); }}
  h1 {{ font-size: clamp(34px, 5vw, 60px); line-height: 1.05; margin: 16px 0 20px; letter-spacing: -.02em; }}
  .lede {{ font-size: clamp(18px, 2.2vw, 22px); color: var(--muted); max-width: 44ch; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 28px; font-size: 14px; color: var(--muted); }}
  .meta span {{ border: 1px solid var(--line); border-radius: 999px; padding: 5px 12px; background: var(--panel); }}
  .scrolly {{ display: grid; grid-template-columns: minmax(0, 44%) minmax(0, 56%); gap: 48px; align-items: start; }}
  .visual {{ position: sticky; top: 0; height: 100vh; display: flex; align-items: center; }}
  .card {{
    width: 100%; background: var(--panel); border: 1px solid var(--line); border-radius: 20px;
    padding: 32px; box-shadow: var(--shadow); min-height: 240px;
    display: grid; align-content: center; position: relative; overflow: hidden;
  }}
  .progress {{ position: absolute; top: 0; left: 0; right: 0; height: 4px; background: var(--accent-soft); }}
  .progress span {{ display: block; height: 100%; width: 0; background: var(--accent); transition: width .35s ease; }}
  .counter {{ position: absolute; top: 18px; right: 22px; font-size: 12px; color: var(--muted);
              letter-spacing: .08em; text-transform: uppercase; }}
  .card .value {{ font-size: clamp(44px, 6vw, 76px); line-height: 1; font-weight: 700; letter-spacing: -.03em; }}
  .card .unit {{ color: var(--muted); margin-top: 10px; font-size: 15px; }}
  .chart {{ margin-top: 26px; display: grid; gap: 12px; }}
  .bar {{ display: grid; grid-template-columns: 9.5em 1fr auto; gap: 10px; align-items: center; font-size: 13px; }}
  .bar-label {{ color: var(--muted); }}
  .bar-track {{ background: var(--accent-soft); border-radius: 999px; height: 10px; overflow: hidden; }}
  .bar-fill {{ display: block; height: 100%; background: var(--accent); border-radius: 999px; }}
  .bar-value {{ font-variant-numeric: tabular-nums; color: var(--ink); }}
  .steps {{ padding: 8vh 0 16vh; display: grid; gap: 40vh; }}
  .step {{ background: transparent; }}
  .step .eyebrow {{ text-transform: uppercase; letter-spacing: .1em; font-size: 12px; color: var(--accent); margin: 0 0 8px; }}
  .step h2 {{ margin: 0 0 10px; font-size: clamp(24px, 3vw, 34px); letter-spacing: -.02em; }}
  .step .figures {{ display: none; }}
  .step p:last-child {{ color: var(--muted); }}
  .caveat {{ border-left: 3px solid var(--warn); background: var(--warn-soft); padding: 16px 20px; border-radius: 0 12px 12px 0; color: #4a3200; }}
  footer {{ border-top: 1px solid var(--line); padding: 40px 0 64px; color: var(--muted); font-size: 14px; }}
  footer code {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 2px 6px; }}
  .links {{ display: flex; flex-wrap: wrap; gap: 18px; margin: 18px 0 26px; }}
  @media (max-width: 900px) {{
    .scrolly {{ grid-template-columns: 1fr; gap: 0; }}
    .visual {{ display: none; }}
    .steps {{ gap: 40px; padding: 4vh 0 10vh; }}
    .step .figures {{ display: block; font-size: 15px; }}
    .step .figures strong {{ font-size: 30px; }}
    .chart {{ display: none; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    html {{ scroll-behavior: auto; }}
    .card {{ transition: none; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="kicker">In-silico trial readout · {html.escape(protocol_id)}</p>
    <h1>Would this trial have worked?</h1>
    <p class="lede">One protocol, {screened} synthetic patients, {int(_get(config, 'epochs', 0))} epochs.
    Scroll to read what the simulation says — cohort, exposure, effect, safety, verdict.</p>
    <div class="meta">
      <span>run {html.escape(run_id)}</span>
      <span>seed {html.escape(str(seed))}</span>
      <span>protocol digest {html.escape(digest)}</span>
      <span>LLM mode: {html.escape(llm_mode)}</span>
    </div>
  </header>

  <div class="scrolly">
    <div class="visual" aria-hidden="true">
      <div class="card">
        <div class="progress" aria-hidden="true"><span id="visual-progress"></span></div>
        <div class="counter" id="visual-counter">1 / {len(steps)}</div>
        <div class="value" id="visual-value">{html.escape(steps[0]['value'])}</div>
        <div class="unit" id="visual-unit">{html.escape(steps[0]['unit'])}</div>
        <div id="visual-chart"></div>
      </div>
    </div>
    <div class="steps">
{steps_html}
      <section class="step">
        <p class="eyebrow">Before you quote any of this</p>
        <h2>What this is, and what it is not</h2>
        <p class="caveat">Synthetic data, decision support for trial design — not clinical evidence.
        The cohort is generated, the physiology is modelled, and no patient exists. Use it to
        compare designs, not to claim safety or efficacy.</p>
        <div class="links">
          <a href="{html.escape(dashboard_url)}">Full dashboard</a>
          <a href="{html.escape(dashboard_url)}trial_report.html">Narrative report</a>
          <a href="{html.escape(repo_url)}">Repository</a>
          <a href="{html.escape(repo_url)}/blob/main/docs/LLM_QUALITY.md">How the LLM is kept honest</a>
        </div>
      </section>
    </div>
  </div>

  <footer>
    <p>Reproduce this page: <code>make demo</code> then
    <code>python scripts/build_story.py --manifest artifacts/&lt;run&gt;/run_manifest.json --out story.html</code></p>
    <p>Engine: {html.escape(engine)} · every figure above is read from the run manifest, so the page
    cannot drift away from the run it describes.</p>
  </footer>
</div>
<script>
{SCRIPT}
</script>
</body>
</html>
"""


def _mmdh_short(value: float) -> str:
    """Short signed figure for the verdict card."""
    return f"{value:+.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a scrollytelling story from a run manifest.")
    parser.add_argument("--manifest", required=True, help="path to run_manifest.json")
    parser.add_argument("--out", required=True, help="path of the HTML file to write")
    parser.add_argument(
        "--dashboard-url",
        default="https://sergeyger.github.io/InSilicoTrial_MAS/",
        help="public URL of the dashboard (used for links)",
    )
    parser.add_argument(
        "--repo-url",
        default="https://github.com/SergeyGer/InSilicoTrial_MAS",
        help="public URL of the repository",
    )
    args = parser.parse_args()

    manifest = json.loads(pathlib.Path(args.manifest).read_text())
    page = build_story(manifest, dashboard_url=args.dashboard_url, repo_url=args.repo_url)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(page) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
