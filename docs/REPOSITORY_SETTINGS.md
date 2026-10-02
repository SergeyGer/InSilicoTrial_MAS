# Repository settings

The exact configuration to apply to <https://github.com/SergeyGer/InSilicoTrial_MAS> after
the first push. It is written as a checklist a maintainer can work through: every value below
is literal, so copy it rather than paraphrasing it.

Repository facts assumed here: default branch `main`, package version `1.0.0`, MIT licence,
owner `SergeyGer`.

> The branch-protection check names in section 6 were read from
> `.github/workflows/ci.yml` and `.github/workflows/codeql.yml`. If a workflow or a job
> `name:` changes, update the required checks in the same pull request - a renamed check
> silently stops blocking merges.

---

## 1. About

| Field | Value |
| --- | --- |
| Description | `Multi-agent in-silico clinical trial simulation on Databricks & AWS: synthetic patient personas (PK/PD + ML + LLM), Spark/Delta/MLflow pipeline, biostatistical readout, DSMB rules.` |
| Website | `https://github.com/SergeyGer/InSilicoTrial_MAS/tree/main/docs` (see the note below) |
| Topics | See section 2 |

The description is 180 characters, inside GitHub's 350-character limit, and names the three
things a reviewer needs: synthetic-cohort simulation (synthetic patient personas), the
multi-agent design, and the Databricks/AWS runtime. It is the exact `DESCRIPTION` constant in
`scripts/github_repo_setup.py`; that script is the single source of truth for the About box,
so if you reword it, change the constant and the `curl` payload in section 8 as well.

Website: `scripts/github_repo_setup.py` ships `HOMEPAGE` pointing at the documentation tree
above, and `make github-setup` applies it. There is no hosted site yet; never point this field
at a run artefact or a personal page.

About panel checkboxes:

- **Releases**: on (so `v1.0.0` shows on the repository home page once tagged).
- **Packages**: off (nothing is published to GitHub Packages).
- **Deployments**: off (Databricks deployments do not report here).
- **Environments**: on (`dev` and `prod` mirror the bundle targets).
- **Downloads**: the setup script leaves `has_downloads` enabled; harmless, and it keeps the
  source archive links working.

## 2. Topics

16 lowercase, hyphenated topics covering the stack and the domain. This is the exact
`TOPICS` tuple in `scripts/github_repo_setup.py`, so `make github-setup` applies it verbatim:

```text
clinical-trials
in-silico-trials
multi-agent-systems
synthetic-data
digital-twins
pharmacokinetics
pharmacodynamics
databricks
apache-spark
delta-lake
mlflow
unity-catalog
aws
terraform
langchain
python
```

Keep the list between 12 and 16: GitHub surfaces at most 20, and a shorter list keeps the
repository findable for the terms that matter (`clinical-trials`, `in-silico-trials`,
`databricks`).

## 3. Social preview

| Field | Value |
| --- | --- |
| File | `.github/social-preview.png` |
| Size | 1280 x 640 px (the committed file is exactly that) |
| Regenerate | `make architecture` (runs `scripts/render_social_preview.py`; the Release workflow verifies it with `--check`) |
| Uploaded | Manually, in the UI: **Settings -> General -> Social preview -> Edit -> Upload an image** |

There is no API for the social preview image; it must be uploaded by hand after the file is
merged. Keep the file under 1 MB, keep the title legible at 400 px wide (the size GitHub
uses in link cards), and do not put a claim of clinical validity on it.

## 4. Feature toggles

