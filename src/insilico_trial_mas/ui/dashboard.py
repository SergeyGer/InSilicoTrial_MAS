"""Static, self-contained HTML dashboard for a finished simulation run.

Why a *static* dashboard rather than a web app: a trial readout has to survive
being attached to a ticket, opened from a Unity Catalog volume, mailed to a
medical monitor, rendered inside a Databricks notebook (``displayHTML``) and
printed to PDF for a dose-escalation meeting. A single HTML file with inline SVG
and a few kilobytes of vanilla JavaScript does all of that with no server, no CDN
and no extra Python dependency.

The screen flow follows how a trial team actually reads a study:

1. **Overview** - did anything work, and is anything unsafe?
2. **Efficacy** - per-arm effects with confidence intervals and dose-response.
3. **Safety** - adverse events by arm and CTCAE grade, plus the DSMB monitor.
4. **Patients** - the digital twins behind the aggregates (searchable).
5. **Reproducibility** - seed, digests, replay command, data quality.
6. **Lineage & traces** - which agent produced what, and what the personas cost.

:func:`write_dashboard` is the public entry point; the pipeline calls it after
every run when ``export_dashboard`` is enabled (the default).
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..version import __version__
from .charts import (
    Chart,
    bar_with_ci,
    esc,
    forest_plot,
    heatmap,
    histogram,
    line_with_band,
    patient_timeline,
    stacked_bars,
    threshold_monitor,
)
from .data import DashboardData, load_dashboard_data
from .theme import stylesheet, theme_script

logger = get_logger("ui.dashboard")

DASHBOARD_FILE = "dashboard.html"
#: Pre-rendered patient timelines (the rest stay available as tables).
MAX_RENDERED_TIMELINES = 40

TAB_LABELS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("efficacy", "Efficacy"),
    ("safety", "Safety"),
    ("patients", "Patients"),
    ("reproducibility", "Reproducibility"),
    ("lineage", "Lineage & traces"),
)


# ---------------------------------------------------------------------------
# Small HTML helpers
# ---------------------------------------------------------------------------


def _card(title: str, body: str, *, subtitle: str = "", foot: str = "", flush: bool = False) -> str:
    head = ""
    if title or subtitle:
        head = (
            '<div class="card__head"><div>'
            f"<h2>{esc(title)}</h2>"
            + (f'<div class="muted" style="font-size:12.5px">{esc(subtitle)}</div>' if subtitle else "")
            + "</div></div>"
        )
    inner = body if flush else f'<div class="card__body">{body}</div>'
    tail = f'<div class="card__foot">{foot}</div>' if foot else ""
    classes = "card card--flush" if flush else "card"
    return f'<section class="{classes}">{head}{inner}{tail}</section>'


def _kpi(label: str, value: str, *, hint: str = "", tone: str = "") -> str:
    tone_class = f" kpi--{tone}" if tone else ""
    hint_html = f'<div class="kpi__hint">{esc(hint)}</div>' if hint else ""
    return (
        f'<div class="card kpi{tone_class}">'
        f'<div class="kpi__label">{esc(label)}</div>'
        f'<div class="kpi__value">{esc(value)}</div>'
        f"{hint_html}</div>"
    )


def _chip(text: str, tone: str = "") -> str:
    tone_class = f" chip--{tone}" if tone else ""
    return f'<span class="chip{tone_class}">{esc(text)}</span>'


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    numeric_from: int = 1,
    sortable: bool = True,
    raw_cells: bool = False,
) -> str:
    sort_attr = ' data-sortable="true"' if sortable else ""
    head_cells = "".join(
        f'<th{sort_attr} class="{"num" if index >= numeric_from else ""}">{esc(name)}</th>'
        for index, name in enumerate(headers)
    )
    body_rows: list[str] = []
    for row in rows:
        cells: list[str] = []
        for index, value in enumerate(row):
            numeric = index >= numeric_from
            text = "" if value is None else str(value)
            if raw_cells and text.startswith("<"):
                cells.append(f'<td class="{"num" if numeric else ""}">{text}</td>')
                continue
            plain = text.replace("<", "&lt;").replace(">", "&gt;")
            cells.append(
                f'<td class="{"num" if numeric else ""}" data-value="{esc(plain)}">{plain}</td>'
            )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    empty_row = f'<tr><td colspan="{len(headers)}" class="muted">no data</td></tr>'
    body = "".join(body_rows) or empty_row
    return (
        '<div class="table-scroll"><table>'
        f"<thead><tr>{head_cells}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )


def _def_list(items: Sequence[tuple[str, Any]]) -> str:
    parts: list[str] = ['<dl class="def-list">']
    for label, value in items:
        rendered = value if isinstance(value, str) and value.startswith("<") else esc(value if value is not None else "n/a")
        parts.append(f"<dt>{esc(label)}</dt><dd>{rendered}</dd>")
    parts.append("</dl>")
    return "".join(parts)


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "n/a"
    if abs(number) >= 1000:
        return f"{number:,.0f}{suffix}"
    return f"{number:.{digits}f}{suffix}"


def _fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "n/a"
    return f"{number * 100:.{digits}f}%"


def _fmt_p(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "n/a"
    if number < 0.0001:
        return "<0.0001"
    return f"{number:.4f}"


def _chart_block(chart: Chart, *, caption: str = "") -> str:
    caption_html = f'<div class="card__foot">{esc(caption)}</div>' if caption else ""
    return f"{chart.svg}{caption_html}"


def _json_script(payload: Any, element_id: str) -> str:
    """Embed JSON so it can never terminate the surrounding script element."""
    serialised = json.dumps(payload, default=str, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/json" id="{esc(element_id)}">{serialised}</script>'


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------


def _header(data: DashboardData) -> str:
    protocol = data.protocol or {}
    provenance = data.provenance or {}
    arms = protocol.get("arms") or []
    return f"""
