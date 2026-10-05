"""Regression tests for the two packaging defects the container build exposed.

Both bugs were invisible in the development checkout - the templates are on disk
next to the source and the working directory happens to be the repository root -
but they break an installed wheel and therefore the Docker image:

1. ``templates/*.j2`` were not declared in ``[tool.setuptools.package-data]``, so a
   non-editable install shipped without the Jinja report templates and failed at
   the report step with ``TemplateNotFound``.
2. ``conf/trial_protocol_demo.yaml`` was resolved relative to the current working
   directory, so ``insilico-trial demo`` failed anywhere except the checkout root
   (the image runs with ``WORKDIR /data``).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "insilico_trial_mas"


def _package_data_patterns() -> list[str]:
    """Read the declared package-data globs without a TOML parser.

    ``tomllib`` only exists from Python 3.11 and the project supports 3.10, so the
    section is extracted textually - the assertion is about the declared content,
    not about parsing the whole file.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.setuptools.package-data]", 1)
    assert len(section) == 2, "pyproject.toml must declare [tool.setuptools.package-data]"
    match = re.search(r"insilico_trial_mas\s*=\s*\[(.*?)\]", section[1], re.S)
    assert match, f"package-data for the package is missing: {section[1][:120]!r}"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_every_packaged_asset_is_declared() -> None:
    """Every non-Python asset in the package must match a package-data pattern.

    A wheel is built in CI and used by the Docker image, so an undeclared asset is
    a runtime failure in production rather than a cosmetic packaging wart.
    """
    patterns = _package_data_patterns()
    assert "templates/*.j2" in patterns, "the Jinja report templates must be packaged"

    assets = [
        path.relative_to(PACKAGE_DIR).as_posix()
        for path in PACKAGE_DIR.rglob("*")
        if path.is_file()
        and path.suffix in {".j2", ".yaml", ".yml", ".json", ".md", ".typed", ".csv"}
        and "__pycache__" not in path.parts
    ]
    assert assets, "the package should ship at least the report templates"

    undeclared = [
        asset
        for asset in assets
        if not any(fnmatch.fnmatch(asset, pattern) for pattern in patterns)
    ]
    assert not undeclared, f"assets missing from [tool.setuptools.package-data]: {undeclared}"


def test_report_templates_exist_where_the_builder_expects_them() -> None:
    """The two templates referenced by the report builder must be present."""
    for template in ("report.md.j2", "report.html.j2"):
        assert (PACKAGE_DIR / "templates" / template).exists()


def test_asset_roots_include_the_container_layout() -> None:
    from insilico_trial_mas.config import CONTAINER_ASSET_ROOT, asset_roots

    roots = asset_roots()
    assert Path.cwd() in roots
    assert CONTAINER_ASSET_ROOT in roots, "the image keeps the profiles in /app/conf"
    assert REPO_ROOT in roots, "the repository root must be a candidate"
    assert len(roots) == len(set(roots)), "candidate roots must be unique"


def test_resolve_asset_finds_profiles_from_an_unrelated_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`conf/*.yaml` resolves even when the process runs somewhere else entirely."""
    from insilico_trial_mas.config import resolve_asset

    monkeypatch.chdir(tmp_path)
    resolved = resolve_asset("conf/trial_protocol_demo.yaml")
    assert resolved.exists(), f"unresolved: {resolved}"
    assert resolved.name == "trial_protocol_demo.yaml"

    # An absolute path is returned untouched, whether or not it exists.
    absolute = tmp_path / "nope.yaml"
    assert resolve_asset(absolute) == absolute

    # A genuinely wrong relative path still surfaces as "not found" to the caller.
    assert not resolve_asset("conf/does-not-exist.yaml").exists()


def test_load_config_accepts_a_repository_relative_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from insilico_trial_mas.config import load_config

    monkeypatch.chdir(tmp_path)
    config = load_config("conf/simulation_local.yaml")
    assert config.n_patients > 0
    assert config.storage.backend == "local"


def test_demo_runs_from_another_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end guard for defect 2: the demo must not depend on the CWD."""
    from insilico_trial_mas.cli import main

    monkeypatch.chdir(tmp_path)
    output = tmp_path / "demo-out"
    exit_code = main(["demo", "--patients", "40", "--epochs", "2", "--output-dir", str(output)])
    assert exit_code == 0
    runs = list(output.glob("RUN-*/report/dashboard.html"))
    assert runs, "the demo must produce a dashboard from any working directory"


def test_profile_protocol_path_resolves_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile's own protocol_path must survive a different working directory.

    `simulation_cluster.yaml` points at `conf/trial_protocol_demo.yaml`; the image
    starts the process in `/data`, so an unresolved path there is a hard failure.
    """
    from insilico_trial_mas.config import load_config
    from insilico_trial_mas.pipeline import load_protocol

    monkeypatch.chdir(tmp_path)
    for profile in ("simulation_cluster.yaml", "simulation_community_edition.yaml"):
        config = load_config(f"conf/{profile}")
        protocol = load_protocol(config.protocol_path)
        assert protocol.protocol_id, f"{profile} did not resolve its protocol"


def test_protocol_loader_resolves_the_bundled_demo_protocol() -> None:
    from insilico_trial_mas.config import resolve_asset
    from insilico_trial_mas.pipeline import load_protocol

    protocol = load_protocol(resolve_asset("conf/trial_protocol_demo.yaml"))
    assert protocol.arms, "the bundled protocol must define arms"


def test_model_persistence_falls_back_when_the_path_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Databricks-only `ml.model_path` must not kill a container run.

    `simulation_cluster.yaml` points the model at `/dbfs/...`, which does not exist
    outside a workspace. The artefact is a cache, so the run falls back to
    `output_dir/models` and says so.
    """
    import logging
    from pathlib import Path as _Path

    from insilico_trial_mas.config import load_config
    from insilico_trial_mas.logging_utils import get_logger
    from insilico_trial_mas.ml import training

    config = load_config("conf/simulation_local.yaml", {"output_dir": str(tmp_path / "out")})
    config.ml.model_path = "/dbfs/insilico-trial/models/physiology_model.json"

    saved: list[str] = []

    class _Head:
        version = "test"

        def save(self, path: str) -> _Path:
            if path.startswith("/dbfs"):
                raise PermissionError(13, "Permission denied", path)
            saved.append(path)
            target = _Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
            return target

    result = training.TrainingResult(head=_Head(), metrics={}, n_train=1, n_holdout=1)

    messages: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: messages.append(record.getMessage())  # type: ignore[method-assign]
    logger = get_logger("ml.training")
    logger.addHandler(handler)
    try:
        path = training.persist_model(result, config)
    finally:
        logger.removeHandler(handler)

    assert saved and saved[0].startswith(str(tmp_path / "out")), saved
    assert "dbfs" in path.as_posix() or path.as_posix().startswith(str(tmp_path / "out"))
    assert path.name == "physiology_model.json"
    assert any("falling back" in message for message in messages), messages
