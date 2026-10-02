"""Deterministic, order-independent random number generation.

Reproducibility is a regulatory requirement for in-silico trials and a practical
requirement for this project: the sequential engine, the local multiprocessing
engine and the Spark engine must produce *identical* numbers. That is only true
if no agent ever consumes a shared, order-dependent RNG stream. Every stochastic
draw is therefore derived from a stable hash of explicit parts
(``sim_run_seed``, ``patient_id``, ``epoch``, ``purpose``).
"""

from __future__ import annotations

import hashlib

import numpy as np


def stable_seed(*parts: object, bits: int = 63) -> int:
    """Derive a deterministic non-negative integer seed from arbitrary parts."""
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % (2**bits)


def rng_for(*parts: object, bits: int = 63) -> np.random.Generator:
    """Return a NumPy generator seeded deterministically by ``parts``."""
    return np.random.default_rng(stable_seed(*parts, bits=bits))


def stable_hash(*parts: object, length: int = 16) -> str:
    """Short stable hex digest - used for ids, prompt hashes and cache keys."""
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]