<header class="site-header">
  <div class="site-header__inner">
    <div class="brand">
      <span class="brand__mark">IS</span>
      <span>InSilicoTrial MAS
        <span class="brand__sub">/ {esc(protocol.get("protocol_id", "simulation"))} readout</span>
      </span>
    </div>
    <span class="header-spacer"></span>
    <div class="header-actions">
      {_chip(f"run {data.run_id}", "accent")}
      {_chip(str(provenance.get("engine_backend", "engine")) or "engine")}
      {_chip(f"{len(arms)} arms")}
      <button class="btn btn--ghost" type="button" data-theme-toggle title="Toggle light/dark theme">theme</button>
      <button class="btn" type="button" onclick="window.print()">Print / PDF</button>
    </div>
  </div>
</header>
"""


def _page_head(data: DashboardData) -> str:
    protocol = data.protocol or {}
    provenance = data.provenance or {}
    title = protocol.get("title") or f"Simulated trial {protocol.get('protocol_id', '')}"
    indication = protocol.get("indication") or "n/a"
    return f"""
<div class="page-head">
  <div class="eyebrow">Simulated clinical trial readout</div>
  <h1>{esc(title)}</h1>
  <p class="lede">
    {esc(protocol.get("phase", ""))} &middot; {esc(protocol.get("design", ""))} &middot; {esc(indication)}
    &middot; {esc(str(protocol.get("therapeutic_area", "")))}
    &middot; product <strong>{esc((protocol.get("drug") or {}).get("name", "n/a"))}</strong>
  </p>
  <div class="row">
    {_chip(f"patients {provenance.get('n_patients', 'n/a')}")}
    {_chip(f"epochs {provenance.get('n_epochs', 'n/a')}")}
    {_chip(f"model {provenance.get('physiology_model_version', 'n/a')}")}
    {_chip(f"LLM {provenance.get('llm_mode', 'off')}")}
    {_chip(f"seed {provenance.get('seed', 'n/a')}")}
    {_chip(f"built {provenance.get('finished_at', '')}")}
  </div>
  <p class="note disclaimer">
    <strong>Synthetic data.</strong> Every patient in this readout is generated by the platform.
    It is a trial-design decision-support artefact - not clinical evidence, not a regulatory
    submission and not medical advice. Replace the packaged population priors and drug parameters
    with licensed data before any regulated use.
  </p>
