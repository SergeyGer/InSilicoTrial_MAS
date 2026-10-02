"""Generate ``docs/architecture.svg`` - the system diagram used in the README.

The diagram is generated rather than hand-drawn so the geometry stays consistent
when the architecture changes: every band, card and arrow is placed by a small
helper API instead of by hand-tuned coordinates. The output is a plain SVG with
no filters and no external fonts, which is what GitHub's image sanitiser renders
reliably.

Usage::

    python scripts/render_architecture.py            # writes docs/architecture.svg
    python scripts/render_architecture.py --check    # fail if the file is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "docs" / "architecture.svg"

WIDTH, HEIGHT = 1480, 1010

# Palette mirrors src/insilico_trial_mas/ui/theme.py so the diagram, the report
# and the dashboard read as one product.
INK = "#101c28"
MUTED = "#5a6b7b"
FAINT = "#8798a8"
LINE = "#d7dee6"
SURFACE = "#ffffff"
CANVAS = "#f6f8fa"
ACCENT = "#0b6e99"
ACCENT_SOFT = "#e3f0f7"
VIOLET = "#7a5ea8"
VIOLET_SOFT = "#efeaf7"
GREEN = "#1c7c54"
GREEN_SOFT = "#e4f3ec"
AMBER = "#a2701a"
AMBER_SOFT = "#fbf1dc"
CRITICAL = "#a8321f"

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

_parts: list[str] = []


def add(markup: str) -> None:
    _parts.append(markup)


def band(x: float, y: float, w: float, h: float, label: str, *, hint: str = "") -> None:
    """A labelled layer band."""
    add(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{SURFACE}" '
        f'stroke="{LINE}" stroke-width="1"/>'
    )
    add(
        f'<text x="{x + 18}" y="{y + 26}" font-family="{FONT}" font-size="12" font-weight="700" '
        f'fill="{ACCENT}" letter-spacing="1.4">{escape(label.upper())}</text>'
    )
    if hint:
        add(
            f'<text x="{x + 18 + len(label) * 9.2}" y="{y + 26}" font-family="{FONT}" font-size="12" '
            f'fill="{FAINT}">{escape(hint)}</text>'
        )


def card(
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    *,
    subtitle: str = "",
    lines: tuple[str, ...] = (),
    accent: str = ACCENT,
    title_size: float = 14.5,
    mono_subtitle: bool = False,
) -> None:
    """A component card: accent spine, title, optional subtitle and detail lines."""
    add(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{SURFACE}" '
        f'stroke="{LINE}" stroke-width="1"/>'
    )
    add(f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}"/>')
    add(
        f'<text x="{x + 16}" y="{y + 24}" font-family="{FONT}" font-size="{title_size}" '
        f'font-weight="650" fill="{INK}">{escape(title)}</text>'
    )
    cursor = y + 42
    if subtitle:
        family = MONO if mono_subtitle else FONT
        add(
            f'<text x="{x + 16}" y="{cursor}" font-family="{family}" font-size="11.5" '
            f'fill="{accent}">{escape(subtitle)}</text>'
        )
        cursor += 17
    for line in lines:
        add(
            f'<text x="{x + 16}" y="{cursor}" font-family="{FONT}" font-size="11.5" '
            f'fill="{MUTED}">{escape(line)}</text>'
        )
        cursor += 15


def chip(x: float, y: float, w: float, text: str, *, fill: str = ACCENT_SOFT, ink: str = ACCENT, size: float = 11) -> None:
    add(f'<rect x="{x}" y="{y}" width="{w}" height="22" rx="11" fill="{fill}"/>')
    add(
        f'<text x="{x + w / 2}" y="{y + 15}" font-family="{FONT}" font-size="{size}" font-weight="600" '
        f'fill="{ink}" text-anchor="middle">{escape(text)}</text>'
    )


def arrow(x1: float, y1: float, x2: float, y2: float, *, label: str = "", dashed: bool = False, colour: str = FAINT) -> None:
    """A vertical or horizontal connector with an optional centred label."""
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    add(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" stroke-width="1.6" '
        f'marker-end="url(#arrowhead)"{dash}/>'
    )
    if label:
        if abs(x2 - x1) < 1:  # vertical
            add(
                f'<text x="{x1 + 8}" y="{(y1 + y2) / 2 + 4}" font-family="{FONT}" font-size="10.5" '
                f'fill="{FAINT}">{escape(label)}</text>'
            )
        else:  # horizontal
            add(
                f'<text x="{(x1 + x2) / 2}" y="{y1 - 6}" font-family="{FONT}" font-size="10.5" '
                f'fill="{FAINT}" text-anchor="middle">{escape(label)}</text>'
            )


def elbow(x1: float, y1: float, x2: float, y2: float, *, label: str = "", colour: str = FAINT) -> None:
    """Right-angle connector: horizontal, then vertical, then horizontal."""
    midx = (x1 + x2) / 2
    add(
        f'<path d="M {x1} {y1} H {midx} V {y2} H {x2}" fill="none" stroke="{colour}" '
        f'stroke-width="1.6" marker-end="url(#arrowhead)"/>'
    )
    if label:
        add(
            f'<text x="{midx + 6}" y="{(y1 + y2) / 2}" font-family="{FONT}" font-size="10.5" '
            f'fill="{FAINT}">{escape(label)}</text>'
        )


def text(x: float, y: float, value: str, *, size: float = 12, weight: str = "400", fill: str = INK,
         anchor: str = "start", family: str = FONT, letter_spacing: float = 0) -> None:
    spacing = f' letter-spacing="{letter_spacing}"' if letter_spacing else ""
    add(
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}"{spacing}>{escape(value)}</text>'
    )


def build() -> str:
    _parts.clear()
    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" '
        f'height="{HEIGHT}" role="img" font-family="{FONT}">'
    )
    add(
        "<title>InSilicoTrial MAS architecture</title>"
        "<desc>Layered architecture: entry points, pipeline orchestration, three agent types, "
        "three interchangeable execution engines, medallion storage on Delta Lake, and the "
        "Databricks/AWS platform services that host them.</desc>"
    )
    add(
        "<defs><marker id=\"arrowhead\" viewBox=\"0 0 10 10\" refX=\"9\" refY=\"5\" markerWidth=\"7\" "
        "markerHeight=\"7\" orient=\"auto-start-reverse\">"
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{FAINT}"/></marker></defs>'
    )
    add(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{CANVAS}"/>')

    # ---------------------------------------------------------------- header
    text(40, 46, "InSilicoTrial MAS", size=23, weight="700")
    text(292, 46, "system architecture", size=16, fill=MUTED)
    text(
        40,
        70,
        "Multi-agent in-silico clinical trial simulation: synthetic patient personas, distributed execution, "
        "auditable Delta storage, biostatistical readout.",
        size=12.5,
        fill=MUTED,
    )
    chip(1130, 30, 150, "Databricks  ·  AWS", fill=ACCENT_SOFT, ink=ACCENT)
    chip(1292, 30, 148, "206 tests  ·  MIT", fill=GREEN_SOFT, ink=GREEN)

    # ---------------------------------------------------------------- bands
    band(40, 92, 1010, 108, "1  Entry points")
    band(40, 216, 1010, 92, "2  Orchestration (driver)")
    band(40, 324, 1010, 232, "3  Multi-agent system")
    band(40, 576, 1010, 116, "4  Execution engines")
    band(40, 708, 1010, 260, "5  Medallion storage (Delta Lake)")
    band(1076, 92, 364, 876, "Platform services")

    # ------------------------------------------------------- 1 entry points
    card(60, 128, 226, 58, "CLI  insilico-trial", subtitle="simulate · report · dashboard", lines=(), accent=ACCENT)
    card(300, 128, 226, 58, "Databricks Job", subtitle="bundle · wheel · schedule", lines=(), accent=ACCENT)
    card(540, 128, 226, 58, "Notebooks", subtitle="insilico_trial_demo.ipynb", lines=(), accent=ACCENT)
    card(780, 128, 250, 58, "Studio UI + dashboard", subtitle="live run · static readout", lines=(), accent=ACCENT)

    # ------------------------------------------------------ 2 orchestration
    card(
        60,
        250,
        970,
        44,
        "pipeline.py",
        subtitle="protocol validation  →  screening & randomisation  →  simulation  →  analysis  →  Gold tables  →  report, dashboard, manifest",
        accent=VIOLET,
        title_size=14,
    )
    arrow(545, 200, 545, 246, label="")

    # ---------------------------------------------------------- 3 agents
    card(
        60,
        362,
        268,
        176,
        "Protocol Agent",
        subtitle="agents/protocol_agent.py",
        lines=(
            "· eligibility screening + CONSORT log",
            "· stratified permuted-block",
            "  randomisation (deterministic)",
            "· per-epoch dose directives",
            "· titration and protocol deviations",
        ),
        accent=VIOLET,
    )
    card(
        344,
        362,
        402,
        176,
        "Patient Persona Agent  x10,000",
        subtitle="agents/patient_agent.py  ·  one digital twin per patient",
        lines=(
            "PK/PD   one-compartment oral, first-order absorption,",
            "            repeated-dose superposition  (ml/pk_pd.py)",
            "ML      mechanistic + ridge residual, individual",
            "            sensitivity, measurement noise  (ml/physiology.py)",
            "LLM     persona narration with validated JSON contract,",
            "            cache, rate limit, retry, offline fallback",
            "        → one Silver row per patient-epoch",
        ),
        accent=ACCENT,
    )
    card(
        762,
        362,
        268,
        176,
        "Biostatistician Agent",
        subtitle="agents/biostatistician_agent.py",
        lines=(
            "· per-arm summaries with Wilson CIs",
            "· Welch / bootstrap / Newcombe /",
            "  Fisher / Benjamini-Hochberg",
            "· dose-response and ALT signals",
            "· DSMB stopping rules per epoch",
            "· report, CDISC export, MLflow metrics",
        ),
        accent=GREEN,
    )
    arrow(328, 450, 344, 450)
    arrow(746, 450, 762, 450)

    # --------------------------------------------------------- 4 engines
    card(60, 610, 300, 66, "SequentialEngine", subtitle="reference implementation · tests", lines=(), accent=AMBER)
    card(380, 610, 300, 66, "LocalEngine", subtitle="process pool + asyncio per worker", lines=(), accent=AMBER)
    card(700, 610, 330, 66, "SparkEngine", subtitle="applyInPandas | mapInPandas", lines=(), accent=AMBER)
    arrow(545, 556, 545, 606, label="one partition = one batch of patients; identical agent code")
    chip(
        236,
        686,
        758,
        "Verified: the Spark engine reproduces the sequential engine bit-for-bit  (tests/test_engines.py)",
        fill=GREEN_SOFT,
        ink=GREEN,
        size=11.5,
    )

    # --------------------------------------------------------- 5 storage
    card(
        60,
        744,
        300,
        206,
        "bronze",
        subtitle="raw ingestion",
        lines=(
            "synthetic_cohort",
            "protocol_definitions",
            "llm_raw_traces",
            "",
            "append-only, replayable",
        ),
        accent=AMBER,
    )
    card(
        380,
        744,
        300,
        206,
        "silver",
        subtitle="per-epoch simulation log",
        lines=(
            "patient_states   (50 columns)",
            "adverse_events",
            "protocol_deviations",
            "screen_failures",
            "",
            "Delta time travel:",
            "DESCRIBE HISTORY /",
            "VERSION AS OF n",
        ),
        accent=ACCENT,
    )
    card(
        700,
        744,
        330,
        206,
        "gold",
        subtitle="decision-ready aggregates",
        lines=(
            "arm_summaries",
            "endpoint_comparisons",
            "safety_summary",
            "stopping_rule_evaluations",
            "run_manifest   (full provenance)",
            "lineage_edges",
        ),
        accent=GREEN,
    )
    arrow(360, 846, 380, 846)
    arrow(680, 846, 700, 846)
    arrow(545, 708, 545, 740, label="engine writes observations")

    # ------------------------------------------------- 6 platform services
    card(
        1096,
        128,
        324,
        186,
        "Unity Catalog",
        subtitle="governance · lineage · grants",
        lines=(
            "trial_simulations_prod.{bronze,silver,gold}",
            "one service principal per agent role",
            "external volumes for genomic data",
            "automatic table-level lineage",
        ),
        accent=VIOLET,
    )
    card(
        1096,
        330,
        324,
        186,
        "MLflow",
        subtitle="tracking · registry · tracing",
        lines=(
            "params, metrics, report artefacts",
            "physiology model versions + digests",
            "persona spans: tokens, latency, cache",
            "one experiment per environment",
        ),
        accent=ACCENT,
    )
    card(
        1096,
        532,
        324,
        186,
        "AWS",
        subtitle="compute · storage · identity",
        lines=(
            "EC2 worker pool (spot with fallback)",
            "S3 lakehouse + genomic reference",
            "IAM role, storage credential, KMS",
            "AWS Batch for genomic pre-processing",
        ),
        accent=AMBER,
    )
    card(
        1096,
        734,
        324,
        214,
        "Reproducibility",
        subtitle="what makes a readout auditable",
        lines=(
            "seed + protocol digest + model digest",
            "RNG per (run, patient, epoch, purpose)",
            "LLM responses cached and hashed",
            "wall-clock fields excluded by design",
            "run_manifest.json = replay recipe",
        ),
        accent=GREEN,
    )
    elbow(1050, 450, 1096, 220, label="")
    elbow(1030, 630, 1096, 626, label="")

    # ---------------------------------------------------------------- legend
    legend_y = 986
    text(40, legend_y, "Legend", size=11.5, weight="700", fill=MUTED)
    for index, (colour, label) in enumerate(
        ((VIOLET, "agent"), (ACCENT, "component"), (AMBER, "engine / raw layer"), (GREEN, "verification / curated layer"))
    ):
        x = 110 + index * 190
        add(f'<rect x="{x}" y="{legend_y - 9}" width="10" height="10" rx="2" fill="{colour}"/>')
        text(x + 18, legend_y, label, size=11, fill=MUTED)
    text(
        WIDTH - 40,
        legend_y,
        "Diagram source: scripts/render_architecture.py",
        size=10.5,
        fill=FAINT,
        anchor="end",
    )

    add("</svg>")
    return "\n".join(_parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render docs/architecture.svg")
    parser.add_argument("--check", action="store_true", help="fail if the committed file differs")
    args = parser.parse_args()

    svg = build()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != svg:
            print("docs/architecture.svg is stale - run scripts/render_architecture.py", file=sys.stderr)
            return 1
        print("docs/architecture.svg is up to date")
        return 0

    OUTPUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(svg) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
