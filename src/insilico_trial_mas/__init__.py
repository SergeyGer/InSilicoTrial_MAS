"""InSilicoTrial MAS - distributed multi-agent in-silico clinical trial simulation.

The package models a synthetic patient cohort as a multi-agent system:

* :mod:`insilico_trial_mas.agents.protocol_agent` - the trial organiser.
* :mod:`insilico_trial_mas.agents.patient_agent` - synthetic patient personas
  (hybrid mechanistic PK/PD + tabular ML + LLM qualitative reasoning).
* :mod:`insilico_trial_mas.agents.biostatistician_agent` - aggregates the logs
  and produces a compliance-ready report.

Execution is delegated to pluggable engines (:mod:`insilico_trial_mas.engine`)
so the very same agent code runs sequentially, in a local multiprocessing pool,
or distributed across a PySpark/Databricks cluster.
"""

from __future__ import annotations

from .version import __version__

__all__ = ["__version__"]