</div>
"""


def _tab_bar(active: str) -> str:
    buttons = "".join(
        f'<button class="tab" type="button" data-tab="{esc(key)}" '
        f'aria-selected="{"true" if key == active else "false"}">{esc(label)}</button>'
        for key, label in TAB_LABELS
    )
    return f'<nav class="tabs" role="tablist">{buttons}</nav>'


def _overview_panel(data: DashboardData) -> str:
    overview = (data.analysis or {}).get("overview") or {}
    provenance = data.provenance or {}
    summaries = data.arm_summaries or []
    treatment = [row for row in summaries if not (row.get("extra") or {}).get("is_control")]
    control = next((row for row in summaries if (row.get("extra") or {}).get("is_control")), None)
    best = max(treatment, key=lambda row: -(row.get("mean_sbp_change") or 0), default=None)
    worst_grade3 = max((float(row.get("grade3_plus_rate") or 0.0) for row in summaries), default=0.0)
    sae = max((float(row.get("sae_rate") or 0.0) for row in summaries), default=0.0)

    kpis = [
        _kpi("Patients", str(provenance.get("n_patients", data.patient_count or "n/a")), hint=f"{len(summaries)} arms"),
        _kpi("Silver rows", f"{data.row_count:,}" if data.row_count else "n/a", hint="patient x epoch observations"),
        _kpi(
            "Primary effect",
            f"{_fmt(best.get('mean_sbp_change') if best else None)} mmHg",
            hint=f"best treatment arm: {best.get('arm_id')}" if best else "",
            tone=(
                "good"
                if best is not None
                and control is not None
                and float(best.get("mean_sbp_change") or 0.0) < float(control.get("mean_sbp_change") or 0.0)
                else ""
            ),
        ),
        _kpi(
            "Responder rate",
            _fmt_pct(best.get("responder_rate") if best else None),
            hint="primary responder definition",
        ),
        _kpi(
            "Adverse events",
            f"{int(overview.get('n_adverse_events') or 0):,}" if overview.get("n_adverse_events") is not None else "n/a",
            hint="modelled + persona-reported",
        ),
        _kpi("Grade >= 3", _fmt_pct(worst_grade3), hint="worst arm", tone="warn" if worst_grade3 > 0.1 else ""),
        _kpi("Serious AEs", _fmt_pct(sae), hint="worst arm", tone="critical" if sae > 0.05 else ""),
        _kpi("Safety alerts", str(len(data.safety_alerts)), hint="DSMB review signals", tone="critical" if data.safety_alerts else "good"),
        _kpi("Duration", f"{_fmt(provenance.get('duration_seconds'), 1)} s", hint=f"engine: {provenance.get('engine_backend', 'n/a')}"),
        _kpi("Model", str(provenance.get("physiology_backend", "n/a")), hint=str(provenance.get("physiology_model_version", ""))[:28]),
    ]

    def _arm_ci(row: dict[str, Any], sign: float) -> float:
        mean = float(row.get("mean_sbp_change") or 0.0)
        sd = float(row.get("sd_sbp_change") or 0.0)
        n = max(1, int(row.get("n_patients") or 1))
        return mean + sign * 1.96 * sd / n**0.5

    dose_chart = bar_with_ci(
        labels=[str(row.get("label") or row.get("arm_id")) for row in summaries],
        values=[float(row.get("mean_sbp_change") or 0.0) for row in summaries],
        lower=[_arm_ci(row, -1.0) for row in summaries],
        upper=[_arm_ci(row, 1.0) for row in summaries],
        unit=" mmHg",
        title="Primary endpoint by arm",
        description="Change from baseline in systolic blood pressure at end of treatment, mean with 95% confidence interval.",
        height=250,
    )
    responder_chart = bar_with_ci(
        labels=[str(row.get("label") or row.get("arm_id")) for row in summaries],
        values=[float(row.get("responder_rate") or 0.0) * 100 for row in summaries],
        unit="%",
        title="Responder rate by arm",
        description="Share of patients meeting the protocol responder definition.",
        height=250,
        value_digits=1,
    )

    comparison_rows = [
        {
            "label": str(row.get("endpoint", "")),
            "group": str(row.get("arm_id", "")),
            "group_index": index,
            "estimate": row.get("effect_estimate"),
            "low": row.get("ci_low"),
            "high": row.get("ci_high"),
            "p_value": row.get("p_value"),
        }
        for index, row in enumerate(data.comparisons or [])
    ]
    forest = forest_plot(comparison_rows, description="Effect versus control with 95% confidence interval; the dashed line is no effect.")

    flow = data.cohort_flow or {}
    consort_steps = [
        ("Screened", flow.get("screened", "n/a"), ""),
        ("Screen failures", flow.get("screen_failures", "n/a"), ", ".join(f"{k}: {v}" for k, v in list((flow.get("failure_reasons") or {}).items())[:3])),
        ("Randomised and simulated", flow.get("enrolled", "n/a"), "stratified block randomisation"),
        ("Completed all epochs", flow.get("completed", "n/a"), ""),
        ("Discontinued treatment", flow.get("discontinued", "n/a"), "toxicity or consent withdrawal"),
    ]
    consort = "".join(
        f'<div class="consort__step"><span class="consort__label">{esc(label)}'
        + (f'<br/><span class="faint" style="font-size:12px">{esc(hint)}</span>' if hint else "")
        + f'</span><span class="consort__value">{esc(value)}</span></div>'
        for label, value, hint in consort_steps
    )

    alerts_html = (
        '<ul class="alert-list">'
        + "".join(
            f'<li>{_chip(str(alert.get("severity", "info")).upper(), "critical" if alert.get("severity") == "high" else "warn")}'
            f'<span>{esc(alert.get("message", ""))}</span></li>'
            for alert in data.safety_alerts
        )
        + "</ul>"
        if data.safety_alerts
        else '<p class="note note--good">No safety signal crossed the protocol alerting thresholds in this simulation. '
        "Absence of a signal in a synthetic cohort is not evidence of safety.</p>"
    )

    return f"""
<section class="panel" data-panel="overview" data-active="true">
  <div class="grid grid--kpi">{''.join(kpis)}</div>

  <div class="grid grid--2" style="margin-top:16px">
    {_card("Primary endpoint by arm", _chart_block(dose_chart), subtitle="mean change from baseline with 95% CI", flush=False)}
    {_card("Responder rate", _chart_block(responder_chart), subtitle="share of patients meeting the responder definition", flush=False)}
  </div>

  <div class="grid grid--2" style="margin-top:16px">
    {_card("Treatment comparisons", _chart_block(forest), subtitle="every endpoint declared in the protocol", flush=False)}
    <div class="grid" style="gap:16px">
      {_card("Participant disposition", f'<div class="consort">{consort}</div>')}
      {_card("Safety signals for DSMB review", alerts_html)}
    </div>
  </div>
