"""Data-lineage tracking.

Unity Catalog captures lineage automatically for table-to-table flows, but not
for the agent-to-agent flow that actually produced a row (which persona, which
prompt, which model version). The platform therefore records its own lineage
graph and writes it to ``gold.lineage_edges``, which is what the notebook's
"Visualizing Data Lineage" cell renders.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

LINEAGE_COLUMNS = ("source", "target", "relation", "agent", "run_id", "details_json", "created_at")


@dataclass(slots=True)
class LineageNode:
    """A node of the lineage graph (table, artifact, agent or model)."""

    node_id: str
    kind: str  # table | artifact | agent | model | report
    label: str = ""
    location: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LineageEdge:
    """A directed edge: ``source`` produced/consumed ``target``."""

    source: str
    target: str
    relation: str = "derives_from"
    agent: str = ""
    run_id: str = ""
    created_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_row(self, created_at: str = "") -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "agent": self.agent,
            "run_id": self.run_id,
            "details_json": json.dumps(self.details, default=str),
            "created_at": created_at or self.created_at,
        }


class LineageTracker:
    """Collects nodes and edges during a run; renders tables and Mermaid diagrams."""

    def __init__(self, run_id: str, *, created_at: str = "") -> None:
        self.run_id = run_id
        self.created_at = created_at
        self.nodes: dict[str, LineageNode] = {}
        self.edges: list[LineageEdge] = []

    # -- recording ---------------------------------------------------------
    def add_node(self, node_id: str, kind: str, **metadata: Any) -> LineageNode:
        node = self.nodes.get(node_id)
        if node is None:
            node = LineageNode(node_id=node_id, kind=kind, label=metadata.pop("label", node_id), metadata=metadata)
            self.nodes[node_id] = node
        else:
            node.metadata.update(metadata)
        return node

    def add_edge(
        self,
        source: str,
        target: str,
        *,
        relation: str = "derives_from",
        agent: str = "",
        **details: Any,
    ) -> LineageEdge:
        edge = LineageEdge(
            source=source,
            target=target,
            relation=relation,
            agent=agent,
            run_id=self.run_id,
            created_at=self.created_at,
            details=details,
        )
        self.edges.append(edge)
        return edge

    # -- rendering ---------------------------------------------------------
    def to_frame(self) -> pd.DataFrame:
        rows = [edge.as_row(self.created_at) for edge in self.edges]
        return pd.DataFrame(rows, columns=list(LINEAGE_COLUMNS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "nodes": [asdict(node) for node in self.nodes.values()],
            "edges": [asdict(edge) for edge in self.edges],
        }

    def upstream(self, node_id: str) -> list[str]:
        """All ancestors of ``node_id`` (breadth-first, cycle safe)."""
        incoming: dict[str, list[str]] = {}
        for edge in self.edges:
            incoming.setdefault(edge.target, []).append(edge.source)
        seen: set[str] = set()
        queue = list(incoming.get(node_id, []))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            queue.extend(incoming.get(current, []))
        return sorted(seen)

    def downstream(self, node_id: str) -> list[str]:
        outgoing: dict[str, list[str]] = {}
        for edge in self.edges:
            outgoing.setdefault(edge.source, []).append(edge.target)
        seen: set[str] = set()
        queue = list(outgoing.get(node_id, []))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            queue.extend(outgoing.get(current, []))
        return sorted(seen)

    def to_mermaid(self, *, max_edges: int = 200) -> str:
        """Mermaid ``flowchart`` source (renders in Databricks markdown and GitHub)."""
        lines = ["flowchart LR"]
        for node_id, node in list(self.nodes.items())[:max_edges]:
            safe = node_id.replace(".", "_").replace("/", "_").replace("-", "_").replace(":", "_")
            shape = {"table": "[", "artifact": "(", "agent": "{{", "model": "[/", "report": ">"}.get(node.kind, "[")
            closing = {"table": "]", "artifact": ")", "agent": "}}", "model": "/]", "report": "]"}.get(node.kind, "]")
            lines.append(f'    {safe}{shape}"{node.label or node_id}"{closing}')
        for edge in self.edges[:max_edges]:
            source = edge.source.replace(".", "_").replace("/", "_").replace("-", "_").replace(":", "_")
            target = edge.target.replace(".", "_").replace("/", "_").replace("-", "_").replace(":", "_")
            lines.append(f"    {source} -->|{edge.relation}| {target}")
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "kinds": {kind: sum(1 for n in self.nodes.values() if n.kind == kind) for kind in {n.kind for n in self.nodes.values()}},
            "agents": sorted({edge.agent for edge in self.edges if edge.agent}),
        }
