"""Small helpers kept separate from the tracker to avoid import cycles."""

from __future__ import annotations


def tracking_available() -> bool:
    """True when the optional MLflow dependency is importable."""
    try:
        import mlflow  # noqa: F401 - availability probe
    except Exception:
        return False
    return True