</section>
"""


def _efficacy_panel(data: DashboardData) -> str:
    summaries = data.arm_summaries or []
    summary_table = _table(
        ["Arm", "Dose (mg)", "n", "dSBP", "SD", "dDBP", "dHR", "Responders", "ALT ratio", "Discontinued"],
        [
            [
                row.get("label") or row.get("arm_id"),
                _fmt(row.get("dose_mg"), 0),
                row.get("n_patients"),
                _fmt(row.get("mean_sbp_change")),
                _fmt(row.get("sd_sbp_change")),
                _fmt(row.get("mean_dbp_change")),
                _fmt(row.get("mean_hr_change")),
                _fmt_pct(row.get("responder_rate")),
                _fmt(row.get("mean_alt_ratio")),
                row.get("discontinuations", 0),
            ]
            for row in summaries
        ],
    )

    comparison_table = _table(
        ["Endpoint", "Arm", "Effect", "95% CI low", "95% CI high", "p-value", "q-value", "Test", "MCID met"],
        [
            [
                row.get("endpoint"),
                row.get("arm_id"),
                _fmt(row.get("effect_estimate"), 3),
                _fmt(row.get("ci_low"), 3),
                _fmt(row.get("ci_high"), 3),
                _fmt_p(row.get("p_value")),
                _fmt_p(row.get("q_value")),
                row.get("test"),
                "yes" if row.get("meets_mcid") else "no",
            ]
            for row in (data.comparisons or [])
        ],
    )

    trajectory_blocks: list[str] = []
    for trajectory in data.trajectories or []:
        if trajectory["metric"] in {"plasma_conc_mg_l", "alt_u_l"}:
            continue
        epochs = sorted({epoch for series in trajectory["series"] for epoch in series["epochs"]})
        chart = line_with_band(
            epochs,
            [
                {
                    "name": series["name"],
                    "values": series["values"],
                    "low": series["low"],
                    "high": series["high"],
                }
                for series in trajectory["series"]
                if series["epochs"] == epochs
            ],
            title=f"{trajectory['label']} over time",
            description=f"Mean {trajectory['label'].lower()} per arm and epoch with a 95% confidence band.",
            x_label="epoch (0 = baseline)",
            y_label=trajectory["unit"],
            height=250,
            width=680,
        )
        trajectory_blocks.append(_card(f"{trajectory['label']} trajectory", _chart_block(chart), subtitle=f"mean per arm with 95% CI ({trajectory['unit']})"))

    concentration = next((item for item in (data.trajectories or []) if item["metric"] == "plasma_conc_mg_l"), None)
    concentration_card = ""
    if concentration:
        epochs = sorted({epoch for series in concentration["series"] for epoch in series["epochs"]})
        chart = line_with_band(
            epochs,
            [{"name": series["name"], "values": series["values"]} for series in concentration["series"] if series["epochs"] == epochs],
            title="Exposure by arm",
            description="Mean average steady-state plasma concentration per arm and epoch.",
            x_label="epoch",
            y_label="mg/L",
            height=250,
            width=680,
            band=False,
        )
        concentration_card = _card("Exposure (PK)", _chart_block(chart), subtitle="mean average steady-state concentration per arm")

    dose_response = data.dose_response or []
    dose_chart = bar_with_ci(
        labels=[str(row.get("label") or row.get("arm_id")) for row in dose_response],
        values=[float(row["mean"]) if row.get("mean") is not None else float("nan") for row in dose_response],
        lower=[float(row["ci_low"]) if row.get("ci_low") is not None else float("nan") for row in dose_response],
        upper=[float(row["ci_high"]) if row.get("ci_high") is not None else float("nan") for row in dose_response],
        title="Dose-response",
        description="Mean primary endpoint value by nominal dose with 95% confidence interval.",
        height=250,
        unit="",
    )
    trend_note = ""
    if dose_response and dose_response[0].get("trend_slope_per_mg") is not None:
        trend_note = (
            f"Linear trend across arms: {_fmt(dose_response[0].get('trend_slope_per_mg'), 4)} per mg "
            f"(R² = {_fmt(dose_response[0].get('trend_r_squared'), 3)})."
        )

    demographics = data.demographics or []
    demographics_table = _table(
        ["Arm", "n", "Age mean", "Age SD", "Female %", "End SBP", "Mean dSBP", "Discontinued"],
        [
            [
                row.get("arm_id"),
                row.get("n"),
                _fmt(row.get("age_mean")),
                _fmt(row.get("age_sd")),
                _fmt(row.get("female_pct")),
                _fmt(row.get("sbp_end_mean")),
                _fmt(row.get("sbp_change_mean")),
                row.get("discontinued"),
            ]
            for row in demographics
        ],
    )

    return f"""
<section class="panel" data-panel="efficacy">
  <div class="grid grid--2">
    {_card("Per-arm summary (end of treatment)", summary_table, subtitle="descriptive statistics per randomisation arm")}
    {_card("Dose-response", _chart_block(dose_chart), subtitle=trend_note or "mean primary endpoint by nominal dose")}
  </div>
  <div style="margin-top:16px">{_card("Treatment comparisons versus control", comparison_table, subtitle="Welch t-test or two-proportion/Fisher exact with bootstrap or Newcombe intervals; q-values control the secondary family")}</div>
  <div class="grid grid--2" style="margin-top:16px">{''.join(trajectory_blocks)}{concentration_card}</div>
  <div style="margin-top:16px">{_card("Demographics and baseline", demographics_table)}</div>
