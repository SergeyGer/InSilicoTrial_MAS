"""Server-side SVG charts - no JavaScript charting library, no matplotlib.

Every function returns a standalone ``<svg>`` string that scales to its container
(``viewBox`` + ``width: 100%``), inherits the dashboard's CSS custom properties
(so light/dark mode costs nothing), and carries an accessible ``<title>`` plus a
``<desc>``. Native ``<title>`` elements inside data marks provide hover tooltips
without any scripting.

The visual language is deliberately clinical: one accent hue, neutral greys,
hairline grids, no 3-D effects, no gradients except a single subtle fill, and
direct labels instead of legends wherever the series count allows it.
"""

from __future__ import annotations

import html
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Categorical palette (colour-blind safe, ordered by typical use).
PALETTE: tuple[str, ...] = (
    "var(--c-series-1)",
    "var(--c-series-2)",
    "var(--c-series-3)",
    "var(--c-series-4)",
    "var(--c-series-5)",
    "var(--c-series-6)",
)

#: Semantic colours for safety visualisations.
GRADE_COLORS: tuple[str, ...] = (
    "var(--c-grade-1)",
    "var(--c-grade-2)",
    "var(--c-grade-3)",
    "var(--c-grade-4)",
    "var(--c-grade-5)",
)


def esc(value: object) -> str:
    """Escape text for HTML/SVG content."""
    return html.escape(str(value), quote=True)


