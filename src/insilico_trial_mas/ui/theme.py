"""The design system: one stylesheet, shared by the dashboard and the Studio.

The look is deliberately restrained - it has to survive being shown to a
pharmacovigilance reviewer, printed to PDF for a trial team meeting, and projected
in a demo room. That means: a neutral slate palette, a single clinical accent, no
gradients on data, tabular numerals for every metric, hairline borders instead of
heavy shadows, and one semantic colour scale that only appears for risk.

Colour is defined once as CSS custom properties so that

* light and dark mode are a variable swap (``prefers-color-scheme`` plus an
  explicit ``data-theme`` override for the in-page toggle),
* inline SVG charts inherit the palette through ``var(--c-*)`` - no colour
  duplication between Python and CSS,
* a customer can rebrand the dashboard by overriding a handful of variables.
"""

from __future__ import annotations

#: Light theme tokens.
LIGHT_TOKENS: dict[str, str] = {
    "--c-bg": "#f6f8fa",
    "--c-surface": "#ffffff",
    "--c-surface-2": "#f2f5f8",
    "--c-surface-3": "#e9eef3",
    "--c-border": "#d7dee6",
    "--c-border-strong": "#bcc7d3",
    "--c-text": "#101c28",
    "--c-text-muted": "#5a6b7b",
    "--c-text-faint": "#8798a8",
    "--c-accent": "#0b6e99",
    "--c-accent-strong": "#085a7d",
    "--c-accent-soft": "#e3f0f7",
    "--c-good": "#1c7c54",
    "--c-good-soft": "#e4f3ec",
    "--c-warn": "#a2701a",
    "--c-warn-soft": "#fbf1dc",
    "--c-critical": "#a8321f",
    "--c-critical-soft": "#fbe9e5",
    "--c-series-1": "#0b6e99",
    "--c-series-2": "#7a5ea8",
    "--c-series-3": "#1c7c54",
    "--c-series-4": "#a2701a",
    "--c-series-5": "#a8321f",
    "--c-series-6": "#5a6b7b",
    "--c-grade-1": "#bcd9e6",
    "--c-grade-2": "#7fb3cc",
    "--c-grade-3": "#d9a441",
    "--c-grade-4": "#c96a34",
    "--c-grade-5": "#a8321f",
    "--c-grid": "#e5eaef",
    "--c-shadow": "0 1px 2px rgba(16, 28, 40, 0.05), 0 1px 8px rgba(16, 28, 40, 0.04)",
}

#: Dark theme tokens (same roles, re-tuned for a dark background).
DARK_TOKENS: dict[str, str] = {
    "--c-bg": "#0d141b",
    "--c-surface": "#131c25",
    "--c-surface-2": "#18222c",
    "--c-surface-3": "#1f2b36",
    "--c-border": "#25323e",
    "--c-border-strong": "#33424f",
    "--c-text": "#e8eef4",
    "--c-text-muted": "#9fb0bf",
    "--c-text-faint": "#738699",
    "--c-accent": "#4aa8d0",
    "--c-accent-strong": "#6dbde0",
    "--c-accent-soft": "#12293a",
    "--c-good": "#4fbf8b",
    "--c-good-soft": "#10291f",
    "--c-warn": "#e0b34a",
    "--c-warn-soft": "#2b2413",
    "--c-critical": "#e2725a",
    "--c-critical-soft": "#2e1713",
    "--c-series-1": "#4aa8d0",
    "--c-series-2": "#a58fd6",
    "--c-series-3": "#4fbf8b",
    "--c-series-4": "#e0b34a",
    "--c-series-5": "#e2725a",
    "--c-series-6": "#9fb0bf",
    "--c-grade-1": "#2c4c5e",
    "--c-grade-2": "#3f7791",
    "--c-grade-3": "#c08a2c",
    "--c-grade-4": "#c96a34",
    "--c-grade-5": "#e2725a",
    "--c-grid": "#1e2a35",
    "--c-shadow": "0 1px 2px rgba(0, 0, 0, 0.4), 0 1px 12px rgba(0, 0, 0, 0.25)",
}