| Feature | Setting | Why |
| --- | --- | --- |
| Issues | **On** | The issue forms in `.github/ISSUE_TEMPLATE/` request the engine backend, the Python version, the `insilico-trial env-check` output and the synthetic-data confirmation. |
| Discussions | **On** | Linked from `.github/ISSUE_TEMPLATE/config.yml`; use it for "how do I", protocol design and design debate so the issue tracker stays a defect tracker. Enable the categories: Announcements, General, Ideas, Q&A, Show and tell. |
| Wiki | **Off** | Documentation lives in `docs/` in this repository, under review, with `CODEOWNERS` coverage; two documentation surfaces drift. |
| Projects | **Off** | A single maintainer tracks work with labels and milestones; an empty project board is noise. |
| Sponsorships | **Off** | No `FUNDING.yml` is shipped and no funding is solicited. |
| Preserve this repository | Optional | Enable it if the repository is ever archived; irrelevant while active. |
| Allow forking | **On** | Required for the normal pull-request flow. |

Pull request merge settings (Settings -> General -> Pull Requests):

- Allow **squash merging** (default commit message: pull request title).
- Allow **rebase merging**.
- **Disable merge commits** - `main` requires linear history (section 6).
- **Automatically delete head branches**: on.

## 5. Security and analysis

Apply all of these; they are free for public repositories.

| Setting | Value |
| --- | --- |
| Private vulnerability reporting | **On** — required by `SECURITY.md`, which sends reporters to a private advisory |
| Dependency graph | On |
| Dependabot alerts | On |
| Dependabot security updates | On |
| Dependabot version updates | Driven by `.github/dependabot.yml` (weekly, grouped) |
| Code scanning | **CodeQL: advanced** — the workflow in `.github/workflows/codeql.yml` |
| CodeQL default setup | **Off** — default setup and an advanced workflow cannot both run |
| Secret scanning | On |
| Secret scanning push protection | On |

Actions settings (Settings -> Actions -> General):

- Actions permissions: allow GitHub-authored actions and verified creators only.
- Workflow permissions: **read repository contents** by default; no workflow currently needs
  write access beyond `security-events: write` in CodeQL.
- "Allow GitHub Actions to create and approve pull requests": **off**.
- Fork pull request workflows: require approval for first-time contributors.

## 6. Branch protection for `main`

Settings -> Branches -> Add branch protection rule (or the equivalent repository ruleset,
which also lets you protect the `v*` tags):

| Option | Value |
| --- | --- |
| Branch name pattern | `main` |
| Require a pull request before merging | **On** |
| Required approvals | **1** (see the single-maintainer note below) |
| Dismiss stale pull request approvals when new commits are pushed | **On** |
| Require review from Code Owners | Off in `scripts/github_repo_setup.py`; recommended **On** once a second maintainer joins, so `.github/CODEOWNERS` (which protects `ml/`, `agents/`, `terraform/`, the workflows and the ethics/security policies) is enforced by GitHub rather than by convention |
| Require approval of the most recent reviewable push | On |
| Require conversation resolution before merging | **On** |
| Require status checks to pass before merging | **On** |
| Require branches to be up to date before merging | **On** (the script sends `"strict": true`) |
| Required status checks | The exact names below |
| Require linear history | **On** |
| Require deployments to succeed | Off |
| Lock branch | Off |
| Do not allow bypassing the above settings | Off — `"enforce_admins": false`, so the maintainer can administer the repository |
| Allow force pushes | **Off** |
| Allow deletions | **Off** |

Required status checks, copied verbatim from the workflows:

```text
Lint and type-check
Tests (Python 3.10)
Tests (Python 3.11)
Tests (Python 3.12)
Simulation plausibility and reproducibility
Spark engine (local[*] driver)
Terraform and bundle validation
Analyze Python
```

`scripts/github_repo_setup.py --protect-main` applies exactly these eight
`REQUIRED_CHECKS`, CodeQL included, so a security regression cannot be merged either.

Notes:

- Seven checks come from `.github/workflows/ci.yml`; `Analyze Python` is the job in
  `.github/workflows/codeql.yml`. GitHub matches status checks by the job's `name:`, and a
  matrix job produces one check per matrix value - which is why the Python versions are
  listed separately. `.github/workflows/release.yml` runs on `v*` tags only, so its jobs
  must not be added here.