def _fmt(value: float, digits: int = 1) -> str:
    """Compact number formatting for axis labels and data labels."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:.{max(0, digits - 1)}f}"
    return f"{value:.{digits}f}"


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Human-friendly axis ticks covering ``[low, high]``."""
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        return [low]
    span = high - low
    raw_step = span / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for multiplier in (1, 2, 2.5, 5, 10):
        step = magnitude * multiplier
        if step >= raw_step:
            break
    start = math.floor(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step * 0.5:
        ticks.append(round(value, 10))
        value += step
    return ticks


@dataclass(slots=True)
class Chart:
    """Rendered SVG plus metadata used by the layout and the tests."""

    svg: str
    title: str
    description: str = ""
    height: int = 220

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.svg


def _frame(width: int, height: int, *, title: str, description: str, body: str, class_name: str = "chart") -> str:
    return (
        f'<svg class="{class_name}" viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{esc(title)}</title><desc>{esc(description or title)}</desc>{body}</svg>"
    )


def _empty_state(width: int, height: int, message: str) -> str:
    return (
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" class="chart-empty">{esc(message)}</text>'
    )


# ---------------------------------------------------------------------------
# Bar chart with confidence intervals / error bars
# ---------------------------------------------------------------------------


def bar_with_ci(
    labels: Sequence[str],
    values: Sequence[float],
    *,
    lower: Sequence[float] | None = None,
    upper: Sequence[float] | None = None,
    unit: str = "",
    title: str = "",
    description: str = "",
    height: int = 240,
    width: int = 640,
    value_digits: int = 2,
    baseline_zero: bool = True,
) -> Chart:
    """Horizontal-free vertical bars with optional error bars and value labels."""
    if not labels:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no data")), title, description, height)

    lows = list(lower) if lower is not None else [float("nan")] * len(values)
    highs = list(upper) if upper is not None else [float("nan")] * len(values)
    finite = [v for v in values if v is not None and math.isfinite(v)]
    if not finite:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no data")), title, description, height)

    candidates = finite + [v for v in lows if math.isfinite(v)] + [v for v in highs if math.isfinite(v)]
    low = min(candidates + ([0.0] if baseline_zero else []))
    high = max(candidates + ([0.0] if baseline_zero else []))
    pad = (high - low) * 0.15 or 1.0
    low, high = low - pad, high + pad

    margin_left, margin_right, margin_top, margin_bottom = 46, 14, 18, 40
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    def y_of(value: float) -> float:
        return margin_top + plot_h * (1 - (value - low) / (high - low))

    ticks = nice_ticks(low, high, 4)
    slot = plot_w / len(labels)
    bar_w = min(64.0, slot * 0.55)

    parts: list[str] = []
    for tick in ticks:
        y = y_of(tick)
        parts.append(f'<line class="grid" x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}"/>')
        parts.append(
            f'<text class="axis" x="{margin_left - 8}" y="{y + 3.5:.2f}" text-anchor="end">{esc(_fmt(tick))}</text>'
        )
    zero_y = y_of(0.0) if low <= 0 <= high else margin_top + plot_h

    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        cx = margin_left + slot * (index + 0.5)
        if value is None or not math.isfinite(value):
            continue
        y = y_of(value)
        top = min(y, zero_y)
        bar_h = max(1.5, abs(zero_y - y))
        parts.append(
            f'<rect class="bar" x="{cx - bar_w / 2:.2f}" y="{top:.2f}" width="{bar_w:.2f}" height="{bar_h:.2f}" '
            f'rx="3" fill="{PALETTE[index % len(PALETTE)]}"><title>{esc(label)}: {esc(_fmt(value, value_digits))}{esc(unit)}</title></rect>'
        )
        if math.isfinite(lows[index]) and math.isfinite(highs[index]):
            y_low, y_high = y_of(lows[index]), y_of(highs[index])
            parts.append(
                f'<line class="error-bar" x1="{cx:.2f}" y1="{y_low:.2f}" x2="{cx:.2f}" y2="{y_high:.2f}"/>'
                f'<line class="error-bar" x1="{cx - 5:.2f}" y1="{y_low:.2f}" x2="{cx + 5:.2f}" y2="{y_low:.2f}"/>'
                f'<line class="error-bar" x1="{cx - 5:.2f}" y1="{y_high:.2f}" x2="{cx + 5:.2f}" y2="{y_high:.2f}"/>'
            )
        label_y = y - 6 if value >= 0 else top + bar_h + 14
        parts.append(
            f'<text class="value-label" x="{cx:.2f}" y="{label_y:.2f}" text-anchor="middle">'
            f"{esc(_fmt(value, value_digits))}{esc(unit)}</text>"
        )
        parts.append(
            f'<text class="axis" x="{cx:.2f}" y="{height - margin_bottom + 16}" text-anchor="middle">{esc(label)}</text>'
        )

    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Forest plot (treatment vs control)
# ---------------------------------------------------------------------------


def forest_plot(
    rows: Sequence[dict],
    *,
    title: str = "Treatment effect versus control",
    description: str = "",
    unit: str = "",
    height: int | None = None,
    width: int = 720,
) -> Chart:
    """Forest plot: one row per arm/endpoint with a CI whisker and a null line.

    ``rows`` items: ``{"label", "group", "estimate", "low", "high", "p_value",
    "significant" (optional bool)}``.
    """
    if not rows:
        height = height or 200
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no comparisons")), title, description, height)

    row_h = 30
    margin_top, margin_bottom, margin_left, margin_right = 26, 34, 250, 90
    height = height or margin_top + margin_bottom + row_h * len(rows)
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    values = [r["estimate"] for r in rows] + [r["low"] for r in rows] + [r["high"] for r in rows]
    values = [v for v in values if v is not None and math.isfinite(v)] + [0.0]
    low, high = min(values), max(values)
    pad = (high - low) * 0.12 or 0.5
    low, high = low - pad, high + pad

    def x_of(value: float) -> float:
        return margin_left + plot_w * (value - low) / (high - low)

    parts: list[str] = []
    # Null line (no effect) - the single most important reference in the plot.
    zero_x = x_of(0.0)
    parts.append(f'<line class="null-line" x1="{zero_x:.2f}" y1="{margin_top - 4}" x2="{zero_x:.2f}" y2="{margin_top + plot_h + 4}"/>')
    for tick in nice_ticks(low, high, 5):
        x = x_of(tick)
        parts.append(f'<line class="grid" x1="{x:.2f}" y1="{margin_top - 4}" x2="{x:.2f}" y2="{margin_top + plot_h + 4}"/>')
        parts.append(
            f'<text class="axis" x="{x:.2f}" y="{height - margin_bottom + 18}" text-anchor="middle">{esc(_fmt(tick))}{esc(unit)}</text>'
        )

    for index, row in enumerate(rows):
        y = margin_top + row_h * index + row_h / 2
        estimate, ci_low, ci_high = row["estimate"], row["low"], row["high"]
        group_index = int(row.get("group_index", index))
        colour = PALETTE[group_index % len(PALETTE)]
        label = str(row.get("label", ""))
        group = str(row.get("group", ""))
        favours = "favours treatment" if estimate < 0 else "favours control"
        tooltip = f"{label} ({group}): {_fmt(estimate)}{unit} [{_fmt(ci_low)}, {_fmt(ci_high)}] p={row.get('p_value', float('nan')):.4g} - {favours}"
        parts.append(
            f'<text class="row-label" x="{margin_left - 16}" y="{y + 4:.2f}" text-anchor="end">{esc(label)}</text>'
        )
        if group:
            parts.append(
                f'<text class="row-group" x="{margin_left - 16}" y="{y + 16:.2f}" text-anchor="end">{esc(group)}</text>'
            )
        if math.isfinite(ci_low) and math.isfinite(ci_high):
            parts.append(
                f'<line class="ci" x1="{x_of(ci_low):.2f}" y1="{y:.2f}" x2="{x_of(ci_high):.2f}" y2="{y:.2f}" stroke="{colour}"/>'
            )
        if math.isfinite(estimate):
            parts.append(
                f'<circle class="point" cx="{x_of(estimate):.2f}" cy="{y:.2f}" r="5" fill="{colour}"><title>{esc(tooltip)}</title></circle>'
            )
        p_value = row.get("p_value")
        p_text = "" if p_value is None or not math.isfinite(p_value) else ("<0.0001" if p_value < 0.0001 else f"{p_value:.4f}")
        parts.append(
            f'<text class="axis" x="{width - margin_right + 12}" y="{y + 4:.2f}">{esc(p_text)}</text>'
        )

    parts.append(
        f'<text class="axis" x="{width - margin_right + 12}" y="{margin_top - 10}" >p-value</text>'
    )
    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Line chart with confidence band (trajectories)
# ---------------------------------------------------------------------------


def line_with_band(
    x_values: Sequence[float],
    series: Sequence[dict],
    *,
    title: str = "",
    description: str = "",
    x_label: str = "",
    y_label: str = "",
    height: int = 260,
    width: int = 720,
    band: bool = True,
    zero_line: float | None = None,
) -> Chart:
    """Multi-series line chart; each series may carry ``low``/``high`` arrays.

    ``series`` items: ``{"name", "values", "low" (optional), "high" (optional)}``.
    """
    if not x_values or not series:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no data")), title, description, height)

    all_values: list[float] = []
    for item in series:
        for key in ("values", "low", "high"):
            values = item.get(key)
            if values is None:
                continue
            all_values.extend(v for v in values if v is not None and math.isfinite(v))
    if zero_line is not None:
        all_values.append(zero_line)
    if not all_values:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no data")), title, description, height)

    low, high = min(all_values), max(all_values)
    pad = (high - low) * 0.12 or 1.0
    low, high = low - pad, high + pad

    margin_left, margin_right, margin_top, margin_bottom = 52, 96, 22, 44
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    x_low, x_high = min(x_values), max(x_values)

    def x_of(value: float) -> float:
        return margin_left + (plot_w * (value - x_low) / (x_high - x_low) if x_high > x_low else plot_w / 2)

    def y_of(value: float) -> float:
        return margin_top + plot_h * (1 - (value - low) / (high - low))

    parts: list[str] = []
    for tick in nice_ticks(low, high, 4):
        y = y_of(tick)
        parts.append(f'<line class="grid" x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis" x="{margin_left - 8}" y="{y + 3.5:.2f}" text-anchor="end">{esc(_fmt(tick))}</text>')
    for tick in nice_ticks(x_low, x_high, min(6, max(2, len(x_values)))):
        x = x_of(tick)
        parts.append(f'<text class="axis" x="{x:.2f}" y="{height - margin_bottom + 18}" text-anchor="middle">{esc(_fmt(tick, 0))}</text>')

    if zero_line is not None:
        y = y_of(zero_line)
        parts.append(f'<line class="null-line" x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}"/>')

    legend_y = margin_top - 8
    for index, item in enumerate(series):
        colour = PALETTE[index % len(PALETTE)]
        name = str(item.get("name", f"series {index + 1}"))
        values = [v if v is not None and math.isfinite(v) else None for v in item["values"]]
        lows = item.get("low")
        highs = item.get("high")
        if band and lows is not None and highs is not None:
            upper = " ".join(
                f"{x_of(x):.2f},{y_of(hi):.2f}"
                for x, hi in zip(x_values, highs, strict=False)
                if hi is not None and math.isfinite(hi)
            )
            lower = " ".join(
                f"{x_of(x):.2f},{y_of(lo):.2f}"
                for x, lo in zip(reversed(list(x_values)), reversed(list(lows)), strict=False)
                if lo is not None and math.isfinite(lo)
            )
            if upper and lower:
                parts.append(f'<polygon class="band" points="{upper} {lower}" fill="{colour}"/>')
        points = [
            f"{x_of(x):.2f},{y_of(value):.2f}"
            for x, value in zip(x_values, values, strict=False)
            if value is not None
        ]
        if len(points) >= 2:
            parts.append(f'<polyline class="line" points="{" ".join(points)}" stroke="{colour}"/>')
        for x, value in zip(x_values, values, strict=False):
            if value is None:
                continue
            parts.append(
                f'<circle class="point" cx="{x_of(x):.2f}" cy="{y_of(value):.2f}" r="3" fill="{colour}">'
                f"<title>{esc(name)} @ {esc(_fmt(x, 0))}: {esc(_fmt(value))}</title></circle>"
            )
        parts.append(
            f'<rect x="{width - margin_right + 8}" y="{legend_y + index * 18}" width="10" height="10" rx="2" fill="{colour}"/>'
            f'<text class="legend" x="{width - margin_right + 24}" y="{legend_y + index * 18 + 9}">{esc(name[:18])}</text>'
        )

    if x_label:
        parts.append(
            f'<text class="axis-title" x="{margin_left + plot_w / 2:.2f}" y="{height - 6}" text-anchor="middle">{esc(x_label)}</text>'
        )
    if y_label:
        parts.append(
            f'<text class="axis-title" x="14" y="{margin_top + plot_h / 2:.2f}" text-anchor="middle" '
            f'transform="rotate(-90 14 {margin_top + plot_h / 2:.2f})">{esc(y_label)}</text>'
        )
    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Heatmap (arm x adverse-event term)
# ---------------------------------------------------------------------------


def heatmap(
    row_labels: Sequence[str],
    column_labels: Sequence[str],
    values: Sequence[Sequence[float]],
    *,
    title: str = "",
    description: str = "",
    value_suffix: str = "",
    height: int | None = None,
    cell_width: int = 96,
    cell_height: int = 30,
    label_width: int = 170,
) -> Chart:
    """Matrix view with a sequential colour scale and per-cell tooltips."""
    if not row_labels or not column_labels:
        height = height or 160
        return Chart(_frame(640, height, title=title, description=description, body=_empty_state(640, height, "no data")), title, description, height)

    width = label_width + cell_width * len(column_labels) + 24
    height = height or 40 + cell_height * len(row_labels) + 34
    flat = [v for row in values for v in row if v is not None and math.isfinite(v)]
    peak = max(flat) if flat else 1.0

    parts: list[str] = []
    for column_index, column in enumerate(column_labels):
        x = label_width + cell_width * column_index + cell_width / 2
        parts.append(
            f'<text class="axis" x="{x:.2f}" y="20" text-anchor="middle">{esc(column[:14])}</text>'
        )
    for row_index, row_label in enumerate(row_labels):
        y = 34 + cell_height * row_index
        parts.append(f'<text class="row-label" x="{label_width - 12}" y="{y + cell_height / 2 + 4:.2f}" text-anchor="end">{esc(row_label[:24])}</text>')
        row = values[row_index] if row_index < len(values) else []
        for column_index, column in enumerate(column_labels):
            value = row[column_index] if column_index < len(row) else float("nan")
            x = label_width + cell_width * column_index
            if value is None or not math.isfinite(value):
                parts.append(
                    f'<rect class="heat-cell empty" x="{x + 2:.2f}" y="{y + 2:.2f}" width="{cell_width - 4}" height="{cell_height - 4}" rx="4"/>'
                )
                continue
            intensity = 0.08 + 0.85 * (value / peak if peak > 0 else 0)
            text_class = "heat-value strong" if intensity > 0.55 else "heat-value"
            parts.append(
                f'<rect class="heat-cell" x="{x + 2:.2f}" y="{y + 2:.2f}" width="{cell_width - 4}" height="{cell_height - 4}" rx="4" '
                f'fill="var(--c-accent)" fill-opacity="{intensity:.3f}">'
                f"<title>{esc(row_label)} / {esc(column)}: {esc(_fmt(value, 1))}{esc(value_suffix)}</title></rect>"
            )
            parts.append(
                f'<text class="{text_class}" x="{x + cell_width / 2:.2f}" y="{y + cell_height / 2 + 4:.2f}" text-anchor="middle">'
                f"{esc(_fmt(value, 1))}{esc(value_suffix)}</text>"
            )

    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Stacked bars (CTCAE grade distribution)
# ---------------------------------------------------------------------------


def stacked_bars(
    labels: Sequence[str],
    stacks: Sequence[Sequence[float]],
    *,
    stack_labels: Sequence[str] = ("G1", "G2", "G3", "G4", "G5"),
    title: str = "",
    description: str = "",
    height: int = 230,
    width: int = 640,
) -> Chart:
    """Grade distribution per arm: 100%-stacked horizontal bars."""
    if not labels:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no data")), title, description, height)

    totals = [sum(max(0.0, v) for v in row) for row in stacks]
    if not any(totals):
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no events")), title, description, height)

    margin_left, margin_right, margin_top, margin_bottom = 140, 60, 26, 16
    row_h = 30
    height = height or margin_top + margin_bottom + row_h * len(labels)
    plot_w = width - margin_left - margin_right
    parts: list[str] = []

    for index, label in enumerate(labels):
        y = margin_top + index * row_h
        total = totals[index] or 1.0
        x = float(margin_left)
        for stack_index, value in enumerate(stacks[index]):
            share = max(0.0, value) / total
            segment = plot_w * share
            if segment <= 0:
                continue
            colour = GRADE_COLORS[stack_index % len(GRADE_COLORS)]
            parts.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{segment:.2f}" height="{row_h - 8}" rx="2" fill="{colour}">'
                f"<title>{esc(label)} {esc(stack_labels[stack_index])}: {esc(_fmt(value, 2))}</title></rect>"
            )
            if segment > 26:
                parts.append(
                    f'<text class="stack-value" x="{x + segment / 2:.2f}" y="{y + (row_h - 8) / 2 + 4:.2f}" text-anchor="middle">'
                    f"{esc(_fmt(value, 0))}</text>"
                )
            x += segment
        parts.append(f'<text class="row-label" x="{margin_left - 12}" y="{y + (row_h - 8) / 2 + 4:.2f}" text-anchor="end">{esc(label[:22])}</text>')

    legend_x = float(margin_left)
    for stack_index, stack_label in enumerate(stack_labels):
        parts.append(
            f'<rect x="{legend_x}" y="{height - 14}" width="10" height="10" rx="2" fill="{GRADE_COLORS[stack_index % len(GRADE_COLORS)]}"/>'
            f'<text class="legend" x="{legend_x + 15}" y="{height - 5}">{esc(stack_label)}</text>'
        )
        legend_x += 64

    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Patient timeline (the "digital twin" view)
