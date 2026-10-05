# Security notes

CodeQL (`python` queries) runs on every push and pull request to `main`, plus a
weekly scheduled scan — see [`.github/workflows/codeql.yml`](../.github/workflows/codeql.yml).
This file records what the analysis found and how each finding was resolved, so the
decisions can be reviewed in the repository instead of only in the alert UI.

Across the project's history the scanner opened **24 alerts: 23 fixed in code, 1 dismissed
as a false positive with a written rationale** (see below). The first scan produced 21 of
them; the three later ones came from intermediate commits of the same hardening work and
were fixed in the follow-up commit.

## Hardening patterns introduced

Two patterns are now part of the codebase. Both are enforced by tests in
[`tests/test_credentials.py`](../tests/test_credentials.py).

### Never log provider text

`llm/logging_utils` exposes `error_kind(exc)`, which maps an exception to a
**constant** from a closed vocabulary (`timeout`, `connection`, `invalid-value`,
…). Provider SDKs are not careful about what they echo back: a throttling or
authorisation error can contain the request payload, the prompt or the API key, so
`f"{exc}"` in a log line, in the Silver `llm_error` column or in a traceback pasted
into an issue can copy that text out of the process.

`debug_error_text(exc)` still returns the full `Type: message` text, but it is
documented as sanctioned **only** inside an exception (`raise … from`) and never in
a log call.

### Never log identifiers

`anonymised_ref(identifier)` returns a stable 8-character SHA-256 digest. A failing
agent stays traceable and correlatable with the run manifest, but no patient-level
value is written into the log stream.

### Keep artefacts to what the UI renders

`ui/data.py` carries only the environment fields the reproducibility panel displays
(`DASHBOARD_ENVIRONMENT_FIELDS`). The dashboard is a demo artefact, not an archive:
if a field is not rendered, it is not embedded.

## Findings and resolutions

| # | Query | Location | Resolution |
| --- | --- | --- | --- |
| 2 | `py/clear-text-logging-sensitive-data` | `agents/patient_agent.py` | **Fixed.** Provider exception text replaced by `error_kind()`; the Silver `llm_error` column stores the label too. |
| 1 | `py/clear-text-storage-sensitive-data` | `ui/dashboard.py` | **Dismissed** — false positive, rationale below. |
| 3 | `py/unused-local-variable` | `storage/delta_store.py` | **Fixed.** The unused `writer` re-assignment is gone; the append path calls `insertInto` directly and documents that it ignores the write mode. |
| 4, 5 | `py/unused-global-variable` | `storage/factory.py`, `agents/patient_agent.py` | **Fixed.** The storage factory logs the selected backend (making its logger meaningful); the unused module logger in the patient agent was removed. |
| 6–13 | `py/comparison-of-identical-expressions` | `reporting/report.py`, `ui/dashboard.py`, `ui/data.py`, `scripts/calibrate_protocol.py` | **Fixed.** NaN guards use `math.isnan()` instead of the `x != x` idiom. |
| 14 | `py/ineffectual-statement` | `ml/physiology.py` | **Fixed.** The `PhysiologyModel` protocol method raises `NotImplementedError` instead of a bare ellipsis, so a subclass that forgets to implement it fails loudly. |
| 15, 16 | `py/unnecessary-delete` | `scripts/render_architecture.py`, `tests/test_pipeline.py`, `tests/test_credentials.py` | **Fixed.** The leftover `del` statements and the unused parameter were removed. |
| 17, 18 | `py/pythagorean` | `stats/estimators.py` | **Fixed.** Newcombe intervals use `math.hypot(a, b)`. |
| 19 | `py/catch-base-exception` | `async_utils.py` | **Fixed.** `run_sync` catches `Exception` (re-raised in the caller's thread) and lets `KeyboardInterrupt`/`SystemExit` propagate. |
| 20, 21 | `py/empty-except` | `llm/prompts.py` | **Fixed.** The JSON repair strategies record why each attempt failed and report the original decode error when nothing parses. |

## Dismissed: `py/clear-text-storage-sensitive-data` in `ui/dashboard.py`

The alert points at `target.write_text(build_dashboard(data), …)` — the line that
writes the run readout. It is a false positive:

* The artefact contains **synthetic simulation output only**. There is no
  credential, key, token or real patient record anywhere in it;
  [README.md](../README.md) and [ETHICS_AND_LIMITATIONS.md](ETHICS_AND_LIMITATIONS.md)
  state the synthetic-only scope, and `insilico-trial env-check` never reads a
  credential value.
* The taint source is CodeQL's **identifier heuristic**, not a secret: the payload
  carries per-patient cohort records (`patient_id`, dose/exposure/trajectory rows,
  persona narration) and LLM **token counters** (`prompt_tokens`,
  `completion_tokens`, rendered as the token-distribution panel in the Lineage &
  traces screen). Names containing *patient* and *token* match the patterns the
  query treats as private data.
* The two findings raised by the same query family that **were** genuine were fixed
  in code (see the patterns above): provider SDK text no longer reaches logs, and
  log lines carry a digest instead of an identifier. The dashboard payload was also
  reduced to what the UI renders.
* Removing patient-level detail from the readout would remove the feature itself —
  per-patient review of dose, exposure, adverse events and narration is the point of
  the Patients screen. Deployments that share a dashboard outside the trial team
  should treat the file as confidential study material, exactly like any other
  patient-level listing, and rely on the run manifest for provenance.

If a future release adds support for **real** patient data, this decision must be
revisited: the export would then need pseudonymisation or access control before the
artefact leaves the workspace.
