# Running InSilicoTrial MAS in Docker

The container wraps the whole platform — the CLI, the Spark engine hook, the report
templates and the example protocols — into one image that needs no Python, no JVM
and no credentials on the host. It is the fastest way to try the platform, and it
is what CI builds on every pull request, so the image cannot silently rot.

```bash
docker compose up -d studio     # http://localhost:8765
docker compose run --rm demo    # one trial, results in the `artifacts` volume
```

---

## 1. What is in the image

| Target | Size | Contents | Use it for |
| --- | --- | --- | --- |
| `runtime` (default) | 647 MB | Python 3.12 slim, the package in its own virtual environment, `conf/` profiles, non-root user | CLI runs, the Studio UI, CI smoke tests |
| `spark` | 1.63 GB | `runtime` + headless OpenJDK 17 + PySpark/Delta extras | Reproducing the Databricks engine locally |
| `dev` | 904 MB | `runtime` + dev tooling, the test suite and the linters | Interactive shell, running tests against mounted sources |
| `test` | 904 MB | `dev` + pytest/ruff/mypy executed **during the build** | A build-time quality gate |

```bash
docker build -t insilico-trial-mas .                      # runtime
docker build --target spark -t insilico-trial-mas:spark .
docker build --target dev   -t insilico-trial-mas:dev   .
docker build --target test  .                             # fails if a test fails
```

Facts worth knowing about the default image:

* runs as **uid/gid 10001** (`insilico`), never as root;
* `ENTRYPOINT` is `insilico-trial`, so `docker run … demo --patients 200` works
  without spelling out the module path;
* all writable state is on the `/data` volume; the rest of the filesystem can be
  mounted read-only;
* the LLM provider defaults to **offline** (`INSILICO_LLM__PROVIDER=offline`), so a
  fresh container produces a complete, reproducible trial with no network access;
* `HEALTHCHECK` polls `GET /api/config`, which is why `docker compose ps` reports
  the Studio as *healthy* rather than merely *up*.

Optional extras are a build argument rather than a baked-in decision:

```bash
docker build --build-arg EXTRAS=llm -t insilico-trial-mas:bedrock .   # langchain-aws + boto3
docker build --build-arg EXTRAS=llm,ml -t insilico-trial-mas:full .   # + scikit-learn/xgboost
```

---

## 2. Quick start

### Compose (recommended)

```bash
git clone https://github.com/SergeyGer/InSilicoTrial_MAS.git
cd InSilicoTrial_MAS

docker compose up -d studio          # Studio UI on http://localhost:8765
docker compose run --rm demo         # small end-to-end trial
docker compose logs -f studio        # follow the log
docker compose down                  # stop (add -v to drop the results volume)
```

`docker compose up` on its own starts the Studio **and** completes one demo trial,
because the `demo` service is a one-shot job that exits when the report is written.

### Plain Docker, no Compose

```bash
docker build -t insilico-trial-mas .

# a full trial; results land in ./artifacts/docker on the host
mkdir -p artifacts/docker
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/artifacts/docker:/data/artifacts" \
  insilico-trial-mas demo --patients 400 --epochs 6

# the live Studio UI
docker run --rm -p 8765:8765 \
  -v insilico-artifacts:/data/artifacts \
  insilico-trial-mas studio --host 0.0.0.0 --port 8765 --no-browser \
      --config /app/conf/simulation_local.yaml
```

Then open <http://localhost:8765>.

---

## 3. Results, volumes and file ownership

The image writes to `/data/artifacts` (`INSILICO_OUTPUT_DIR`) and declares `/data`
as a volume.

| Approach | Command | Notes |
| --- | --- | --- |
| Named volume (default) | `docker compose up -d studio` | Docker creates it with the right ownership; no permission friction |
| Copy results to the host | `docker compose cp studio:/data/artifacts ./artifacts` | Works even while the container runs |
| Bind mount | add `--user "$(id -u):$(id -g)"` and `-v "$PWD/artifacts:/data/artifacts"` | Match the ids, otherwise the container user cannot write into a host directory it does not own |
| Inspect the volume | `docker run --rm -v insilico-trial-mas_artifacts:/data alpine ls -R /data` | Useful when you do not want to copy anything out |

The `--user` flag is the whole trick for bind mounts: the container runs as uid
10001 by default, and a host directory created by your own account is usually owned
by uid 1000. Running the container with your ids removes the mismatch without
weakening the image.

Every run writes a self-contained readout, so the results are portable:

```
/data/artifacts/demo/RUN-…/
  report/trial_report.md, .html          # narrative report
  report/dashboard.html                  # single-file interactive dashboard
  report/cdisc/*.csv                     # CDISC-inspired exports
  manifest.json                          # provenance, digests, replay command
  lake/{bronze,silver,gold}/…            # versioned Parquet tables
```

---

## 4. Configuration and credentials

Configuration is identical to a native install — YAML profiles plus `INSILICO_*`
overrides — and the image already points `--config` at `/app/conf`:

```bash
docker run --rm \
  -e INSILICO_N_PATIENTS=10000 \
  -e INSILICO_ENGINE__BACKEND=local \
  -e INSILICO_LLM_MODE=all \
  -v insilico-artifacts:/data/artifacts \
  insilico-trial-mas simulate --config /app/conf/simulation_local.yaml
```

Provider credentials are **never** baked into the image. Compose injects a `.env`
file when one exists (`env_file: required: false`), which dovetails with the
template shipped in the repository:

```bash
cp .env.example .env      # then fill in what you need
docker compose up -d studio
```

For a single run, pass variables explicitly — the standard SDK chains apply:

