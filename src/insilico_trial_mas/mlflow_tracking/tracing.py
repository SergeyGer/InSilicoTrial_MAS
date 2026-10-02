"""LLM trace recording and analysis (the MLflow Tracing story).

Every persona LLM call produces a *span* with its parent chain::

    run
     └── epoch_simulation
          └── patient_agent
               └── llm_call (tokens, latency, cache hit)

Spans are appended to a JSONL file as they finish, which is what
:class:`TraceStore` reads back to answer the three questions the specification's
fifth notebook cell asks: prompt token distribution, network request intervals
and step-by-step trace hierarchies. When MLflow is available the same payloads
are logged to the run as an artifact, so they can also be browsed in the MLflow
UI without any additional code.
"""

from __future__ import annotations

import json
import statistics
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from ..logging_utils import get_logger
from ..reproducibility import stable_hash

logger = get_logger("tracking.tracing")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class Span:
    """One traced operation."""

    trace_id: str
    span_id: str
    parent_id: str
    name: str
    kind: str = "LLM"  # RUN | AGENT | LLM | TOOL | CHAIN
    start_time: str = ""
    end_time: str = ""
    duration_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit: bool = False
    provider: str = ""
    model: str = ""
    status: str = "OK"
    attributes: dict[str, Any] = field(default_factory=dict)


class TraceRecorder:
    """Append-only span recorder (thread safe, never raises)."""

    def __init__(self, path: str | Path, *, run_id: str, enabled: bool = True) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.enabled = enabled
        self._lock = threading.Lock()
        self._spans: list[Span] = []

    # -- recording ---------------------------------------------------------
    def record_span(self, span: Span) -> Span:
        if not self.enabled:
            return span
        with self._lock:
            self._spans.append(span)
            self._append(span)
        return span

    def record_llm_call(
        self,
        *,
        patient_id: str,
        epoch: int,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        duration_ms: float,
        cache_hit: bool = False,
        prompt_hash: str = "",
        error: str = "",
        parent_id: str = "",
    ) -> Span:
        """Record one persona LLM call as a child span of the patient agent."""
        trace_id = stable_hash(self.run_id, patient_id, length=24)
        agent_span_id = stable_hash(trace_id, "patient-agent", length=16)
        span = Span(
            trace_id=trace_id,
            span_id=stable_hash(trace_id, "llm", epoch, prompt_hash, length=16),
            parent_id=parent_id or agent_span_id,
            name=f"patient.persona.epoch[{epoch}]",
            kind="LLM",
            start_time=_now_iso(),
            duration_ms=round(duration_ms, 3),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_hit=cache_hit,
            provider=provider,
            model=model,
            status="ERROR" if error else "OK",
            attributes={
                "patient_id": patient_id,
                "epoch": epoch,
                "prompt_hash": prompt_hash,
                "error": error,
                "run_id": self.run_id,
            },
        )
        # The parent agent span is emitted once per (trace, agent) pair.
        with self._lock:
            known = {s.span_id for s in self._spans}
        if agent_span_id not in known:
            self.record_span(
                Span(
                    trace_id=trace_id,
                    span_id=agent_span_id,
                    parent_id="",
                    name=f"patient_agent[{patient_id}]",
                    kind="AGENT",
                    start_time=_now_iso(),
                    attributes={"patient_id": patient_id, "run_id": self.run_id},
                )
            )
        return self.record_span(span)

    # -- output ------------------------------------------------------------
    def spans(self) -> list[Span]:
        with self._lock:
            return list(self._spans)

    def summary(self) -> dict[str, Any]:
        """Token distribution, latency percentiles and span counts."""
        spans = [s for s in self.spans() if s.kind == "LLM"]
        if not spans:
            return {"spans": 0, "llm_calls": 0}
        tokens_in = [s.tokens_in for s in spans]
        tokens_out = [s.tokens_out for s in spans]
        durations = [s.duration_ms for s in spans if s.duration_ms > 0]
        return {
            "spans": len(self.spans()),
            "llm_calls": len(spans),
            "cache_hits": sum(1 for s in spans if s.cache_hit),
            "errors": sum(1 for s in spans if s.status == "ERROR"),
            "tokens_in_total": sum(tokens_in),
            "tokens_out_total": sum(tokens_out),
            "tokens_in_mean": round(statistics.fmean(tokens_in), 1) if tokens_in else 0.0,
            "tokens_in_p95": _percentile(tokens_in, 0.95),
            "tokens_out_mean": round(statistics.fmean(tokens_out), 1) if tokens_out else 0.0,
            "latency_ms_mean": round(statistics.fmean(durations), 2) if durations else 0.0,
            "latency_ms_p50": _percentile(durations, 0.50),
            "latency_ms_p95": _percentile(durations, 0.95),
            "latency_ms_max": round(max(durations), 2) if durations else 0.0,
            "providers": sorted({s.provider for s in spans}),
            "models": sorted({s.model for s in spans}),
        }

    def flush(self) -> Path | None:
        """Ensure everything is on disk (spans are already appended eagerly)."""
        return self.path if self.path.exists() else None

    # -- internals ---------------------------------------------------------
    def _append(self, span: Span) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(span), ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - filesystem failure
            logger.warning(f"could not write trace span to {self.path}: {exc}")


