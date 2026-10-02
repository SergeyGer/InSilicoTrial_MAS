# User interface: the readout dashboard and the live Studio

This document is the design record for the platform's user interface: what was
built, why those choices were made, what was rejected, and how to extend it.

---

## 1. Decision record

The platform has to be demonstrated to four different audiences, which rules out a
single "one size" UI:

| Audience | Setting | Needs |
| --- | --- | --- |
| Clinical / pharmacovigilance reviewer | reads a finished readout, often on paper | efficacy and safety tables with intervals, provenance, print/PDF |
| Data science / platform engineer | terminal, notebooks, CI | command-line artefacts, embeddable HTML, machine-readable JSON |
| Decision maker in a demo | projected screen, no setup time | launch a run live, see results appear, individual patient stories |
| Auditor / regulator-facing | air-gapped or restricted network | no CDN, no external service, reproducible artefacts |

Options considered:

| Option | Verdict | Why |
| --- | --- | --- |
| Streamlit app | rejected as the primary surface | adds a dependency to the runtime Databricks must install, awkward to embed in a notebook, and the artefact cannot be attached to a ticket |
| Dash / Plotly | rejected | same dependency problem, plus client-side rendering makes the printed output fragile |
| Databricks AI/BI dashboard | kept as an optional consumption layer (Gold tables are dashboard-ready) | excellent inside a workspace, useless in a customer demo or on a laptop |
| React/Vue SPA | rejected | needs a build step and a Node toolchain in CI for a readout that is fundamentally static |
| **Static self-contained HTML + optional stdlib Studio server** | **chosen** | zero dependencies, works offline, prints cleanly, embeds anywhere, and the live demo still exists |

Both surfaces are delivered:

* **`insilico-trial dashboard`** - renders a finished run into a single HTML file
  (`report/dashboard.html`) with inline SVG charts and ~4 KB of vanilla JS.
  Generated automatically after every run unless `export_dashboard: false`.
* **`insilico-trial studio`** - a live control panel served by Python's
  `http.server` on loopback: choose protocol and cohort size, press run, watch the
  phases progress, then open the dashboard of the run that just finished.

---

## 2. The readout dashboard

Six screens, ordered the way a trial team reads a study.

### Overview - "did it work, and is it safe?"

* Ten KPI cards: patients, Silver rows, primary effect, responder rate, adverse
  events, grade >= 3, serious AEs, safety alerts, duration, model.
* Primary-endpoint bar chart with 95% confidence intervals and a responder-rate chart.
* Forest plot of every declared endpoint against control, with the no-effect line
  as a mandatory reference.
* CONSORT-style disposition (screened, screen failures with reasons, randomised,
  completed, discontinued).
* Safety signals for DSMB review, or an explicit statement that nothing crossed
  the thresholds - including the reminder that absence of signal in a synthetic
  cohort is not evidence of safety.

### Efficacy

Per-arm summary table, dose-response with the fitted trend, the full comparison
table (effect, confidence interval, p, q, test, MCID), mean trajectories with 95%
confidence bands for systolic BP, diastolic BP and heart rate, the exposure (PK)
trajectory, and demographics.

### Safety

Adverse-event incidence heatmap (arm x preferred term, patient-level percentage),
CTCAE grade distribution per arm, the safety table with risk differences, Fisher
exact p-values and Benjamini-Hochberg q-values, the triggered stopping rules, and a
threshold monitor per rule and arm showing where the DSMB threshold was crossed.

### Patients - the digital twins behind the aggregates

A searchable list of individual agents, sorted by worst CTCAE grade, and a detail
pane with:

* the individual trajectory: administered dose (bars), plasma concentration
  (solid line), systolic BP (dashed line) and adverse-event markers per epoch;
* an epoch table (dose, concentration, SBP, DBP, HR, ALT, events);
* the persona's verbatim narration, quoted per epoch, marked as LLM-generated.

This is the screen that makes the multi-agent architecture tangible: the aggregate
is not a spreadsheet, it is 10,000 individual trajectories with genotypes,
adherence decisions and symptom reports.

### Reproducibility

Provenance (protocol digest, package version, git revision, seed, engine, storage
location, model version and digest, LLM policy and cache hits), execution
environment, data-quality audit (narration coverage, error rate, null columns),
the pre-run cost estimate, and the exact replay command.

### Lineage & traces

The agent -> table -> report lineage graph, LLM telemetry (calls, cache hit rate,
prompt and completion tokens, narration error rate), prompt-token and latency
distributions, and the Mermaid source of the lineage graph for pasting into
GitHub or a Databricks markdown cell.

---

## 3. The Studio (live demo)

`insilico-trial studio` serves a control panel on `127.0.0.1:8765` by default
(loopback only; `--host` is required to expose it further):

* protocol selector populated from `conf/*.yaml` plus whatever the base config
  points at;