</section>
"""


def _safety_panel(data: DashboardData) -> str:
    heat = data.ae_heatmap or {}
    heat_chart = (
        heatmap(
            heat.get("terms", []),
            heat.get("arms", []),
            heat.get("values", []),
            title="Adverse-event rate by arm",
            description="Patient-level incidence per arm and preferred term, as a percentage of the arm.",
            value_suffix="%",
        )
        if heat
        else Chart("<p class='muted'>no adverse events recorded</p>", "empty")
    )

    stacks = data.grade_stacks or {}
    grade_chart = (
        stacked_bars(
            stacks.get("labels", []),
            stacks.get("stacks", []),
            stack_labels=stacks.get("grade_labels", ["G1", "G2", "G3", "G4", "G5"]),
            title="CTCAE grade distribution",
            description="Adverse-event counts by CTCAE grade and arm.",
        )
        if stacks
        else Chart("<p class='muted'>no adverse events recorded</p>", "empty")
    )

    safety_table = _table(
        ["Arm", "Term", "SOC", "Patients", "Rate", "Control", "Risk diff.", "G3+", "SAE", "G5", "p", "q"],
        [
            [
                row.get("arm_id"),
                row.get("term"),
                row.get("soc"),
                f"{row.get('n_patients_with_event', 0)}/{row.get('n_patients', 0)}",
                _fmt_pct(row.get("rate")),
                _fmt_pct(row.get("control_rate")),
                _fmt_pct(row.get("risk_difference")),
                row.get("grade3_plus", 0),
                row.get("serious", 0),
                row.get("grade5", 0),
                _fmt_p(row.get("p_value")),
                _fmt_p(row.get("q_value")),
            ]
            for row in (data.safety_summary or [])
        ],
    )

    rule_specs = {str(rule.get("rule_id")): rule for rule in (data.stopping_rules or [])}
    monitors: list[str] = []
    for rule_id, spec in list(rule_specs.items())[:4]:
        for arm_id in sorted({str(row.get("arm_id")) for row in (data.stopping or []) if str(row.get("rule_id")) == rule_id}):
            evaluation = [
                row for row in (data.stopping or [])
                if str(row.get("rule_id")) == rule_id and str(row.get("arm_id")) == arm_id
            ]
            evaluation.sort(key=lambda row: row.get("epoch") or 0)
            chart = threshold_monitor(
                [int(row.get("epoch") or 0) for row in evaluation],
                [float(row.get("observed") or 0.0) for row in evaluation],
                float(spec.get("threshold") or 0.0),
                title=f"{rule_id} - {arm_id}",
                description=f"{spec.get('description', '')} Threshold {_fmt_pct(spec.get('threshold'))}, action {spec.get('action')}.",
            )
            monitors.append(_card(f"{rule_id} - {arm_id}", _chart_block(chart), subtitle=f"{spec.get('metric')} vs threshold {_fmt_pct(spec.get('threshold'))}"))

    triggered = [row for row in (data.stopping or []) if row.get("triggered")]
    triggered_table = _table(
        ["Rule", "Arm", "Epoch", "Metric", "Observed", "Threshold", "Action"],
        [
            [
                row.get("rule_id"),
                row.get("arm_id"),
                row.get("epoch"),
                row.get("metric"),
                _fmt_pct(row.get("observed")),
                _fmt_pct(row.get("threshold")),
                row.get("action"),
            ]
            for row in triggered
        ],
    )

    return f"""
<section class="panel" data-panel="safety">
  <div class="grid grid--2">
    {_card("Adverse events by arm and term", _chart_block(heat_chart), subtitle="patient-level incidence (% of arm)")}
    {_card("CTCAE grade distribution", _chart_block(grade_chart), subtitle="event counts per grade")}
  </div>
  <div style="margin-top:16px">{_card("Safety table", safety_table, subtitle="risk difference versus control with Fisher exact p-values and Benjamini-Hochberg q-values")}</div>
  <div style="margin-top:16px">
    {_card("Data Safety Monitoring Board stopping rules", triggered_table, subtitle=f"{len(triggered)} of {len(data.stopping or [])} rule/arm/epoch evaluations triggered")}
  </div>
  <div class="grid grid--2" style="margin-top:16px">{''.join(monitors)}</div>
</section>
"""


def _patients_panel(data: DashboardData) -> str:
    patients = data.patients or []
    if not patients:
        return """
<section class="panel" data-panel="patients">
  <p class="note">The patient explorer needs <span class="mono">silver_observations.parquet</span>, which is missing for this run.</p>
</section>
"""

    rows = "".join(
        f'<div class="patient-row" role="button" tabindex="0" data-patient="{esc(patient["patient_id"])}">'
        f'<span class="mono">{esc(patient["patient_id"])}</span>'
        f'<span class="faint">{esc(patient["arm_id"])}</span>'
        f'<span class="faint">G{patient["worst_grade"]}</span>'
        f'<span class="faint">{"responder" if patient["responder"] else ""}</span>'
        "</div>"
        for patient in patients
    )

    summary = (
        f"Showing {len(patients)} simulated patients "
        f"({min(MAX_RENDERED_TIMELINES, len(patients))} with a rendered trajectory). "
        "Sorted by worst CTCAE grade, then by identifier."
    )
    return f"""
<section class="panel" data-panel="patients">
  <div class="grid" style="grid-template-columns:minmax(280px,360px) 1fr; align-items:start">
    {_card(
        "Cohort",
        f'<input class="search-input" type="search" data-patient-search placeholder="Search patient id, arm, G3+..." aria-label="Search patients"/>'
        f'<div class="patient-list" data-patient-list style="margin-top:10px">{rows}</div>',
        subtitle=summary,
    )}
    <div data-patient-detail>
      {_card("Patient detail", '<p class="muted">Select a patient to inspect its individual trajectory, genotype-driven exposure and persona narration.</p>')}
    </div>
  </div>
  <div style="margin-top:16px">
    {_card(
        "How to read this view",
        '<p class="muted" style="margin:0">Each row is one digital twin agent. The trajectory shows the administered dose '
        "(bars), the simulated plasma concentration (solid line), systolic blood pressure (dashed line) and every adverse "
        "event the agent experienced (markers above the plot). Persona narration, when the LLM trigger fired, is quoted "
        "verbatim - it is the qualitative half of the hybrid model.</p>",
    )}
  </div>
