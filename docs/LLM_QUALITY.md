# LLM quality and guardrails

How this project decides what an LLM is allowed to influence, how unreliable
answers are detected, and what happens when a model misbehaves. Every claim below
points at the code that implements it.

Scope note first, because it explains the whole design: **the LLM never produces a
number that appears in a result table.** Concentrations, effect sizes, adverse-event
probabilities, confidence intervals and p-values are computed by the mechanistic and
statistical code. The model contributes *qualitative* narration (how a patient says
they feel) and is treated as untrusted input at every step.

## Where the model is used

| Surface | Module | Role |
|---|---|---|
| Patient persona | `src/insilico_trial_mas/agents/patient_agent.py` | narrates symptoms per epoch; may escalate severity and add terms the model did not predict |
| Protocol agent | `src/insilico_trial_mas/agents/protocol_agent.py` | validates the protocol deterministically; does not call an LLM for decisions |
| Biostatistician | `src/insilico_trial_mas/agents/biostatistician_agent.py` | recomputes statistics from data; `llm_reported_patients` records how much of a safety table came from the model |
| Prompt contract | `src/insilico_trial_mas/llm/prompts.py` | builds the JSON-only contract and parses the answer |

### When the model is called at all

`PatientAgent._should_consult_llm` (`patient_agent.py:287`) is a cost and quality
control, not an optimisation detail:

* `epoch == 0` or `llm_mode == "off"` — never;
* `MAX_LLM_CALLS_PER_PATIENT = 6` (`patient_agent.py:48`) — hard budget per patient;
* `llm_mode == "all"` — every epoch;
* `llm_mode == "triggered"` — only when `worst_ctaae_grade >= llm_trigger_grade`
  (default `2`, `config.py:147`), or when the seeded sample draw fires
  (`llm_sample_rate`, default `0.05`).

The sample draw uses `rng_for(seed, run_id, patient_id, epoch, "llm-sample")`, so
*which* patients get a narrative is reproducible, not random per run.

## The eight layers

### 1. Prompt contract

`build_patient_prompt` (`llm/prompts.py`) instructs the model to answer with a single
JSON object and states the allowed keys and enum values (`symptoms[]`, `ctcae_grade`,
`overall_tolerability`, `adherence_intent`). Tests pin the contract:
`test_prompt_contains_persona_and_schema` (`tests/test_llm.py:52`).

### 2. Extraction and repair

`_extract_payload` (`llm/prompts.py:197`) handles the four shapes models actually
emit: strict JSON, fenced JSON, JSON with trailing commas or single quotes, and
Python-literal dicts. The last one is parsed with `ast.literal_eval` — **never**
`eval`. A repair is recorded (`ParsedSymptoms.repaired`) so it is visible later.

### 3. Structural validation and bounds

`parse_symptom_response` (`llm/prompts.py:272`) is the single entry point and it
**never raises**: every failure becomes `parse_error` with the raw text preserved, and
the caller decides the policy. Within it:

* at most `MAX_LLM_SYMPTOMS = 5` symptoms are accepted (`patient_agent.py:46`);
* `term` is truncated to 120 characters, the verbatim quote to 400;
* `ctcae_grade` passes through `_coerce_grade` (`prompts.py:259`), which rejects
  booleans and non-numeric values and clamps to the CTCAE range;
* `overall_tolerability` must be one of `{"good", "acceptable", "poor"}` and
  `adherence_intent` one of `{"continue", "unsure", "discontinue"}`
  (`prompts.py:153`); anything else is replaced with the safe default.

Tests: `test_parse_plain_json`, `test_parse_markdown_fenced_json`,
`test_parse_repairs_trailing_commas_and_single_quotes`, `test_parse_bare_array_and_extra_prose`,
`test_parse_clamps_grade_and_limits_count`, `test_parse_reports_unparseable_output`,
`test_parse_empty_response` (`tests/test_llm.py:83-130`).

### 4. Safety filter

`FORBIDDEN_MEDICAL_ADVICE` (`llm/prompts.py:157`) lists prescriptive phrases a patient
simulator must never emit ("you should take", "i recommend", "diagnosis is",
"prescribe", "stop taking your"). A response containing any of them is discarded
entirely (`parse_error="response contained medical advice and was rejected by the
safety filter"`), not partially used. Pinned by `test_parse_rejects_medical_advice`
(`tests/test_llm.py:110`).

### 5. Merge policy: narration may only add or escalate

`_apply_narration` (`patient_agent.py:394`) is where an LLM answer meets model output:

* a term the drug model already predicted can only have its grade **escalated**, never
  reduced, and its `source` becomes `hybrid`;
* a term the model did not predict is kept but flagged `source = "llm"` and its `soc`
  is set to "Reported by patient (LLM)";
* the verbatim quote is stored separately, so prose never overwrites a structured
  field.

This is what makes the safety table auditable: `llm_reported_patients`
(`biostatistician_agent.py:357`) counts how many patients in each row were influenced
by the model at all.

### 6. Resilience: retry, rate limit, timeout, deterministic fallback

`ResilientLLMClient` (`llm/resilient.py:93`) wraps every provider with a response
cache, a token-bucket rate limiter, a timeout, bounded exponential-backoff retries for
retryable errors only, and an optional fallback provider. A failing model therefore
cannot abort a run: the fallback is the deterministic offline client, which produces
the *same JSON contract* (`heuristic_symptom_response`, `prompts.py:329`), so
downstream code has one code path.