def _tokens_block(selector: str, tokens: dict[str, str]) -> str:
    lines = "\n".join(f"    {name}: {value};" for name, value in tokens.items())
    return f"{selector} {{\n{lines}\n}}"


#: Structural CSS: layout, components and print rules. Deliberately framework-free.
STRUCTURE_CSS = """
*, *::before, *::after { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--c-bg);
  color: var(--c-text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  font-variant-numeric: tabular-nums;
}

a { color: var(--c-accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.wrap { max-width: 1240px; margin: 0 auto; padding: 0 24px 72px; }

/* ---------------------------------------------------------------- header */
.site-header {
  position: sticky; top: 0; z-index: 20;
  background: color-mix(in srgb, var(--c-surface) 92%, transparent);
  backdrop-filter: saturate(140%) blur(8px);
  border-bottom: 1px solid var(--c-border);
}
.site-header__inner {
  max-width: 1240px; margin: 0 auto; padding: 14px 24px;
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 650; letter-spacing: -0.01em; }
.brand__mark {
  width: 26px; height: 26px; border-radius: 7px;
  background: linear-gradient(150deg, var(--c-accent), var(--c-series-2));
  display: grid; place-items: center; color: #fff; font-size: 13px; font-weight: 700;
}
.brand__sub { color: var(--c-text-muted); font-weight: 400; font-size: 13px; }
.header-spacer { flex: 1 1 auto; }
.header-actions { display: flex; gap: 8px; align-items: center; }

/* ---------------------------------------------------------------- pieces */
.eyebrow {
  text-transform: uppercase; letter-spacing: 0.09em; font-size: 11px; font-weight: 650;
  color: var(--c-text-faint);
}
h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: -0.015em; }
h2 { font-size: 17px; margin: 0 0 4px; letter-spacing: -0.01em; }
h3 { font-size: 14px; margin: 0 0 8px; }
p { margin: 0 0 12px; }
.lede { color: var(--c-text-muted); max-width: 78ch; }
.muted { color: var(--c-text-muted); }
.faint { color: var(--c-text-faint); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }

/* ---------------------------------------------------------------- layout */
.page-head { padding: 28px 0 8px; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--c-border); margin: 18px 0 22px; overflow-x: auto; }
.tab {
  appearance: none; border: 0; background: none; cursor: pointer;
  padding: 10px 14px; font: inherit; font-size: 14px; color: var(--c-text-muted);
  border-bottom: 2px solid transparent; white-space: nowrap;
}
.tab[aria-selected="true"] { color: var(--c-text); border-bottom-color: var(--c-accent); font-weight: 600; }
.tab:hover { color: var(--c-text); }

.panel { display: none; }
.panel[data-active="true"] { display: block; animation: fade 140ms ease-out; }
@keyframes fade { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }

.grid { display: grid; gap: 16px; }
.grid--kpi { grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); }
.grid--2 { grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); }
.grid--3 { grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }

.card {
  background: var(--c-surface); border: 1px solid var(--c-border); border-radius: 10px;
  padding: 16px 18px; box-shadow: var(--c-shadow);
}
.card--flush { padding: 0; overflow: hidden; }
.card__head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  padding: 14px 18px 10px;
}
.card__body { padding: 0 18px 16px; }
.card__foot { padding: 10px 18px 14px; border-top: 1px solid var(--c-border); color: var(--c-text-muted); font-size: 12.5px; }

.kpi .kpi__value { font-size: 25px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.15; }
.kpi .kpi__label { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--c-text-faint); margin-bottom: 6px; }
.kpi .kpi__hint { font-size: 12.5px; color: var(--c-text-muted); }
.kpi--good .kpi__value { color: var(--c-good); }
.kpi--warn .kpi__value { color: var(--c-warn); }
.kpi--critical .kpi__value { color: var(--c-critical); }

/* ---------------------------------------------------------------- tables */
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
thead th {
  text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--c-text-faint); font-weight: 650; padding: 8px 10px; border-bottom: 1px solid var(--c-border);
  position: sticky; top: 0; background: var(--c-surface); cursor: default;
}
thead th[data-sortable="true"] { cursor: pointer; user-select: none; }
thead th[data-sortable="true"]:hover { color: var(--c-text); }
tbody td { padding: 8px 10px; border-bottom: 1px solid var(--c-grid); vertical-align: top; }
tbody tr:hover { background: var(--c-surface-2); }
.table-scroll { max-height: 520px; overflow: auto; }
.num { text-align: right; font-variant-numeric: tabular-nums; }

/* ---------------------------------------------------------------- chips */
.chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 600; border: 1px solid var(--c-border-strong); color: var(--c-text-muted);
  background: var(--c-surface-2);
}
.chip--accent { color: var(--c-accent-strong); border-color: color-mix(in srgb, var(--c-accent) 40%, transparent); background: var(--c-accent-soft); }
.chip--good { color: var(--c-good); border-color: color-mix(in srgb, var(--c-good) 40%, transparent); background: var(--c-good-soft); }
.chip--warn { color: var(--c-warn); border-color: color-mix(in srgb, var(--c-warn) 40%, transparent); background: var(--c-warn-soft); }
.chip--critical { color: var(--c-critical); border-color: color-mix(in srgb, var(--c-critical) 40%, transparent); background: var(--c-critical-soft); }

.btn {
  appearance: none; font: inherit; font-size: 13.5px; font-weight: 550; cursor: pointer;
  padding: 7px 13px; border-radius: 8px; border: 1px solid var(--c-border-strong);
  background: var(--c-surface); color: var(--c-text);
}
.btn:hover { border-color: var(--c-accent); color: var(--c-accent-strong); }
.btn--primary { background: var(--c-accent); border-color: var(--c-accent); color: #fff; }
.btn--primary:hover { background: var(--c-accent-strong); border-color: var(--c-accent-strong); color: #fff; }
.btn--ghost { border-color: transparent; background: transparent; color: var(--c-text-muted); }

/* ---------------------------------------------------------------- notes */
.note {
  border-left: 3px solid var(--c-accent); background: var(--c-surface-2);
  padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13.5px; color: var(--c-text-muted);
}
.note--warn { border-left-color: var(--c-warn); background: var(--c-warn-soft); color: var(--c-text); }
.note--critical { border-left-color: var(--c-critical); background: var(--c-critical-soft); color: var(--c-text); }
.note--good { border-left-color: var(--c-good); background: var(--c-good-soft); color: var(--c-text); }

.disclaimer { margin: 16px 0 0; }

/* ---------------------------------------------------------------- charts */
.chart { display: block; overflow: visible; }
.chart .grid { stroke: var(--c-grid); stroke-width: 1; }
.chart .axis { fill: var(--c-text-faint); font-size: 11px; }
.chart .axis-title { fill: var(--c-text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
.chart .legend { fill: var(--c-text-muted); font-size: 11.5px; }
.chart .row-label { fill: var(--c-text); font-size: 12.5px; }
.chart .row-group { fill: var(--c-text-faint); font-size: 11px; }
.chart .value-label { fill: var(--c-text); font-size: 11.5px; font-weight: 600; }
.chart .chart-empty { fill: var(--c-text-faint); font-size: 12.5px; }
.chart .bar { opacity: 0.92; }
.chart .bar:hover { opacity: 1; }
.chart .error-bar { stroke: var(--c-text-faint); stroke-width: 1.4; }
.chart .ci { stroke-width: 2; opacity: 0.85; }
.chart .point { stroke: var(--c-surface); stroke-width: 1.5; }
.chart .line { fill: none; stroke-width: 2; }
.chart .line.dashed { stroke-dasharray: 5 4; opacity: 0.85; }
.chart .band { opacity: 0.14; }
.chart .null-line { stroke: var(--c-border-strong); stroke-width: 1.4; stroke-dasharray: 4 3; }
.chart .threshold-line { stroke: var(--c-critical); stroke-width: 1.6; stroke-dasharray: 6 4; }
.chart .trigger-region { fill: var(--c-critical); opacity: 0.06; }
.chart .alert-label { fill: var(--c-critical); font-size: 11px; font-weight: 650; }
.chart .heat-cell { fill: var(--c-surface-2); }
.chart .heat-cell.empty { fill: var(--c-surface-3); }
.chart .heat-value { fill: var(--c-text); font-size: 11.5px; }
.chart .heat-value.strong { fill: #fff; font-weight: 650; }
.chart .stack-value { fill: rgba(255,255,255,0.92); font-size: 11px; font-weight: 600; }
.chart .dose-bar { fill: var(--c-accent); opacity: 0.28; }
.chart .ae-marker { fill: var(--c-critical); stroke: var(--c-surface); stroke-width: 1.2; }
.sparkline { display: inline-block; vertical-align: middle; }

/* ---------------------------------------------------------------- misc */
.def-list { display: grid; grid-template-columns: minmax(140px, 220px) 1fr; gap: 6px 18px; font-size: 13.5px; }
.def-list dt { color: var(--c-text-muted); }
.def-list dd { margin: 0; }

.consort { display: grid; gap: 10px; }
.consort__step {
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
  border: 1px solid var(--c-border); border-left: 3px solid var(--c-accent);
  border-radius: 8px; padding: 10px 14px; background: var(--c-surface-2);
}
.consort__label { font-size: 13.5px; }
.consort__value { font-weight: 650; font-size: 16px; }

.alert-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.alert-list li { display: flex; gap: 10px; align-items: flex-start; font-size: 13.5px; }

.search-input {
  font: inherit; font-size: 13.5px; padding: 7px 11px; border-radius: 8px;
  border: 1px solid var(--c-border-strong); background: var(--c-surface); color: var(--c-text); min-width: 220px;
}
.patient-list { max-height: 420px; overflow: auto; border: 1px solid var(--c-border); border-radius: 8px; }
.patient-row {
  display: grid; grid-template-columns: 1fr auto auto auto; gap: 10px; align-items: center;
  padding: 8px 12px; border-bottom: 1px solid var(--c-grid); cursor: pointer; font-size: 13px;
}
.patient-row:last-child { border-bottom: 0; }
.patient-row:hover { background: var(--c-surface-2); }
.patient-row[aria-selected="true"] { background: var(--c-accent-soft); }

.bar-track { height: 7px; border-radius: 999px; background: var(--c-surface-3); overflow: hidden; }
.bar-fill { height: 100%; background: var(--c-accent); border-radius: 999px; }

pre.code {
  background: var(--c-surface-2); border: 1px solid var(--c-border); border-radius: 8px;
  padding: 12px 14px; overflow: auto; font-size: 12.5px; margin: 0;
}

.flow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.flow__node {
  border: 1px solid var(--c-border); border-radius: 8px; padding: 8px 12px;
  background: var(--c-surface-2); font-size: 12.5px;
}
.flow__node--agent { border-left: 3px solid var(--c-series-2); }
.flow__node--table { border-left: 3px solid var(--c-accent); }
.flow__node--report { border-left: 3px solid var(--c-good); }
.flow__arrow { color: var(--c-text-faint); }

.site-footer {
  margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--c-border);
  color: var(--c-text-faint); font-size: 12.5px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
}

/* ---------------------------------------------------------------- print */
@media print {
  :root { color-scheme: light; }
  body { background: #fff; font-size: 11.5px; }
  .site-header, .tabs, .header-actions, .btn { display: none !important; }
  .panel { display: block !important; page-break-before: always; }
  .panel:first-of-type { page-break-before: avoid; }
  .card { box-shadow: none; break-inside: avoid; }
  .wrap { max-width: none; padding: 0; }
}
"""


