#!/usr/bin/env python3
"""Launch the InSilicoTrial MAS Studio: a live simulation server for the browser.

Usage::

    python scripts/run_studio.py                                     # conf/simulation_local.yaml, port 8765
    python scripts/run_studio.py --config conf/simulation_local.yaml --port 9000
    python scripts/run_studio.py --host 0.0.0.0 --port 9000 --no-browser
    python scripts/run_studio.py --no-browser                        # headless or remote host

The Studio serves one self-contained page (no CDN, no build step) plus a small JSON
API. Runs execute the real :class:`~insilico_trial_mas.pipeline.TrialSimulationPipeline`
in background threads, and can be watched, re-opened and downloaded from
``http://<host>:<port>/``. Stop the server with Ctrl+C; the exit code is 0 on a
clean shutdown and 1 when the configuration cannot be loaded.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from insilico_trial_mas.ui.studio import DEFAULT_HOST, DEFAULT_PORT, run_studio


def build_parser() -> argparse.ArgumentParser:
    """Command line surface of the Studio launcher."""
    parser = argparse.ArgumentParser(
        prog="run_studio.py",
        description="Serve the InSilicoTrial MAS Studio (live simulation launcher and viewer)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The Studio is a decision-support demo: it never writes to MLflow or exports CDISC data.",
    )
    parser.add_argument(
        "--config",
        default="conf/simulation_local.yaml",
        help="simulation configuration profile (default: conf/simulation_local.yaml)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window on start")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: parse arguments, announce the URL and serve until interrupted."""
    args = build_parser().parse_args(argv)
    print(f"InSilicoTrial MAS Studio: http://{args.host}:{args.port}/  (config: {args.config})")
    print("Press Ctrl+C to stop.")
    return run_studio(args.config, host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
