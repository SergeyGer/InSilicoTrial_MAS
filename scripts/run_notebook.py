"""Execute the verification notebook's code cells outside Jupyter.

Databricks runs `notebooks/insilico_trial_demo.ipynb` interactively, but the code
cells must still be *correct*. This runner extracts them, executes them in one
namespace with a small offline profile and fails loudly on the first error, which
makes the notebook a CI artefact rather than documentation that rots.

Usage::

    python scripts/run_notebook.py                       # default profile, 300 patients
    python scripts/run_notebook.py --patients 1000 --profile conf/simulation_local.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_NOTEBOOK = REPO_ROOT / "notebooks" / "insilico_trial_demo.ipynb"


def load_code_cells(path: Path) -> list[str]:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    cells: list[str] = []
    for cell in notebook["cells"]:
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell["source"])
        # Skip magic/shell lines: the runner is not IPython.
        lines = [line for line in source.splitlines() if not line.lstrip().startswith(("%", "!"))]
        if lines:
            cells.append("\n".join(lines))
    return cells


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the verification notebook's code cells")
    parser.add_argument("--notebook", default=str(DEFAULT_NOTEBOOK))
    parser.add_argument("--profile", default="conf/simulation_local.yaml")
    parser.add_argument("--patients", type=int, default=300)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    notebook_path = Path(args.notebook)
    cells = load_code_cells(notebook_path)
    print(f"executing {len(cells)} code cells from {notebook_path.name}")

    # The notebook reads its configuration from Databricks widgets; outside
    # Databricks the widget helper falls back to these defaults.
    namespace: dict[str, Any] = {
        "__name__": "__notebook__",
        "widget_defaults": {"config": args.profile, "n_patients": str(args.patients)},
    }

    failures = 0
    for index, source in enumerate(cells, start=1):
        if "def widget(" in source:
            # Replace the widget default lookup with the runner's values.
            source = source.replace(
                'PROFILE = widget("config", "conf/simulation_community_edition.yaml")',
                'PROFILE = widget("config", widget_defaults["config"])',
            ).replace(
                'N_PATIENTS = int(widget("n_patients", "10000"))',
                'N_PATIENTS = int(widget("n_patients", widget_defaults["n_patients"]))',
            )
            source = source.replace(
                '        return default\n',
                '        return widget_defaults.get(name, default)\n',
            )
        source = source.replace(
            'config = load_config(PROFILE, {"n_patients": N_PATIENTS})',
            f'config = load_config(PROFILE, {{"n_patients": N_PATIENTS, "epochs": {args.epochs}}})'
        )
        try:
            compiled = compile(source, f"<notebook cell {index}>", "exec")
        except SyntaxError:
            print(f"[cell {index}] SYNTAX ERROR")
            traceback.print_exc()
            failures += 1
            continue
        try:
            exec(compiled, namespace)
        except Exception:
            print(f"[cell {index}] FAILED")
            traceback.print_exc()
            failures += 1
            continue
        if not args.quiet:
            print(f"[cell {index}] ok")

    print(f"\ncells executed: {len(cells)}, failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