</section>
"""


def _reproducibility_panel(data: DashboardData) -> str:
    provenance = data.provenance or {}
    environment = data.environment or {}
    quality = data.data_quality or {}
    estimate = data.estimate or {}
    replay = data.replay or {}

    provenance_list = _def_list(
        [
            ("Run id", f'<span class="mono">{esc(data.run_id)}</span>'),
            ("Protocol digest", f'<span class="mono">{esc(provenance.get("protocol_digest", "n/a"))}</span>'),
            ("Package version", esc(provenance.get("package_version", "n/a"))),
            ("Git revision", f'<span class="mono">{esc(provenance.get("git_revision", "n/a"))}</span>'),
            ("Master seed", esc(provenance.get("seed", "n/a"))),
            ("Engine", esc(provenance.get("engine_backend", "n/a"))),
            ("Spark", esc(provenance.get("spark_version", "") or "not used")),
            ("Storage", esc(provenance.get("storage_backend", "n/a"))),
            ("Silver location", f'<span class="mono">{esc(provenance.get("silver_location", "n/a"))}</span>'),
            ("Physiology model", f'{esc(provenance.get("physiology_model_version", "n/a"))} ({esc(provenance.get("physiology_backend", ""))})'),
            ("Model digest", f'<span class="mono">{esc(provenance.get("physiology_model_digest", "n/a") or "n/a")}</span>'),
            ("LLM policy", f'{esc(provenance.get("llm_provider", ""))} / {esc(provenance.get("llm_model", "") or "offline")} / mode {esc(provenance.get("llm_mode", "off"))}'),
            ("LLM calls", f'{esc(provenance.get("total_llm_calls", 0))} (cache hits {esc(provenance.get("llm_cache_hits", 0))})'),
            ("Duration", f'{_fmt(provenance.get("duration_seconds"), 1)} s'),
            ("Started / finished", f'{esc(provenance.get("started_at", ""))} -> {esc(provenance.get("finished_at", ""))}'),
        ]
    )

    environment_list = _def_list(
        [
            ("Python", esc(environment.get("python_version", "n/a"))),
            ("Platform", esc(environment.get("platform", "n/a"))),
            ("CPU cores", esc(environment.get("cpu_count", "n/a"))),
            ("Memory (GB)", _fmt(environment.get("memory_gb"), 1)),
            ("PySpark", "yes" if environment.get("pyspark_available") else "no"),
            ("JVM", "yes" if environment.get("java_available") else "no"),
            ("Databricks", "yes" if environment.get("databricks") else "no"),
            ("Community edition", "yes" if environment.get("databricks_community") else "no"),
            ("Recommended engine", esc(environment.get("recommended_backend", "n/a"))),
        ]
    )

    quality_list = _def_list(
        [
            ("Silver rows", esc(quality.get("rows", data.row_count))),
            ("Distinct patients", esc(quality.get("patients", data.patient_count))),
            ("Narrated rows", f'{esc(quality.get("llm_narrated_rows", 0))} ({_fmt_pct((quality.get("llm_narrated_pct") or 0) / 100)})'),
            ("Narration failures", esc(quality.get("llm_error_rows", 0))),
            ("Model versions", esc(", ".join(quality.get("model_versions", []) or []))),
            ("Backends", esc(", ".join(quality.get("physiology_backends", []) or []))),
            ("Columns with nulls", esc(len(quality.get("columns_with_nulls", {}) or {}))),
        ]
    )

    estimate_list = _def_list(
        [
            ("Planned rows", esc(estimate.get("rows", "n/a"))),
            ("Planned LLM calls", esc(estimate.get("llm_calls_estimate", "n/a"))),
            ("Spark partitions", esc(estimate.get("spark_partitions", "n/a"))),
            ("Epochs", esc(estimate.get("epochs", "n/a"))),
        ]
    )

    replay_command = str(replay.get("command") or f"insilico-trial simulate --config <config.yaml> --run-id {data.run_id}")

    return f"""
<section class="panel" data-panel="reproducibility">
  <div class="grid grid--2">
    {_card("Run provenance", provenance_list, subtitle="everything required to replay this readout")}
    {_card("Execution environment", environment_list, subtitle="detected at run time")}
  </div>
  <div class="grid grid--2" style="margin-top:16px">
    {_card("Data quality", quality_list, subtitle="completeness and model coverage of the Silver layer")}
    {_card("Cost estimate (pre-run)", estimate_list, subtitle="computed before the simulation started")}
  </div>
  <div style="margin-top:16px">
    {_card(
        "Replay recipe",
        f'<pre class="code">{esc(replay_command)}</pre>'
        '<p class="muted" style="margin:10px 0 0">Random draws are derived from '
        '<span class="mono">sha256(seed, run, patient, epoch, purpose)</span>, so re-running with the same seed and '
        'protocol digest reproduces every simulated value. Wall-clock metadata '
        '(<span class="mono">observation_ts</span>, <span class="mono">llm_latency_ms</span>) is excluded by design.</p>',
        subtitle="deterministic by construction",
    )}
  </div>
