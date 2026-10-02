"""Persistent response cache for LLM calls.

Reproducibility is the reason this exists. An LLM is not deterministic even at
``temperature=0`` (provider-side batching, model updates), so a regulated
in-silico trial must be able to *replay* the exact text it used. The cache:

* keys on a stable digest of provider + model + temperature + prompt,
* is append-only JSONL (crash-safe, greppable, diffable, artifact-friendly),
* is safe to share across threads inside one executor (one lock, atomic appends),
* includes a per-partition in-memory tier so a 10,000-row batch reads the file
  only once.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..reproducibility import stable_hash

logger = get_logger("llm.cache")


def cache_key(
    *,
    provider: str,
    model: str,
    temperature: float,
    prompt: str,
    system: str = "",
    extra: str = "",
) -> str:
    """Stable cache key for one logical request."""
    return stable_hash(provider, model, f"{temperature:.4f}", system, prompt, extra, length=32)


@dataclass(slots=True)
class CacheEntry:
    key: str
    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: str = ""
    metadata: dict[str, Any] | None = None


class LLMResponseCache:
    """Append-only JSONL cache with an in-memory tier."""

    def __init__(self, path: str | Path | None, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled and path)
        self.path = Path(path) if path else None
        self._memory: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._loaded = False
        self.hits = 0
        self.misses = 0

    # -- public API --------------------------------------------------------
    def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        with self._lock:
            self._ensure_loaded()
            entry = self._memory.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            return entry

    def put(self, entry: CacheEntry) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._ensure_loaded()
            if entry.key in self._memory:
                return
            self._memory[entry.key] = entry
            self._append(entry)

    @property
    def size(self) -> int:
        with self._lock:
            self._ensure_loaded()
            return len(self._memory)

    def stats(self) -> dict[str, int]:
        return {"entries": self.size, "hits": self.hits, "misses": self.misses}

    # -- internals ---------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """Load the JSONL file once per process (lazily, under the lock)."""
        if self._loaded or not self.path or not self.path.exists():
            self._loaded = True
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate a partially written last line
                    entry = CacheEntry(
                        key=payload["key"],
                        text=payload.get("text", ""),
                        provider=payload.get("provider", ""),
                        model=payload.get("model", ""),
                        tokens_in=int(payload.get("tokens_in", 0)),
                        tokens_out=int(payload.get("tokens_out", 0)),
                        created_at=payload.get("created_at", ""),
                        metadata=payload.get("metadata"),
                    )
                    self._memory[entry.key] = entry
            logger.info(f"loaded {len(self._memory)} cached LLM responses from {self.path}")
        except OSError as exc:  # pragma: no cover - filesystem failure
            logger.warning(f"could not read LLM cache {self.path}: {exc}")
        self._loaded = True

    def _append(self, entry: CacheEntry) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "key": entry.key,
                "text": entry.text,
                "provider": entry.provider,
                "model": entry.model,
                "tokens_in": entry.tokens_in,
                "tokens_out": entry.tokens_out,
                "created_at": entry.created_at,
                "metadata": entry.metadata or {},
            }
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + os.linesep
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:  # pragma: no cover - filesystem failure
            logger.warning(f"could not append to LLM cache {self.path}: {exc}")
