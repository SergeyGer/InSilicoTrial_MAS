# Notebooks

| Notebook | Purpose |
| --- | --- |
| `insilico_trial_demo.ipynb` | The interactive verification notebook from the specification: environment checklist, cohort generation, distributed agent execution, Delta time travel, lineage and MLflow tracing. |

## Running it

**On Databricks** — attach the notebook to a cluster that has the wheel installed
(the bundle job installs it) and set the widgets:

* `config` — configuration profile, e.g. `conf/simulation_cluster.yaml`
* `n_patients` — cohort size, e.g. `10000`

**On a laptop** — the notebook is ordinary Python apart from the optional widget
helper and `display()`:

```bash
bash scripts/bootstrap.sh
.venv/bin/python scripts/run_notebook.py --patients 200 --epochs 3
```

The runner executes every code cell against a small offline profile, so a broken
cell fails CI instead of surprising a reader months later.

## What to look for

1. **Cell 1** prints the environment checklist and the recommended engine:
   `spark` on a cluster, `local` on Databricks Community Edition, `sequential` for
   tiny runs.
2. **Cell 2** shows the screen-failure rate and the stratified allocation; the
   CONSORT-style accounting ends up in the report.
3. **Cell 3** runs the whole pipeline. On a cluster, open the **Spark Jobs** UI
   during this cell: you will see one `applyInPandas` stage with one task per
   cohort partition, and `mapInPandas` if you switch `engine.partition_mode` to
   `balanced`.
4. **Cell 4** reads historical versions of the Silver table
   (`DESCRIBE HISTORY`, `VERSION AS OF`) and prints the replay recipe from the run
   manifest.
5. **Cell 5** prints the lineage graph (agent → table → report) and the LLM trace
   summary: prompt token distribution, latency percentiles and the per-patient
   span hierarchy.

## Notes

* Time travel works on every backend: with `storage.backend: delta` it is Delta
  Lake, with `local` it is the versioned Parquet store that implements the same
  semantics (`LocalVersionedStore.read(version=n)`).
* On a cluster, set `tracking.trace_path` to a shared path (DBFS or a Unity
  Catalog volume) so executor traces are collected in one file.
* The notebook deliberately avoids `%sql` cells so that it also runs without a
  Spark session; the equivalent SQL is shown in comments and in
  `docs/RUNBOOK_DATABRICKS_AWS.md`.