# ---------------------------------------------------------------------------


def patient_timeline(
    epochs: Sequence[int],
    *,
    dose_mg: Sequence[float],
    concentration: Sequence[float],
    sbp: Sequence[float],
    adverse_events: dict[int, list[str]] | None = None,
    title: str = "Patient trajectory",
    description: str = "",
    height: int = 260,
    width: int = 720,
) -> Chart:
    """Dose bars, concentration curve and systolic BP for a single patient."""
    if not epochs:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no data")), title, description, height)

    adverse_events = adverse_events or {}
    margin_left, margin_right, margin_top, margin_bottom = 54, 104, 30, 40
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    concentrations = [v for v in concentration if v is not None and math.isfinite(v)]
    sbp_values = [v for v in sbp if v is not None and math.isfinite(v)]
    max_conc = max(concentrations) if concentrations else 1.0
    sbp_low = min(sbp_values) - 6 if sbp_values else 0.0
    sbp_high = max(sbp_values) + 6 if sbp_values else 1.0
    max_dose = max([d for d in dose_mg if d and math.isfinite(d)] or [1.0])

    def x_of(epoch: float) -> float:
        span = max(epochs) - min(epochs)
        return margin_left + (plot_w * (epoch - min(epochs)) / span if span else plot_w / 2)

    def y_conc(value: float) -> float:
        return margin_top + plot_h * (1 - value / (max_conc * 1.15 or 1.0))

    def y_sbp(value: float) -> float:
        return margin_top + plot_h * (1 - (value - sbp_low) / ((sbp_high - sbp_low) or 1.0))

    parts: list[str] = []
    for tick in nice_ticks(sbp_low, sbp_high, 4):
        y = y_sbp(tick)
        parts.append(f'<line class="grid" x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis" x="{margin_left - 8}" y="{y + 3.5:.2f}" text-anchor="end">{esc(_fmt(tick, 0))}</text>')

    bar_slot = plot_w / max(1, len(epochs))
    for index, epoch in enumerate(epochs):
        dose = dose_mg[index] if index < len(dose_mg) else 0.0
        if not dose or not math.isfinite(dose) or dose <= 0:
            continue
        bar_h = plot_h * 0.22 * (dose / max_dose)
        x = margin_left + bar_slot * index + bar_slot * 0.18
        parts.append(
            f'<rect class="dose-bar" x="{x:.2f}" y="{margin_top + plot_h - bar_h:.2f}" width="{bar_slot * 0.64:.2f}" '
            f'height="{bar_h:.2f}" rx="2"><title>epoch {esc(epoch)}: {esc(_fmt(dose, 0))} mg</title></rect>'
        )

    conc_points = " ".join(
        f"{x_of(epoch):.2f},{y_conc(value):.2f}"
        for epoch, value in zip(epochs, concentration, strict=False)
        if value is not None and math.isfinite(value)
    )
    if conc_points:
        parts.append(f'<polyline class="line" points="{conc_points}" stroke="{PALETTE[0]}"/>')
    sbp_points = " ".join(
        f"{x_of(epoch):.2f},{y_sbp(value):.2f}"
        for epoch, value in zip(epochs, sbp, strict=False)
        if value is not None and math.isfinite(value)
    )
    if sbp_points:
        parts.append(f'<polyline class="line dashed" points="{sbp_points}" stroke="{PALETTE[2]}"/>')

    for index, epoch in enumerate(epochs):
        for term in adverse_events.get(int(epoch), []):
            x = margin_left + bar_slot * index + bar_slot / 2
            parts.append(
                f'<circle class="ae-marker" cx="{x:.2f}" cy="{margin_top - 12}" r="5">'
                f"<title>epoch {esc(epoch)}: {esc(term)}</title></circle>"
            )

    parts.append(f'<rect x="{width - margin_right + 8}" y="{margin_top - 16}" width="10" height="10" rx="2" fill="{PALETTE[0]}"/>')
    parts.append(f'<text class="legend" x="{width - margin_right + 24}" y="{margin_top - 7}">concentration</text>')
    parts.append(f'<rect x="{width - margin_right + 8}" y="{margin_top + 2}" width="10" height="10" rx="2" fill="{PALETTE[2]}"/>')
    parts.append(f'<text class="legend" x="{width - margin_right + 24}" y="{margin_top + 11}">systolic BP</text>')
    parts.append(f'<rect x="{width - margin_right + 8}" y="{margin_top + 20}" width="10" height="10" rx="2" fill="var(--c-accent)"/>')
    parts.append(f'<text class="legend" x="{width - margin_right + 24}" y="{margin_top + 29}">dose</text>')
    parts.append(f'<circle class="ae-marker" cx="{width - margin_right + 13}" cy="{margin_top + 43}" r="5"/>')
    parts.append(f'<text class="legend" x="{width - margin_right + 24}" y="{margin_top + 47}">adverse event</text>')

    for epoch in epochs:
        parts.append(f'<text class="axis" x="{x_of(epoch):.2f}" y="{height - margin_bottom + 18}" text-anchor="middle">{esc(epoch)}</text>')
    parts.append(
        f'<text class="axis-title" x="{margin_left + plot_w / 2:.2f}" y="{height - 6}" text-anchor="middle">'
        f"simulated epoch</text>"
    )

    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Histogram / distribution strip
