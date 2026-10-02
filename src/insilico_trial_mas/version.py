"""Single source of truth for the package version and provenance fields."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

__version__ = "1.0.0"

#: Schema version of the Silver ``patient_states`` contract. Bump it whenever a
#: column is added/renamed so that Delta time travel reads remain interpretable.
SILVER_SCHEMA_VERSION = "1.0.0"

#: Version of the protocol JSON contract accepted by the Protocol Agent.
PROTOCOL_SCHEMA_VERSION = "1.0.0"


@lru_cache(maxsize=1)
def git_revision() -> str:
    """Return the current git revision (or ``"unknown"`` outside a checkout).

    The value is embedded in every simulation run manifest: in a regulated
    environment an audit trail is worthless without the exact code revision.
    """
    for key in ("INSILICO_GIT_SHA", "GIT_COMMIT", "DATABRICKS_GIT_COMMIT"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return "unknown"
