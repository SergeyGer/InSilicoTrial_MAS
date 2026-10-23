# Documentation index

Task-oriented map of everything in this repository. The narrative wiki lives at
<https://github.com/SergeyGer/InSilicoTrial_MAS/wiki> and is indexed by the
[documentation map](https://github.com/SergeyGer/InSilicoTrial_MAS/wiki/Documentation-Map).

## Start here

| Document | Answers |
| --- | --- |
| [README](../README.md) | What the project is, why it exists, how to run it in one command |
| [Getting Started](https://github.com/SergeyGer/InSilicoTrial_MAS/wiki/Getting-Started) | First run, expected output, troubleshooting the very first steps |
| [DOCKER.md](DOCKER.md) | Building and running the four image targets, compose stack, volumes |
| [User interface](UI.md) | The readout dashboard and the live Studio |

## Understand the system

| Document | Answers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Agents, engines, storage layers, the hybrid physiology model |
| [DATA_MODEL.md](DATA_MODEL.md) | Medallion tables, Delta contracts, lineage, time travel |
| [SPEC_COMPLIANCE.md](SPEC_COMPLIANCE.md) | Which requirement of the specification is implemented where |
| [WHY_AGENTS.md](WHY_AGENTS.md) | Why three agents instead of one pipeline |
| [LLM quality and guardrails](LLM_QUALITY.md) | How model output is validated, bounded, replayed and audited |

## Operate and secure it

| Document | Answers |
| --- | --- |
| [CONFIGURATION.md](CONFIGURATION.md) | Settings, precedence, credentials, secrets handling |
| [RUNBOOK_DATABRICKS_AWS.md](RUNBOOK_DATABRICKS_AWS.md) | Deploying and operating the Databricks on AWS stack |
| [SECURITY_NOTES.md](SECURITY_NOTES.md) | Security decisions, dismissed findings and why |
| [REPOSITORY_SETTINGS.md](REPOSITORY_SETTINGS.md) | Branch ruleset, required checks, environments |
| [ETHICS_AND_LIMITATIONS.md](ETHICS_AND_LIMITATIONS.md) | Responsible use, synthetic data, what the results may not be used for |

## Conventions

* every claim about behaviour points at the code or at a test that proves it;
* documentation changes go through the same pull request checks as code;
* [`.markdownlint.jsonc`](../.markdownlint.jsonc) and
  [`.lychee.toml`](../.lychee.toml) define the linting and link-checking rules that CI
  enforces on this directory and on the README.

If a document here contradicts a test or the CLI help output, the test wins — open an
issue so the text is corrected.