# ---------------------------------------------------------------------------


def histogram(
    values: Sequence[float],
    *,
    title: str = "",
    description: str = "",
    bins: int = 18,
    unit: str = "",
    height: int = 180,
    width: int = 420,
    accent_index: int = 0,
) -> Chart:
    """Simple histogram used for LLM token and latency distributions."""
    clean = [v for v in values if v is not None and math.isfinite(v)]
    if not clean:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "no observations")), title, description, height)

    low, high = min(clean), max(clean)
    if high <= low:
        high = low + 1.0
    width_bin = (high - low) / bins
    counts = [0] * bins
    for value in clean:
        index = min(bins - 1, int((value - low) / width_bin))
        counts[index] += 1
    peak = max(counts)

    margin_left, margin_right, margin_top, margin_bottom = 40, 12, 22, 34
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    bar_w = plot_w / bins
    parts: list[str] = []
    for tick in nice_ticks(0, peak, 3):
        y = margin_top + plot_h * (1 - tick / peak)
        parts.append(f'<line class="grid" x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis" x="{margin_left - 8}" y="{y + 3.5:.2f}" text-anchor="end">{esc(_fmt(tick, 0))}</text>')
    for index, count in enumerate(counts):
        bar_h = plot_h * (count / peak)
        x = margin_left + index * bar_w
        bin_low = low + index * width_bin
        parts.append(
            f'<rect x="{x + 1:.2f}" y="{margin_top + plot_h - bar_h:.2f}" width="{max(1.0, bar_w - 2):.2f}" '
            f'height="{bar_h:.2f}" rx="2" fill="{PALETTE[accent_index % len(PALETTE)]}">'
            f"<title>{esc(_fmt(bin_low))}-{esc(_fmt(bin_low + width_bin))}{esc(unit)}: {count}</title></rect>"
        )
    for tick in nice_ticks(low, high, 4):
        x = margin_left + plot_w * (tick - low) / (high - low)
        parts.append(f'<text class="axis" x="{x:.2f}" y="{height - margin_bottom + 18}" text-anchor="middle">{esc(_fmt(tick))}</text>')
    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