Counters live in `LLMCallStats` (`resilient.py:29`) and are exported as MLflow metrics:
`llm_calls`, `llm_cache_hits`, `llm_failures`, `llm_retries`, `llm_fallbacks`,
`llm_tokens_in`, `llm_tokens_out`, `llm_mean_latency_ms`, `llm_max_latency_ms`.

### 7. Determinism and replay

An LLM is not deterministic even at temperature 0, so the project replays instead of
trusting: `LLMResponseCache` (`llm/cache.py:54`) keys every answer on a stable digest
of provider, model, temperature, system prompt and prompt (`cache_key`,
`cache.py:29`, built on `stable_hash` from `reproducibility.py`), and stores it in an
append-only JSONL file. `test_cache_round_trip` and `test_cache_disabled_returns_nothing`
(`tests/test_llm.py:149`) pin the behaviour.

The engine comparison additionally ignores exactly two columns —
`NON_DETERMINISTIC_COLUMNS = ("observation_ts", "llm_latency_ms")`
(`engine/partition.py:57`) — and `scripts/verify_reproducibility.py` fails the build if
the remaining rows differ between engines. That is also a CI step
(`Simulation plausibility and reproducibility` job, `.github/workflows/ci.yml:63`).

### 8. Verification in CI

| Check | What it proves |
|---|---|
| `pytest -q tests/test_llm.py` | parsing, repair, safety filter, clamping, cache, rate limiting |
| `pytest -q tests/test_calibration.py` | the *output* stays clinically plausible: exposure in range, efficacy separation between arms, non-catastrophic safety rates |
| `python scripts/verify_reproducibility.py --patients 400 --epochs 4` | two runs are bit-identical apart from the two documented columns |
| `demo` run in CI | the end-to-end pipeline still produces a dashboard, report and CDISC exports |

`tests/test_pipeline.py::test_repeated_runs_are_bit_identical` and
`tests/test_agents.py::test_patient_agent_is_deterministic` cover the same property at
a smaller scale.

## Failure modes and what happens

| Failure | Detection | Consequence |
|---|---|---|
| Model returns prose instead of JSON | `_extract_payload` then `parse_error` | symptom list empty; run continues; `llm_error` recorded |
| JSON is almost valid (fences, trailing commas) | repair strategies | accepted, `repaired=True` is kept for audit |
| Grade out of range or wrong type | `_coerce_grade` | clamped to a valid CTCAE grade |
| Unknown enum value | whitelist check | safe default (`acceptable` / `continue`) |
| Prescriptive medical advice | `FORBIDDEN_MEDICAL_ADVICE` | whole response rejected |
| Model invents a symptom | merge policy | kept, but flagged `source = "llm"` and counted in `llm_reported_patients` |
| Model contradicts the model-predicted severity | `_apply_narration` | escalation allowed, de-escalation ignored |
| Provider is down or rate-limits | `ResilientLLMClient` | retries, then deterministic offline fallback; `llm_fallbacks` metric increases |
| Same run executed twice | cache + `verify_reproducibility.py` | identical text replayed or the build fails |

## Honest limits

* **No clinical validity claim.** This is decision support for exploratory in-silico
  work on synthetic data; it is not evidence of safety or efficacy in humans.
* **The offline heuristic is a stand-in, not a model of a patient.** It exists so runs
  are reproducible and cheap; its output is documented as non-clinical
  (`heuristic_symptom_response` docstring).
* **The cache replays text; it does not verify truth.** If a cached answer is wrong, it
  stays wrong — which is why the numeric results never depend on it.
* **A model can still add plausible-sounding symptoms.** They are marked
  `source = "llm"` and appear in `llm_reported_patients`, but a reader must treat those
  rows as qualitative.

## Auditing a run

```bash
# 1. what the run used: provider, model, mode, call and error counts
python - <<'PY'
import json, pathlib
manifest = json.loads(max(pathlib.Path("artifacts").rglob("run_manifest.json")).read_text())
print(json.dumps(manifest["provenance"], indent=2))
PY

# 2. rows where the model failed (typed column, never free text)
python - <<'PY'
import glob, pandas as pd
latest = sorted(glob.glob("artifacts/lake/silver/patient_states/_version=*"))[-1]
df = pd.read_parquet(latest)
print(df["llm_used"].value_counts(dropna=False))
print(df.loc[df["llm_error"].fillna("") != "", ["patient_id", "epoch", "llm_error"]].head())
PY

# 3. provenance of every adverse event: model, hybrid or llm
python - <<'PY'
import glob, pandas as pd
latest = sorted(glob.glob("artifacts/lake/silver/adverse_events/_version=*"))[-1]
df = pd.read_parquet(latest)
print(df["source"].value_counts())
PY

# 4. rerun with the model disabled and compare the numbers
python -m insilico_trial_mas.cli simulate --patients 200 --epochs 3 --llm-mode off --json
```

The Silver tables keep one directory per version (`_version=000042`), so
`sorted(...)[-1]` is the newest one — the same layout the local store writes and the
dashboard reads. `run_manifest.json` records the effective LLM settings under
`provenance`: `llm_provider`, `llm_model`, `llm_mode`, `total_llm_calls`,
`llm_cache_hits`, `llm_errors`.
