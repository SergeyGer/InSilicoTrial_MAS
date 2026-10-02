"""Unity Catalog namespace management (three-tier ``catalog.schema.table``).

The Terraform module in ``terraform/unity_catalog.tf`` provisions exactly the
objects this module names, so the code and the infrastructure cannot drift: both
read their names from the same registry below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import StorageConfig
from ..errors import GovernanceError

#: Medallion layers and the tables the platform owns.
LAYER_TABLES: dict[str, tuple[str, ...]] = {
    "bronze": ("synthetic_cohort", "protocol_definitions", "llm_raw_traces"),
    "silver": ("patient_states", "adverse_events", "protocol_deviations", "screen_failures"),
    "gold": ("arm_summaries", "endpoint_comparisons", "safety_summary", "stopping_rule_evaluations", "run_manifest", "lineage_edges"),
}


@dataclass(frozen=True, slots=True)
class TableRef:
    """A fully qualified Unity Catalog table reference."""

    catalog: str
    schema: str
    name: str

    @property
    def fqn(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"

    @property
    def layer(self) -> str:
        return self.schema

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.fqn


@dataclass(slots=True)
class UnityCatalogNamespace:
    """Resolves logical table names into Unity Catalog references."""

    catalog: str
    schemas: dict[str, str] = field(default_factory=dict)
    root_uri: str = ""

    @classmethod
    def from_storage_config(cls, config: StorageConfig) -> UnityCatalogNamespace:
        return cls(
            catalog=config.catalog,
            schemas={
                "bronze": config.bronze_schema,
                "silver": config.silver_schema,
                "gold": config.gold_schema,
            },
            root_uri=config.root_uri,
        )

    def schema_name(self, layer: str) -> str:
        try:
            return self.schemas[layer]
        except KeyError as exc:
            raise GovernanceError(f"unknown medallion layer {layer!r}; expected bronze/silver/gold") from exc

    def table(self, layer: str, name: str) -> TableRef:
        return TableRef(catalog=self.catalog, schema=self.schema_name(layer), name=name)

    def logical(self, layer: str, name: str) -> str:
        """Provider-agnostic ``layer/name`` key used by the storage layer."""
        return f"{self.schema_name(layer)}/{name}"

    def fqn(self, layer: str, name: str) -> str:
        return self.table(layer, name).fqn

    def external_path(self, layer: str, name: str) -> str:
        if not self.root_uri:
            raise GovernanceError("no storage root_uri configured; external paths are unavailable")
        return f"{self.root_uri.rstrip('/')}/{self.schema_name(layer)}/{name}"

    def all_tables(self) -> list[TableRef]:
        return [self.table(layer, name) for layer, names in LAYER_TABLES.items() for name in names]

    # -- DDL ---------------------------------------------------------------
    def ddl_statements(self) -> list[str]:
        """Idempotent DDL for the catalog, schemas and comment metadata."""
        statements = [
            f"CREATE CATALOG IF NOT EXISTS {self.catalog} COMMENT 'InSilicoTrial MAS clinical trial simulation catalog'",
        ]
        comments = {
            "bronze": "Raw ingestion: synthetic cohorts, protocol definitions and raw LLM traces",
            "silver": "Cleaned per-epoch patient states, adverse events and protocol deviations",
            "gold": "Biostatistician aggregates: arm summaries, endpoint comparisons and safety analytics",
        }
        for layer in ("bronze", "silver", "gold"):
            statements.append(
                f"CREATE SCHEMA IF NOT EXISTS {self.catalog}.{self.schema_name(layer)} COMMENT '{comments[layer]}'"
            )
        return statements

    def grants_summary(self) -> list[dict[str, Any]]:
        """Privilege model mirrored by ``terraform/unity_catalog.tf``."""
        return [
            {
                "principal": "patient_agent_service_principal",
                "schema": f"{self.catalog}.{self.schema_name('silver')}",
                "privileges": ["USE_SCHEMA", "SELECT", "MODIFY"],
                "purpose": "Patient Persona agents stream epoch observations into Silver",
            },
            {
                "principal": "protocol_agent_service_principal",
                "schema": f"{self.catalog}.{self.schema_name('bronze')}",
                "privileges": ["USE_SCHEMA", "SELECT", "MODIFY"],
                "purpose": "Protocol Agent registers cohorts, protocols and raw LLM traces",
            },
            {
                "principal": "biostatistician_agent_service_principal",
                "schema": f"{self.catalog}.{self.schema_name('gold')}",
                "privileges": ["USE_SCHEMA", "SELECT", "MODIFY"],
                "purpose": "Biostatistician Agent publishes aggregates and reports",
            },
            {
                "principal": "simulation_readers",
                "schema": f"{self.catalog}.{self.schema_name('gold')}",
                "privileges": ["USE_SCHEMA", "SELECT"],
                "purpose": "Human reviewers and BI tools read the Gold layer only",
            },
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog": self.catalog,
            "schemas": self.schemas,
            "root_uri": self.root_uri,
            "tables": [t.fqn for t in self.all_tables()],
        }
