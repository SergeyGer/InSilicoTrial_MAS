# Security Policy

## The project holds no real patient data

InSilicoTrial MAS simulates clinical trials over **fully synthetic cohorts**. No module
reads an external patient dataset: the only inputs are YAML files (protocol definitions,
simulation configs and the population-level priors in
`src/insilico_trial_mas/resources/genomic_priors.yaml`), and every patient record is
generated from a seed. There is no join key to any real record and no protected health
information in the system.

Two consequences for security reporting:

- A breach of this repository cannot expose real patients, because none are here. That does
  **not** make the system harmless: it can hold AWS/Databricks/Bedrock credentials and
  narrative text that looks like sensitive clinical material.
- Generated artefacts (reports, Dashboards, `symptoms_json`, LLM caches and trace files)
  contain realistic-looking synthetic personas. Treat them as sensitive-looking material,
  and never attach a real credential, a real patient record or a confidential protocol to an
  issue, discussion or advisory.

## Supported versions

| Version | Supported |
| --- | --- |
| `1.x` (current: `1.0.0`, the `main` branch) | Yes — security fixes are released as patch versions on the latest `1.x`. |
| `< 1.0.0`, development branches, forks and unmerged pull requests | No |

Because the package is `1.x` and pre-1.0 artefacts are not published, only the latest `1.x`
release receives fixes. Always include the exact version (`insilico-trial --version` or
`insilico_trial_mas.version.__version__`) and the git revision from `run_manifest.json`.

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.** No public
issues: a public report exposes every deployment before a fix exists.

Use GitHub's private vulnerability reporting (a private GitHub Security Advisory):

