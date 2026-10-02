"""Report generation: Markdown, HTML and a JSON contract for downstream tooling.

The report is the Biostatistician Agent's deliverable. It always contains the
same blocks (disposition, demographics, efficacy with confidence intervals,
dose-response, safety by CTCAE grade, DSMB stopping rules, telemetry, data
quality, lineage and an explicit limitations/disclaimer section), so a reviewer
can diff two runs and see only what actually changed.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, TemplateNotFound, select_autoescape

from ..agents.biostatistician_agent import AnalysisResult
from ..errors import ConfigurationError
from ..logging_utils import get_logger
from ..schemas import SimulationProvenance, TrialProtocol

logger = get_logger("reporting.report")

TEMPLATE_PACKAGE = "insilico_trial_mas.templates"
MARKDOWN_TEMPLATE = "report.md.j2"
HTML_TEMPLATE = "report.html.j2"

DISCLAIMER = (
    "This document was produced by InSilicoTrial MAS from fully synthetic patient data. "
    "It is a decision-support artefact for trial design and must not be used as clinical evidence, "
    "as a regulatory submission, or as medical advice. Population priors, drug parameters and "
    "adverse-event models are illustrative and require validation against licensed reference data "
    "and real pharmacokinetic studies before any regulatory use."
)


@dataclass(slots=True)
class ReportArtifacts:
    """Paths of the generated artefacts."""

    markdown: str = ""
    html: str = ""
    payload: str = ""
    formats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"markdown": self.markdown, "html": self.html, "payload": self.payload, "formats": self.formats}


def _format_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int,)):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return "n/a"
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    return f"{number:.{digits}f}"


def _format_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return "n/a"
    return f"{number * 100:.{digits}f}%"


def _format_p(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return "n/a"
    if number < 0.0001:
        return "<0.0001"
    return f"{number:.4f}"


def build_environment() -> Environment:
    """Jinja environment loading templates from the installed package."""
    env = Environment(
        loader=_PackageLoader(),
        autoescape=select_autoescape(enabled_extensions=("html",), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["num"] = _format_number
    env.filters["pct"] = _format_pct
    env.filters["pval"] = _format_p
    return env


class _PackageLoader(BaseLoader):
    """Load templates from ``insilico_trial_mas/templates`` via importlib.resources."""

    def get_source(self, environment: Environment, template: str) -> tuple[str, str, Any]:
        try:
            source = resources.files(TEMPLATE_PACKAGE).joinpath(template).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise TemplateNotFound(template) from exc
        return source, template, lambda: False


class ReportBuilder:
    """Renders the Biostatistician Agent's analysis into reviewable artefacts."""

    def __init__(
        self,
        protocol: TrialProtocol,
        provenance: SimulationProvenance,
        analysis: AnalysisResult,
        *,
        lineage: dict[str, Any] | None = None,
        lineage_mermaid: str = "",
        traces: dict[str, Any] | None = None,
        engine_info: dict[str, Any] | None = None,
        protocol_warnings: list[str] | None = None,
    ) -> None:
        self.protocol = protocol
        self.provenance = provenance
        self.analysis = analysis
        self.lineage = lineage or {}
        self.lineage_mermaid = lineage_mermaid
        self.traces = traces or {}
        self.engine_info = engine_info or {}
        self.protocol_warnings = protocol_warnings or []
        self.environment = build_environment()

    # -- public API --------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        """The complete, JSON-serialisable report payload."""
        return {
            "schema_version": "1.0.0",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "disclaimer": DISCLAIMER,
            "protocol": self.protocol.model_dump(mode="json"),
            "provenance": self.provenance.as_dict(),
            "analysis": self.analysis.as_dict(),
            "lineage": self.lineage,
            "lineage_mermaid": self.lineage_mermaid,
            "traces": self.traces,
            "engine": self.engine_info,
            "protocol_warnings": self.protocol_warnings,
        }

    def render(self, fmt: str) -> str:
        if fmt == "markdown":
            template = self.environment.get_template(MARKDOWN_TEMPLATE)
        elif fmt == "html":
            template = self.environment.get_template(HTML_TEMPLATE)
        elif fmt == "json":
            return json.dumps(self.payload(), indent=2, default=str)
        else:
            raise ConfigurationError(f"unsupported report format {fmt!r}; expected markdown, html or json")
        return template.render(**self.payload())

    def write(self, output_dir: str | Path, formats: list[str] | None = None, *, stem: str = "trial_report") -> ReportArtifacts:
        """Render and persist the requested formats."""
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        formats = formats or ["markdown", "html", "json"]
        artifacts = ReportArtifacts(formats=list(formats))
        for fmt in formats:
            content = self.render(fmt)
            if fmt == "markdown":
                path = target / f"{stem}.md"
            elif fmt == "html":
                path = target / f"{stem}.html"
            else:
                path = target / f"{stem}.json"
            path.write_text(content, encoding="utf-8")
            setattr(artifacts, fmt if fmt != "json" else "payload", str(path))
            logger.info(f"wrote {fmt} report to {path}")
        return artifacts

    def headline_summary(self) -> dict[str, Any]:
        """One-screen readout used by the CLI and by MLflow metrics."""
        overview = dict(self.analysis.overview)
        overview["run_id"] = self.provenance.sim_run_id
        overview["engine"] = self.provenance.engine_backend
        overview["model_version"] = self.provenance.physiology_model_version
        overview["safety_alerts"] = len(self.analysis.safety_alerts)
        primary = [c for c in self.analysis.comparisons if c.endpoint == self.protocol.primary_endpoint.name]
        if primary:
            best = min(primary, key=lambda c: abs(c.p_value))
            overview["best_arm"] = best.arm_id
            overview["best_effect"] = round(best.effect_estimate, 3)
            overview["best_p_value"] = round(best.p_value, 6)
            overview["meets_mcid"] = bool(best.meets_mcid)
        return overview

    def render_text_summary(self) -> str:
        """Plain-text console summary (used as the final CLI output)."""
        lines = [
            f"Protocol {self.protocol.protocol_id} | run {self.provenance.sim_run_id}",
            f"Engine {self.provenance.engine_backend} | patients {self.provenance.n_patients} | "
            f"epochs {self.provenance.n_epochs} | rows {self.provenance.n_patients * (self.provenance.n_epochs + 1)}",
            "",
            f"{'arm':<18}{'n':>7}{'dSBP':>9}{'responders':>12}{'G3+':>8}{'SAE':>8}{'disc.':>7}",
        ]
        for summary in self.analysis.arm_summaries:
            lines.append(
                f"{summary.arm_id:<18}{summary.n_patients:>7}{summary.mean_sbp_change:>9.2f}"
                f"{summary.responder_rate * 100:>11.1f}%{summary.grade3_plus_rate * 100:>7.1f}%"
                f"{summary.sae_rate * 100:>7.1f}%{summary.discontinuations:>7}"
            )
        if self.analysis.comparisons:
            lines.append("")
            lines.append(f"{'endpoint':<26}{'arm':<16}{'effect':>9}{'95% CI':>20}{'p':>10}{'q':>8}")
            for comparison in self.analysis.comparisons:
                ci = f"[{comparison.ci_low:.2f}, {comparison.ci_high:.2f}]"
                q_value = f"{comparison.q_value:.3f}" if comparison.q_value is not None else "-"
                lines.append(
                    f"{comparison.endpoint:<26}{comparison.arm_id:<16}{comparison.effect_estimate:>9.2f}"
                    f"{ci:>20}{_format_p(comparison.p_value):>10}{q_value:>8}"
                )
        if self.analysis.safety_alerts:
            lines.append("")
            lines.append("Safety signals:")
            lines.extend(f"  - [{alert['severity']}] {alert['message']}" for alert in self.analysis.safety_alerts)
        triggered = [e for e in self.analysis.stopping_evaluations if e.triggered]
        if triggered:
            lines.append("")
            lines.append("Stopping rules triggered:")
            lines.extend(
                f"  - {e.rule_id} ({e.action}) arm={e.arm_id} epoch={e.epoch} "
                f"observed={e.observed:.3f} threshold={e.threshold:.3f}"
                for e in triggered[:10]
            )
        return "\n".join(lines)