def stylesheet() -> str:
    """Return the complete CSS: tokens (light + dark) plus structure."""
    return "\n".join(
        [
            ":root {",
            "    color-scheme: light dark;",
            *[f"    {name}: {value};" for name, value in LIGHT_TOKENS.items()],
            "}",
            "@media (prefers-color-scheme: dark) {",
            "  :root:not([data-theme=\"light\"]) {",
            *[f"      {name}: {value};" for name, value in DARK_TOKENS.items()],
            "  }",
            "}",
            "[data-theme=\"dark\"] {",
            *[f"    {name}: {value};" for name, value in DARK_TOKENS.items()],
            "}",
            STRUCTURE_CSS,
        ]
    )


def theme_script() -> str:
    """Minimal inline script: theme toggle + tab switching + table sorting.

    Kept in one place because both the dashboard and the Studio use it, and
    because a demo artefact must not depend on a bundle that can go missing.
    """
    return """
(function () {
  "use strict";
  var root = document.documentElement;

  function applyTheme(theme) {
    if (theme) { root.setAttribute("data-theme", theme); } else { root.removeAttribute("data-theme"); }
    try { window.localStorage.setItem("insilico-theme", theme || ""); } catch (e) { /* private mode */ }
  }
  try {
    var saved = window.localStorage.getItem("insilico-theme");
    if (saved) { applyTheme(saved); }
  } catch (e) { /* ignore */ }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement)) { return; }

    if (target.closest("[data-theme-toggle]")) {
      var current = root.getAttribute("data-theme");
      var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      var effective = current || (prefersDark ? "dark" : "light");
      applyTheme(effective === "dark" ? "light" : "dark");
      return;
    }

    var tab = target.closest("[data-tab]");
    if (tab) {
      var name = tab.getAttribute("data-tab");
      document.querySelectorAll("[data-tab]").forEach(function (node) {
        node.setAttribute("aria-selected", String(node === tab));
      });
      document.querySelectorAll("[data-panel]").forEach(function (panel) {
        panel.setAttribute("data-active", String(panel.getAttribute("data-panel") === name));
      });
      if (history.replaceState) { history.replaceState(null, "", "#" + name); }
      return;
    }

    var header = target.closest("th[data-sortable='true']");
    if (header) {
      var table = header.closest("table");
      var index = Array.prototype.indexOf.call(header.parentNode.children, header);
      var ascending = header.getAttribute("data-order") !== "asc";
      header.parentNode.querySelectorAll("th").forEach(function (node) { node.removeAttribute("data-order"); });
      header.setAttribute("data-order", ascending ? "asc" : "desc");
      var rows = Array.prototype.slice.call(table.tBodies[0].rows);
      rows.sort(function (a, b) {
        var left = a.cells[index] ? a.cells[index].getAttribute("data-value") || a.cells[index].textContent.trim() : "";
        var right = b.cells[index] ? b.cells[index].getAttribute("data-value") || b.cells[index].textContent.trim() : "";
        var leftNumber = parseFloat(left.replace(/[^0-9.\\-]/g, ""));
        var rightNumber = parseFloat(right.replace(/[^0-9.\\-]/g, ""));
        var bothNumeric = !isNaN(leftNumber) && !isNaN(rightNumber);
        var comparison = bothNumeric ? leftNumber - rightNumber : String(left).localeCompare(String(right));
        return ascending ? comparison : -comparison;
      });
      rows.forEach(function (row) { table.tBodies[0].appendChild(row); });
    }
  });

  var hash = (window.location.hash || "").replace("#", "");
  if (hash) {
    var initial = document.querySelector("[data-tab='" + hash + "']");
    if (initial) { initial.click(); }
  }
})();
"""