# ---------------------------------------------------------------------------
# Stopping-rule monitor (observed vs threshold over epochs)
# ---------------------------------------------------------------------------


def threshold_monitor(
    epochs: Sequence[int],
    observed: Sequence[float],
    threshold: float,
    *,
    title: str = "",
    description: str = "",
    height: int = 200,
    width: int = 520,
) -> Chart:
    """Observed DSMB metric against its threshold, with the trigger region shaded."""
    if not epochs or not observed:
        return Chart(_frame(width, height, title=title, description=description, body=_empty_state(width, height, "not evaluated")), title, description, height)

    values = [v for v in observed if v is not None and math.isfinite(v)]
    high = max([*values, threshold]) * 1.2 or 1.0
    low = 0.0
    margin_left, margin_right, margin_top, margin_bottom = 44, 14, 18, 34
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    def x_of(epoch: float) -> float:
        span = max(epochs) - min(epochs)
        return margin_left + (plot_w * (epoch - min(epochs)) / span if span else plot_w / 2)

    def y_of(value: float) -> float:
        return margin_top + plot_h * (1 - (value - low) / (high - low))

    parts: list[str] = []
    trigger_y = y_of(threshold)
    parts.append(
        f'<rect class="trigger-region" x="{margin_left}" y="{margin_top}" width="{plot_w}" '
        f'height="{max(0.0, trigger_y - margin_top):.2f}"/>'
    )
    for tick in nice_ticks(low, high, 4):
        y = y_of(tick)
        parts.append(f'<line class="grid" x1="{margin_left}" y1="{y:.2f}" x2="{width - margin_right}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis" x="{margin_left - 8}" y="{y + 3.5:.2f}" text-anchor="end">{esc(_fmt(tick * 100, 0))}%</text>')
    parts.append(f'<line class="threshold-line" x1="{margin_left}" y1="{trigger_y:.2f}" x2="{width - margin_right}" y2="{trigger_y:.2f}"/>')

    points = " ".join(
        f"{x_of(epoch):.2f},{y_of(value):.2f}"
        for epoch, value in zip(epochs, observed, strict=False)
        if value is not None and math.isfinite(value)
    )
    if points:
        parts.append(f'<polyline class="line" points="{points}" stroke="{PALETTE[0]}"/>')
    triggered_any = False
    for epoch, value in zip(epochs, observed, strict=False):
        if value is None or not math.isfinite(value):
            continue
        hit = value >= threshold
        triggered_any = triggered_any or hit
        parts.append(
            f'<circle class="{"point triggered" if hit else "point"}" cx="{x_of(epoch):.2f}" cy="{y_of(value):.2f}" r="{5 if hit else 3}" '
            f'fill="{"var(--c-critical)" if hit else PALETTE[0]}"><title>epoch {esc(epoch)}: {esc(_fmt(value * 100, 1))}% (threshold {esc(_fmt(threshold * 100, 1))}%)</title></circle>'
        )
    for epoch in epochs:
        parts.append(f'<text class="axis" x="{x_of(epoch):.2f}" y="{height - margin_bottom + 18}" text-anchor="middle">{esc(epoch)}</text>')
    if triggered_any:
        parts.append(f'<text class="alert-label" x="{margin_left + 4}" y="{margin_top + 12}">threshold crossed</text>')
    body = "".join(parts)
    return Chart(_frame(width, height, title=title, description=description, body=body), title, description, height)


def sparkline(values: Iterable[float], *, width: int = 120, height: int = 28, colour: str = "var(--c-accent)") -> str:
    """Tiny inline trend line for table cells."""
    clean = [v for v in values if v is not None and math.isfinite(v)]
    if len(clean) < 2:
        return f'<svg class="sparkline" viewBox="0 0 {width} {height}" width="{width}" height="{height}"></svg>'
    low, high = min(clean), max(clean)
    span = (high - low) or 1.0
    points = " ".join(
        f"{4 + (width - 8) * i / (len(clean) - 1):.1f},{height - 4 - (height - 8) * (v - low) / span:.1f}"
        for i, v in enumerate(clean)
    )
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">'
        f'<polyline points="{points}" fill="none" stroke="{colour}" stroke-width="1.5"/></svg>'
    )