```bash
# Amazon Bedrock personas: use the instance role, a profile, or static keys
docker run --rm --build-arg EXTRAS=llm \
  -e INSILICO_LLM__PROVIDER=bedrock \
  -e INSILICO_LLM__MODEL=us.anthropic.claude-3-5-sonnet-20241022-v2:0 \
  -e AWS_REGION=us-east-1 -e AWS_PROFILE=default \
  -v "$HOME/.aws:/home/insilico/.aws:ro" \
  insilico-trial-mas demo --patients 200
```

`insilico-trial env-check` inside the container prints which credential chains it
can see — presence only, never a value.

---

## 5. The Spark image

The `spark` target is the same agent code with a JVM and the Spark extras, which is
what the Databricks engine needs locally:

```bash
docker compose --profile spark run --rm spark        # 2000 patients, local[*]
```

```bash
docker build --target spark -t insilico-trial-mas:spark .
docker run --rm -v insilico-artifacts:/data/artifacts insilico-trial-mas:spark \
  simulate --config /app/conf/simulation_local.yaml --patients 2000 --engine spark
```

The Databricks-oriented profile (`simulation_cluster.yaml`) also points
`ml.model_path` at `/dbfs/...`. Outside a workspace that path is not writable, so
the trained head falls back to `<output_dir>/models` with a warning naming both
paths — a simulation is not lost because a model cache could not be written.

Sizing is controlled from the environment: `SPARK_MASTER` (the standard Spark
variable — `local[*]` by default inside the image, or a cluster URL),
`INSILICO_ENGINE__SPARK_SHUFFLE_PARTITIONS`, `INSILICO_ENGINE__MAX_WORKERS`,
`INSILICO_ENGINE__PARTITION_MODE` (`cohort` or `balanced`) and
`INSILICO_ENGINE__SINGLE_NODE` for a Community Edition style single-node driver.
The JVM inherits the container's memory limit, so `docker run --memory 8g` behaves
as you would expect.

The engines are verified to produce **bit-identical** results
(`make test-spark`), so a container run can be compared against a laptop run
directly.

---

## 6. Development and CI

```bash
docker compose --profile tools run --rm dev     # shell, sources mounted read-only
# inside: make check, make test-spark, insilico-trial …
```

The `test` target turns the quality gates into a build step, which is how CI uses
it — a failing test means a failing image:

```bash
docker build --target test .        # pytest -m "not spark" + ruff + mypy
```

### Publishing

Tagged releases build the runtime and Spark images and push them to GitHub
Container Registry (`.github/workflows/release.yml`):

```bash
docker pull ghcr.io/sergeyger/insilico-trial-mas:1.0.0
docker pull ghcr.io/sergeyger/insilico-trial-mas:spark
```

Build for another architecture with buildx:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t insilico-trial-mas .
```

The image is architecture-neutral apart from the JVM in the `spark` target, which
`openjdk-17-jre-headless` provides for both amd64 and arm64.

---

## 7. Security notes

* **Unprivileged by default.** The container runs as uid 10001 with no shell login
  and no elevated capabilities.
* **No secrets in layers.** `.dockerignore` excludes `.env`, `*.tfvars`, `*.tfstate`
  and `~/.aws`-like paths, and the build never reads a credential.
* **Read-only root filesystem.** Only `/data` and `/tmp` need to be writable:
  `docker run --rm --read-only --tmpfs /tmp -v insilico-artifacts:/data …`
* **No network required** for the default offline provider, which makes the image
  usable in an air-gapped environment.
* **Pinned base.** `python:3.12-slim-bookworm`; Dependabot watches the Docker
  ecosystem in addition to pip, Actions and Terraform.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Permission denied` writing to a bind-mounted `./artifacts` | The container user (10001) does not own the host directory. Add `--user "$(id -u):$(id -g)"`, or use the named volume. |
| `docker compose build` says *buildx isn't installed* | Compose falls back to the legacy builder, which is fine; install the `docker-buildx-plugin` for faster, cached builds. |
| Slow build or `pip` timeouts behind a proxy | Pass `--build-arg HTTP_PROXY=… --build-arg HTTPS_PROXY=…`, or add the proxy to `~/.docker/config.json`. |
| Studio unreachable on `localhost:8765` | Confirm the port mapping with `docker compose ps`; the container listens on `0.0.0.0` only when `--host 0.0.0.0` is passed (the image default is loopback inside the container). |
| Spark run is very slow on Apple Silicon | The `spark` image runs amd64 under emulation unless you build for `linux/arm64`; use `docker buildx build --platform linux/arm64`. |
| `TemplateNotFound: report.md.j2` after hacking on the image | You are running a wheel built before the templates were declared in `package-data`; rebuild — `tests/test_packaging.py` guards this. |
| Want a smaller image | Build with `--build-arg PYTHON_VERSION=3.12` and skip extras; `pyarrow` (156 MB) and `pandas` (72 MB) dominate the size and are required for the Parquet lake. |

---

## 9. Why the image is built the way it is

* **Virtual environment in its own layer** (`builder` → `runtime`): no compiler,
  no build headers and no pip cache reach the runtime image, and the dependency
  layer is cached until `pyproject.toml` changes.
* **Assets are packaged, not copied**: the report templates and priors ship inside
  the wheel, so the image behaves exactly like a `pip install`. The container build
  is what exposed that `templates/*.j2` was missing from `package-data`.
* **`conf/` is copied to `/app/conf`** and the CLI resolves repository-relative
  assets against a list of candidate roots (working directory, repository root,
  `/app`), so `insilico-trial demo` works from any working directory — inside the
  image that is `/data`, not the checkout root.
* **Four targets instead of four Dockerfiles**: they share the same builder layers,
  so `--target dev` and `--target test` cost almost nothing extra.