</section>
"""


def _lineage_panel(data: DashboardData) -> str:
    graph = data.lineage_graph or {}
    edges = graph.get("edges") or []
    node_index = {node.get("node_id"): node for node in (graph.get("nodes") or [])}

    def node_html(node_id: str) -> str:
        node = node_index.get(node_id) or {}
        kind = str(node.get("kind", "table"))
        label = str(node.get("label") or node_id)
        return f'<span class="flow__node flow__node--{esc(kind)}">{esc(label)}</span>'

    flow_parts: list[str] = []
    for edge in edges[:24]:
        flow_parts.append(node_html(str(edge.get("source"))))
        flow_parts.append(f'<span class="flow__arrow">&rarr;<span class="faint" style="font-size:11px"> {esc(edge.get("relation", ""))}</span></span>')
        flow_parts.append(node_html(str(edge.get("target"))))
        flow_parts.append('<span class="flow__arrow" style="flex-basis:100%"></span>')

    lineage_summary = data.lineage or {}
    traces = data.traces or {}
    token_values = traces.get("tokens_in_values") or []
    latency_values = traces.get("latency_values") or []
    token_chart = histogram(
        token_values,
        title="Prompt tokens per persona call",
        description="Distribution of prompt tokens across narrated epochs.",
        unit=" tokens",
    )
    latency_chart = histogram(
        latency_values,
        title="LLM latency",
        description="Distribution of provider latency per persona call in milliseconds.",
        unit=" ms",
        accent_index=1,
    )

    trace_kpis = [
        _kpi("LLM calls", str(traces.get("llm_calls", 0)), hint=f"rows narrated: {traces.get('narrated_rows', 0)}"),
        _kpi("Cache hit rate", _fmt_pct(traces.get("cache_hit_rate")), hint="reused responses"),
        _kpi("Prompt tokens", f"{int(traces.get('tokens_in_total') or 0):,}", hint="total input tokens"),
        _kpi("Completion tokens", f"{int(traces.get('tokens_out_total') or 0):,}", hint="total output tokens"),
        _kpi("Narration errors", _fmt_pct(traces.get("error_rate")), hint="fallbacks included", tone="warn" if (traces.get("error_rate") or 0) > 0.01 else ""),
    ]

    trace_note = (
        '<p class="note">Per-call spans are written to <span class="mono">tracking.trace_path</span> as JSONL and, when '
        "tracking is enabled, copied to <span class=\"mono\">bronze.llm_raw_traces</span>. The distributions above are "
        "derived from the Silver columns, so they are available even when tracing is off.</p>"
    )

    mermaid = data.lineage_mermaid or ""
    flow_body = "".join(flow_parts) or '<span class="muted">no lineage recorded</span>'
    lineage_subtitle = (
        f"{lineage_summary.get('nodes', 0)} nodes, {lineage_summary.get('edges', 0)} edges, "
        f"agents: {', '.join(lineage_summary.get('agents', []) or [])}"
    )

    return f"""
<section class="panel" data-panel="lineage">
  {_card("Agent and table lineage", f'<div class="flow">{flow_body}</div>',
         subtitle=lineage_subtitle)}
  <div class="grid grid--kpi" style="margin-top:16px">{''.join(trace_kpis)}</div>
  <div class="grid grid--2" style="margin-top:16px">
    {_card("Prompt token distribution", _chart_block(token_chart), subtitle="cost driver per persona call")}
    {_card("Provider latency", _chart_block(latency_chart), subtitle="network and inference time")}
  </div>
  <div style="margin-top:16px">{trace_note}</div>
  <div style="margin-top:16px">
    {_card(
        "Mermaid source",
        f'<pre class="code">{esc(mermaid) if mermaid else "not recorded"}</pre>',
        subtitle="paste into a Mermaid renderer (GitHub, Databricks markdown, mermaid.live)",
    )}
  </div>
</section>
"""


def _patient_detail_payload(data: DashboardData) -> dict[str, Any]:
    """Everything the explorer needs, including pre-rendered SVG timelines."""
    details = data.patient_details or {}
    timelines: dict[str, str] = {}
    for patient in (data.patients or [])[:MAX_RENDERED_TIMELINES]:
        detail = details.get(patient["patient_id"])
        if not detail:
            continue
        timelines[patient["patient_id"]] = patient_timeline(
            detail["epochs"],
            dose_mg=detail["dose_mg"],
            concentration=detail["concentration"],
            sbp=detail["sbp"],
            adverse_events={int(epoch): terms for epoch, terms in (detail.get("adverse_by_epoch") or {}).items()},
            title=f"Patient {patient['patient_id']}",
            description="Dose, exposure, systolic blood pressure and adverse events per simulated epoch.",
        ).svg
    return {"patients": data.patients or [], "details": details, "timelines": timelines}


def _patient_explorer_script(element_id: str) -> str:
    return f"""