- A check can only be selected after it has run at least once; push a trivial pull request
  first, then add the checks (the setup script prints a reminder if GitHub rejects the
  payload for this reason).
- With a single maintainer and "Required approvals: 1", the maintainer cannot approve their
  own pull request. Either keep 1 approval and use the admin bypass, or set required
  approvals to 0 and rely on the status checks plus the review discipline. Do not switch
  "Do not allow bypassing" on while the project has one maintainer, or the repository
  becomes unmergeable.
- Enable the same protection for tags `v*` (rulesets make this easy) so a release tag cannot
  be moved after publication.

## 7. Environments, secrets and labels

Environments (Settings -> Environments) mirror the Databricks Asset Bundle targets:

| Environment | Purpose | Suggested protection |
| --- | --- | --- |
| `dev` | Bundle target `dev`, Community Edition / scratch workspace | None |
| `prod` | Bundle target `prod`, the real workspace and bucket | Required reviewers: the maintainer; restrict the branch to `main` |

Secrets and variables (repository or environment scope):

- `DATABRICKS_HOST`, `DATABRICKS_TOKEN` for `bash scripts/databricks_deploy.sh` and
  `make bundle-validate`.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` only if `terraform plan/apply`
  is ever automated; prefer short-lived OIDC credentials over long-lived keys.
- No LLM provider keys are needed for CI: the default provider is the credential-free
  offline one.

Labels to create (Settings -> Labels), beyond the two used by the issue forms:

| Label | Colour | Use |
| --- | --- | --- |
| `bug` | `#d73a4a` | Created by the bug form |
| `enhancement` | `#a2eeef` | Created by the feature form |
| `documentation` | `#0075ca` | Docs, runbooks, ethics text |
| `dependencies` | `#0366d6` | Dependabot pull requests |
| `python`, `github-actions`, `terraform` | `#1d76db` | Dependabot ecosystem labels |
| `security` | `#b60205` | Hardening work that is not a private advisory |
| `science: calibration` | `#fbca04` | PK/PD, physiology, AE or priors changes that need calibration evidence |
| `engine: spark` | `#5319e7` | Spark engine and Delta storage |
| `reproducibility` | `#0e8a16` | Determinism, seeds, run manifests |
| `good first issue` | `#7057ff` | Scoped, well-specified work for new contributors |

Milestones: `v1.0.0` (the initial release, close it when tagged) and `v1.1.0` for the next
group of changes.

## 8. How to apply via API

**Preferred: the checked-in setup script.** `scripts/github_repo_setup.py` holds the intended
state as constants (`DESCRIPTION`, `HOMEPAGE`, `TOPICS`, `FEATURES`, `REQUIRED_CHECKS`) and is
therefore the single source of truth. It compares, prints and applies:

```bash
export GITHUB_TOKEN=ghp_...          # scopes: repo (or public_repo)

make github-show                     # print the live repository settings
python scripts/github_repo_setup.py --dry-run     # show the intended state, call nothing
make github-setup                    # PATCH the About box + PUT the topics
python scripts/github_repo_setup.py --protect-main   # also apply branch protection
```

The raw `curl` equivalents are below, for when the script cannot run (no Python, or a token
kept outside the environment). Set `GITHUB_TOKEN` to a token with the `repo` (or fine-grained
`Administration: write` and `Contents: read`) scope, and keep the payloads identical to the
script's constants so the two do not drift.