* inputs for patients, cohorts, epochs, engine, LLM mode and seed, with
  "quick demo / 1k / 10k" presets;
* a run queue with live status, phase, an accessible progress bar and elapsed
  time, polled every 1.5 s;
* when a run finishes: headline metrics plus the full dashboard embedded in an
  iframe, and a link to the run directory.

The Studio drives the *real* pipeline - not a mock - through the same
`TrialSimulationPipeline` the job uses, and it reports progress through the
pipeline's `progress(phase, fraction)` callback. It runs each simulation in a
daemon thread with its own configuration object, so a demo run cannot corrupt the
profile on disk, and it disables MLflow logging and CDISC exports by default to
keep demo artefacts small.

---

## 4. Design system

Defined once in `src/insilico_trial_mas/ui/theme.py` as CSS custom properties, so
light and dark mode are a variable swap and inline SVG inherits the palette
without duplicating colours in Python.

| Token group | Intent |
| --- | --- |
| `--c-surface*`, `--c-border*` | neutral slate surfaces and hairlines |
| `--c-accent*` | one clinical blue, used for structure and the primary series |
| `--c-good/warn/critical` | semantic risk colours, used only for risk |
| `--c-series-1..6` | colour-blind-safe categorical series for charts |
| `--c-grade-1..5` | CTCAE severity ramp (pale blue to red) |

Conventions: tabular numerals for every metric, uppercase micro-labels with
letter-spacing instead of bold headings, hairline borders rather than heavy
shadows, direct labels instead of legends where the series count allows, and a
print stylesheet that expands all panels and hides navigation.

Accessibility: `role="img"` with `<title>`/`<desc>` on every chart, native HTML
tooltips (`<title>`) on data marks, `aria-selected` on tabs and patient rows,
`role="progressbar"` with `aria-valuenow` in the Studio, semantic tables with
`<th>` scopes, and no information conveyed by colour alone (values are printed in
the cells).

---

## 5. Usage

```bash
# Dashboard for a finished run (also written automatically by every run)
insilico-trial dashboard --run artifacts/final-demo --open
insilico-trial dashboard --run artifacts/            # newest run inside a directory
insilico-trial dashboard --run <dir> --out board.html --json

# Live Studio
insilico-trial studio --config conf/simulation_local.yaml --port 8765
insilico-trial studio --no-browser --port 8765        # for a container/CI demo
```

From Python:

```python
from insilico_trial_mas.ui.dashboard import write_dashboard
path = write_dashboard("artifacts/<RUN-ID>")           # -> report/dashboard.html
```

Inside a Databricks notebook the same file renders in-cell:

```python
from pathlib import Path
displayHTML(Path("artifacts/<RUN-ID>/report/dashboard.html").read_text())
```

Because the file is static and self-contained it also works from a Unity Catalog
volume, from DBFS, as a CI artefact, or attached to a ticket.

---

## 6. Extending it

| Task | Where |
| --- | --- |
| Rebrand (colours, density) | override the CSS variables in `ui/theme.py` |
| Add a KPI card or table | `ui/dashboard.py`, one of the `_*_panel` functions |
| Add a whole screen | append to `TAB_LABELS` and add a `_panel` function |
| Add a chart type | add a function to `ui/charts.py` returning `Chart(svg, title, description)` |
| Change what data reaches the UI | `ui/data.py` (`DashboardData`, one loader per artefact) |
| Embed in a customer portal | serve `dashboard.html` as a static file; the JSON payload is available from `report/trial_report.json` |

Non-goals: interactive filtering of the full cohort (the dashboard embeds a
150-patient sample plus cohort-wide aggregates), authentication (the Studio binds
to loopback and is a developer/demo tool - use the Databricks job and Unity
Catalog permissions for shared access), and live collaboration.

---

## 7. Verification

| Claim | Test |
| --- | --- |
| Chart primitives emit well-formed, accessible SVG | `tests/test_ui_dashboard.py::test_bar_with_ci_produces_valid_svg`, `test_dashboard_has_charts_and_accessible_titles` |
| Every screen renders | `test_dashboard_contains_every_screen` |
| The file is self-contained (no CDN, no external assets) | `test_dashboard_is_self_contained` |
| The dashboard degrades gracefully on a partial run | `test_payload_degrades_gracefully_without_optional_files` |
| Embedded JSON cannot break out of the script tag | `test_dashboard_embeds_patient_data_safely` |
| Every run produces a dashboard | `tests/test_cli.py::test_dashboard_command_builds_a_self_contained_readout` |
| The Studio API validates input, runs the real pipeline and serves the result | `tests/test_ui_studio.py` |
| Both surfaces can be reviewed without running anything | `python scripts/render_ui_previews.py` writes `docs/dashboard-preview.html` and `docs/studio-preview.html` |