(function () {{
  "use strict";
  var payload = JSON.parse(document.getElementById("{element_id}").textContent);
  var details = payload.details || {{}};
  var timelines = payload.timelines || {{}};
  var list = document.querySelector("[data-patient-list]");
  var detailHost = document.querySelector("[data-patient-detail]");
  var search = document.querySelector("[data-patient-search]");

  function escapeHtml(value) {{
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }}

  function renderPatient(patientId) {{
    var detail = details[patientId];
    if (!detail) {{ return; }}
    var rows = detail.epochs.map(function (epoch, index) {{
      var events = (detail.adverse_by_epoch || {{}})[String(epoch)] || [];
      return "<tr><td>" + epoch + "</td><td class='num'>" + (detail.dose_mg[index] || 0).toFixed(1) +
        "</td><td class='num'>" + (detail.concentration[index] || 0).toFixed(2) +
        "</td><td class='num'>" + (detail.sbp[index] || 0).toFixed(1) +
        "</td><td class='num'>" + (detail.dbp[index] || 0).toFixed(1) +
        "</td><td class='num'>" + (detail.hr[index] || 0).toFixed(1) +
        "</td><td class='num'>" + (detail.alt_u_l[index] || 0).toFixed(1) +
        "</td><td>" + escapeHtml(events.join(", ")) + "</td></tr>";
    }}).join("");

    var narration = (detail.symptoms || []).map(function (item) {{
      return "<li><strong>epoch " + item.epoch + "</strong> &middot; " + escapeHtml(item.summary) +
        (item.llm ? " <span class='chip chip--accent'>persona</span>" : "") + "</li>";
    }}).join("");

    var timeline = timelines[patientId] ||
      "<p class='muted'>Trajectory chart is pre-rendered for the first {MAX_RENDERED_TIMELINES} patients in the cohort " +
      "(the ones with the most adverse events). The epoch table below holds the same data.</p>";

    detailHost.innerHTML =
      '<section class="card"><div class="card__head"><div><h2>' + escapeHtml(patientId) + '</h2>' +
      '<div class="muted" style="font-size:12.5px">' + escapeHtml(detail.arm_label) + ' &middot; ' +
      escapeHtml(detail.cohort_id) + ' &middot; ' + escapeHtml(detail.site_id) +
      (detail.discontinued ? ' &middot; <span class="chip chip--warn">discontinued: ' + escapeHtml(detail.discontinuation_reason) + '</span>' : '') +
      '</div></div></div><div class="card__body">' + timeline +
      '<div class="table-scroll" style="margin-top:12px"><table><thead><tr><th>Epoch</th><th class="num">Dose (mg)</th>' +
      '<th class="num">Conc (mg/L)</th><th class="num">SBP</th><th class="num">DBP</th><th class="num">HR</th>' +
      '<th class="num">ALT</th><th>Adverse events</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      (narration ? '<h3 style="margin-top:14px">Persona narration</h3><ul class="alert-list">' + narration + '</ul>' : '') +
      '</div></section>';
  }}

  if (list) {{
    list.addEventListener("click", function (event) {{
      var row = event.target.closest("[data-patient]");
      if (!row) {{ return; }}
      list.querySelectorAll("[data-patient]").forEach(function (node) {{ node.setAttribute("aria-selected", String(node === row)); }});
      renderPatient(row.getAttribute("data-patient"));
    }});
  }}

  if (search) {{
    search.addEventListener("input", function () {{
      var needle = search.value.trim().toLowerCase();
      list.querySelectorAll("[data-patient]").forEach(function (row) {{
        row.style.display = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1 ? "" : "none";
      }});
    }});
  }}
}})();
"""


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_dashboard(data: DashboardData) -> str:
    """Render the complete dashboard document."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    detail_payload = _patient_detail_payload(data)
    notes = "".join(f'<p class="note note--warn">{esc(note)}</p>' for note in data.notes)
    warnings = "".join(f'<p class="note note--warn">Protocol review: {esc(item)}</p>' for item in data.warnings or [])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="generator" content="InSilicoTrial MAS {esc(__version__)}"/>
<title>{esc(data.protocol.get("protocol_id", "Simulation"))} - InSilicoTrial MAS readout</title>
<style>{stylesheet()}</style>
</head>
<body>
{_header(data)}
<main class="wrap">
  {_page_head(data)}
  {notes}
  {warnings}
  {_tab_bar("overview")}
  {_overview_panel(data)}
  {_efficacy_panel(data)}
  {_safety_panel(data)}
  {_patients_panel(data)}
  {_reproducibility_panel(data)}
  {_lineage_panel(data)}
  <footer class="site-footer">
    <span>Generated {esc(generated)} by InSilicoTrial MAS {esc(__version__)} &middot; run {esc(data.run_id)}</span>
    <span>Static self-contained readout &middot; no network access required</span>
  </footer>
</main>
{_json_script(detail_payload, "patient-data")}
<script>{theme_script()}</script>
<script>{_patient_explorer_script("patient-data")}</script>
</body>
</html>
"""


def write_dashboard(run_dir: str | Path, output_path: str | Path | None = None) -> Path:
    """Render ``run_dir`` into a self-contained HTML dashboard.

    Parameters
    ----------
    run_dir:
        A finished run directory (``run_manifest.json`` and/or
        ``silver_observations.parquet``).
    output_path:
        Target file. Defaults to ``<run_dir>/report/dashboard.html``.
    """
    directory = Path(run_dir)
    data = load_dashboard_data(directory)
    target = Path(output_path) if output_path else directory / "report" / DASHBOARD_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_dashboard(data), encoding="utf-8")
    logger.info(f"wrote dashboard to {target} ({target.stat().st_size / 1024:.0f} KB)")
    return target


def dashboard_summary_text(data: DashboardData) -> str:
    """Short plain-text recap used by the CLI after writing the file."""
    lines = [f"run {data.run_id}", f"patients {data.patient_count or 'n/a'}", f"rows {data.row_count or 'n/a'}"]
    for row in (data.arm_summaries or [])[:4]:
        lines.append(
            f"{row.get('arm_id')}: dSBP {_fmt(row.get('mean_sbp_change'))} mmHg, "
            f"responders {_fmt_pct(row.get('responder_rate'))}, G3+ {_fmt_pct(row.get('grade3_plus_rate'))}"
        )
    return " | ".join(lines)


def html_escape(value: Any) -> str:
    """Exposed for callers that build small HTML fragments around the dashboard."""
    return html.escape(str(value), quote=True)