```bash
export GITHUB_TOKEN=ghp_...

# About: description, website, feature toggles.
curl -sS -X PATCH \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/SergeyGer/InSilicoTrial_MAS \
  -d '{
        "description": "Multi-agent in-silico clinical trial simulation on Databricks & AWS: synthetic patient personas (PK/PD + ML + LLM), Spark/Delta/MLflow pipeline, biostatistical readout, DSMB rules.",
        "homepage": "https://github.com/SergeyGer/InSilicoTrial_MAS/tree/main/docs",
        "has_issues": true,
        "has_discussions": true,
        "has_wiki": false,
        "has_projects": false
      }'

# Topics (the endpoint replaces the whole set).
curl -sS -X PUT \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/SergeyGer/InSilicoTrial_MAS/topics \
  -d '{"names":["clinical-trials","in-silico-trials","multi-agent-systems","synthetic-data","digital-twins","pharmacokinetics","pharmacodynamics","databricks","apache-spark","delta-lake","mlflow","unity-catalog","aws","terraform","langchain","python"]}'
```

Three things the API cannot do here:

- **The social preview image must be uploaded manually** in the UI (section 3).
- If the `has_discussions` field is rejected by the API version you are on, enable
  Discussions in the UI, or use GraphQL:
  `gh api graphql -f query='mutation { updateRepository(input: {repositoryId: "<REPO_NODE_ID>", hasDiscussionsEnabled: true}) { repository { hasDiscussionsEnabled } } }'`
  where the node id comes from `gh api repos/SergeyGer/InSilicoTrial_MAS --jq .node_id`.
- The homepage above is the value of the `HOMEPAGE` constant in
  `scripts/github_repo_setup.py`, so the script and this document agree by construction.

Branch protection can be applied with the same token (`--protect-main` does exactly this).
The payload mirrors the script's `REQUIRED_CHECKS` (CodeQL included). Note the review policy:
a pull request is required but **zero approvals**, because GitHub does not let an author
approve their own pull request and the repository currently has one maintainer. Raise
`required_approving_review_count` to `1` and set `require_code_owner_reviews` to `true` when a
second maintainer joins - CODEOWNERS already lists the sensitive paths.

```bash
curl -sS -X PUT \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/SergeyGer/InSilicoTrial_MAS/branches/main/protection \
  -d '{
        "required_status_checks": {
          "strict": true,
          "contexts": [
            "Lint and type-check",
            "Tests (Python 3.10)",
            "Tests (Python 3.11)",
            "Tests (Python 3.12)",
            "Simulation plausibility and reproducibility",
            "Spark engine (local[*] driver)",
            "Terraform and bundle validation",
            "Analyze Python"
          ]
        },
        "enforce_admins": false,
        "required_pull_request_reviews": {
          "dismiss_stale_reviews": true,
          "require_code_owner_reviews": false,
          "required_approving_review_count": 0
        },
        "restrictions": null,
        "required_linear_history": true,
        "allow_force_pushes": false,
        "allow_deletions": false,
        "required_conversation_resolution": true
      }'
```

If the repository is private on a free plan, branch protection is unavailable; make it
public before relying on these gates.

## 9. Final pass

1. Confirm the four files another engineer owns are present and correct: `README.md`,
   `docs/architecture.svg`, `.github/workflows/ci.yml`, `.github/social-preview.png`.
2. Publish the tree with `make publish` (runs `scripts/publish_github.sh`, which refuses to
   push unless the quality gates pass); add `--setup` to apply the About box in the same run.
3. Cut the release by pushing a tag, not by hand: `git tag -a v1.0.0 -m "InSilicoTrial MAS 1.0.0"`
   and `git push origin v1.0.0`. `.github/workflows/release.yml` then builds the sdist and
   wheel, installs the wheel in a clean environment, smoke-tests `insilico-trial demo`,
   verifies the rendered diagrams and previews (`--check`) and publishes the GitHub Release
   using the `## [1.0.0]` section of `CHANGELOG.md` as the notes.
4. Check that `CITATION.cff` renders on the repository home page ("Cite this repository") -
   that confirms GitHub parsed it as valid CFF.
5. Replace the placeholder Code of Conduct contact (`opensource@example.com`) before
   announcing the repository.
6. Open the About panel and the topic list in a private window: what a clinical reviewer sees
   first must not read as a clinical-validity claim.