1. Open <https://github.com/SergeyGer/InSilicoTrial_MAS/security/advisories/new>
   (or the repository's **Security** tab, then **Report a vulnerability**).
2. Describe the problem in English, with the affected version, the affected module or path,
   the impact, and a minimal reproduction (command, config and seed).
3. If the finding involves credentials, rotate them first and redact them in the report —
   never paste a live token, key or connection string.

Please include, where possible:

- the package version, Python version, engine backend and storage backend;
- the reproduction command (`insilico-trial demo --patients 200 --epochs 3` runs offline and
  needs no credentials, which makes reports much easier to reproduce);
- the `insilico-trial env-check` output;
- the exact file or endpoint involved, and what an attacker gains.

A report you send privately is treated as confidential. If the finding is not security
relevant, we will say so and redirect it to the issue tracker.

## Response targets

| Stage | Target |
| --- | --- |
| Acknowledgement of the report | Within 3 business days |
| Initial assessment (severity, affected versions, whether a fix is warranted) | Within 10 business days |
| Fix or mitigation plan communicated | As soon as the assessment is complete; a patched release is prepared for confirmed high-severity findings |

This is a community project maintained by a single maintainer: the targets are best effort
and there is no bug bounty or monetary reward. Credit in the release notes and the advisory
is offered unless you prefer to stay anonymous. Please allow us to release a fix before
publishing details (coordinated disclosure); if a fix is not possible within 90 days of the
assessment, tell us and we will agree on a disclosure date.

## In scope

Findings in the repository's own code and infrastructure definitions, including:

- **Credential handling in the LLM layer.** Credentials or provider tokens leaking into
  logs, run manifests, trace files, the LLM response cache, error messages or the generated
  report; unsafe defaults in `llm/langchain_provider.py`, `llm/factory.py`, `llm/resilient.py`
  or the LLM settings in `config.py`; anything that causes secrets to be written into
  `artifacts/`. The default provider is the credential-free `offline` one — the risk is on
  the `bedrock`/`openai`/`langchain` paths, which rely on the standard AWS/OpenAI credential
  chains.
- **Path traversal in the Studio server.** `insilico-trial studio` (`ui/studio.py`) binds to
  `127.0.0.1` by default and serves run artefacts; a way to read or write files outside the
  configured run directory, to escape the protocol allow-list in `resolve_protocol`, or to
  bypass the traversal checks in the request router or the artifact handler is in scope.
- **Unsafe deserialisation.** Protocol, config and priors YAML are parsed with
  `yaml.safe_load`; persona responses are repaired and parsed with `ast.literal_eval`, never
  `eval`. Any input that achieves arbitrary code execution, object instantiation or a crash
  escalation through these parsers, through Parquet/JSON artefact loading, or through the
  `pickle`-based local engine boundary (`ProcessPoolExecutor` with module-level callables) is
  in scope.
- **Injection through protocol YAML or LLM output.** Jinja2 report templates, the inline
  JSON payload and the SVG charts in the self-contained dashboard, and the Studio's HTML
  responses are all rendered from data that includes model- and LLM-generated text. Cross-
  site scripting (including payloads that break out of the embedded `<script>` block),
  template injection via a protocol field, or prompt injection that turns the persona
  narrator into an advice-dispensing or file-touching component are in scope — the LLM's
  blast radius is supposed to stay inside narration fields.
- **IaC misconfiguration that could expose patient-adjacent data.** `terraform/` defines
  Unity Catalog objects, S3 buckets, storage credentials and grants
  (`unity_catalog.tf`, `aws_storage.tf`, `service_principals.tf`, `warehouses.tf`). Public
  or over-broad bucket policies, missing encryption or versioning, an external location or
  storage credential that can be assumed too widely, or grants that give a principal more
  than the documented read/write split are in scope.
- **Dependency and CI weaknesses that this repository can fix.** A vulnerable pin in
  `pyproject.toml`, `requirements*.txt` or a GitHub Actions workflow, or a CI configuration
  that would expose secrets to a fork's pull request.

## Out of scope

- **The illustrative priors and drug parameters being "clinically wrong."** The ancestry
  mixture, allele frequencies, physiology priors and PK/PD values are deliberately
  illustrative and must be replaced before any regulated use; that is documented in
  `docs/ETHICS_AND_LIMITATIONS.md` and is a scientific limitation, not a vulnerability.
- **Simulation results that do not match a real trial, or any claim about clinical
  validity.** The platform is a trial-design decision-support tool: it is not clinical
  evidence, not a medical device, and not a submission package.
- **Resource exhaustion that you cause deliberately** through a huge `--patients` /
  `--epochs` value, a pathological protocol YAML, or an unbounded LLM mode on your own
  credentials.
- **The Studio having no authentication.** It is a loopback developer/demo tool by design;
  binding it to a non-loopback interface with `--host` exposes it to your network, which is
  a deployment decision, not a defect. A code-level bypass of the loopback default is in
  scope.
- **Third-party providers and packages.** Vulnerabilities in AWS, Databricks, MLflow,
  LangChain, Spark or a Python dependency should be reported upstream; we will bump the
  affected dependency when a fixed release exists. Prompt-injection behaviour that is
  inherent to a hosted LLM and produces no effect beyond narration is likewise out of scope.
- **Anything requiring a compromised host, stolen credentials, physical access, or a
  malicious actor with write access to the repository or workspace.**

## Safe harbour

We will not pursue or support legal action against researchers who act in good faith under
this policy: who test only against their own deployments and synthetic data, avoid privacy
violations and service disruption, report promptly and privately, and give us reasonable
time to fix the issue before public disclosure.

## Hardening checklist for operators

The defaults are safe for a laptop, but a shared deployment needs more:

- keep the offline LLM provider (or a provider you actually control) and never commit
  `.env` or `terraform.tfvars`;
- keep the Studio on loopback; put the Databricks job and Unity Catalog permissions in front
  of shared access instead of exposing `--host`;
- keep `tracking.strict` and `storage` paths inside a directory whose contents you are
  willing to treat as sensitive-looking, because traces and caches contain generated
  clinical narratives;
- review `terraform plan` output for grants, bucket policies and public access blocks before
  every apply, and enable the storage lifecycle/retention settings you intend to rely on.

## Static analysis

CodeQL runs on every push and pull request to `main`, plus a weekly scan. Findings
are triaged in the repository rather than only in the alert UI:
[docs/SECURITY_NOTES.md](docs/SECURITY_NOTES.md) lists every alert with its
resolution, and documents the two hardening patterns the analysis produced - never
log provider exception text (`logging_utils.error_kind`) and never log identifiers
(`logging_utils.anonymised_ref`).