class TraceStore:
    """Read-side view over recorded spans (CLI ``traces`` command, notebook cell 5)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def spans(self) -> list[Span]:
        if not self.path.exists():
            return []
        spans: list[Span] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                spans.append(Span(**{k: v for k, v in payload.items() if k in Span.__slots__}))
        return spans

    EMPTY_SUMMARY: ClassVar[dict[str, Any]] = {
        "spans": 0,
        "llm_calls": 0,
        "traces": 0,
        "cache_hit_rate": 0.0,
        "error_rate": 0.0,
        "tokens_in_total": 0,
        "tokens_in_p50": 0.0,
        "tokens_in_p95": 0.0,
        "latency_ms_p50": 0.0,
        "latency_ms_p95": 0.0,
    }

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Aggregate trace statistics.

        The key set is always complete so report templates can read any field
        without an ``UndefinedError`` when a run produced no persona calls.
        """
        spans = [s for s in self.spans() if run_id is None or s.attributes.get("run_id") == run_id]
        llm = [s for s in spans if s.kind == "LLM"]
        if not llm:
            return {**self.EMPTY_SUMMARY, "spans": len(spans), "traces": len({s.trace_id for s in spans})}
        tokens_in = [s.tokens_in for s in llm]
        durations = [s.duration_ms for s in llm]
        return {
            "spans": len(spans),
            "llm_calls": len(llm),
            "traces": len({s.trace_id for s in spans}),
            "cache_hit_rate": round(sum(1 for s in llm if s.cache_hit) / len(llm), 4),
            "error_rate": round(sum(1 for s in llm if s.status == "ERROR") / len(llm), 4),
            "tokens_in_total": sum(tokens_in),
            "tokens_in_p50": _percentile(tokens_in, 0.5),
            "tokens_in_p95": _percentile(tokens_in, 0.95),
            "latency_ms_p50": _percentile(durations, 0.5),
            "latency_ms_p95": _percentile(durations, 0.95),
        }

    def tree(self, trace_id: str) -> dict[str, Any]:
        """Rebuild the span hierarchy of one trace."""
        spans = [s for s in self.spans() if s.trace_id == trace_id]
        by_id = {s.span_id: s for s in spans}
        children: dict[str, list[str]] = {}
        roots: list[str] = []
        for span in spans:
            if span.parent_id and span.parent_id in by_id:
                children.setdefault(span.parent_id, []).append(span.span_id)
            else:
                roots.append(span.span_id)

        def build(span_id: str) -> dict[str, Any]:
            span = by_id[span_id]
            return {
                "name": span.name,
                "kind": span.kind,
                "duration_ms": span.duration_ms,
                "tokens_in": span.tokens_in,
                "tokens_out": span.tokens_out,
                "status": span.status,
                "children": [build(child) for child in children.get(span_id, [])],
            }

        trees = [build(root) for root in roots]
        return {"trace_id": trace_id, "roots": trees}

    def trace_ids(self, *, limit: int = 10) -> list[str]:
        seen: list[str] = []
        for span in self.spans():
            if span.trace_id not in seen:
                seen.append(span.trace_id)
            if len(seen) >= limit:
                break
        return seen

    def as_rows(self, *, run_id: str | None = None) -> Iterable[dict[str, Any]]:
        for span in self.spans():
            if run_id and span.attributes.get("run_id") != run_id:
                continue
            row = asdict(span)
            row["attributes_json"] = json.dumps(row.pop("attributes"), default=str)
            yield row


def _percentile(values: Sequence[float], q: float) -> float:
    """Small percentile helper (nearest-rank) - avoids a NumPy import at call sites."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(float(ordered[index]), 3)
