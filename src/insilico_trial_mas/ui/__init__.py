"""Dashboard and Studio user interface for InSilicoTrial MAS.

Design constraints that shaped this package:

* **No build step.** The dashboard is a single self-contained HTML file with inline
  SVG charts and a few kilobytes of vanilla JavaScript. It can be e-mailed, opened
  from a Delta volume, attached to a ticket, or displayed inside a Databricks
  notebook with ``displayHTML`` - none of which is true for a React/Streamlit app.
* **No CDN.** Regulated environments are frequently air-gapped, and a demo that
  breaks without internet is not a demo.
* **No extra dependencies.** Charts are rendered server-side as SVG from the same
  pandas frames the report already uses, so the UI adds zero packages to the
  runtime that Databricks must install.

:mod:`~insilico_trial_mas.ui.charts`  - dependency-free SVG chart primitives.
:mod:`~insilico_trial_mas.ui.theme`   - the design system (CSS custom properties).
:mod:`~insilico_trial_mas.ui.data`    - run directory -> dashboard payload.
:mod:`~insilico_trial_mas.ui.dashboard` - HTML assembly and CLI entry point.
:mod:`~insilico_trial_mas.ui.studio`  - optional live launcher served by ``http.server``.
"""

from __future__ import annotations

__all__ = ["charts", "dashboard", "data", "theme"]
