# Development container

Open this repository in VS Code, accept the prompt **"Reopen in Container"** (or run
*Dev Containers: Reopen in Container* from the command palette), and you get a Linux
environment with the package, the test tooling and the linters already installed.
Nothing is installed on your machine except Docker and the Dev Containers extension.

## What you get

| | |
| --- | --- |
| Image | the `dev` target of the project `Dockerfile` (Python 3.12, dev extras, sources, `pytest`/`ruff`/`mypy`) |
| Workspace | the whole repository mounted at `/app`, so edits are written straight to your disk |
| Version control | your host `git` configuration and credentials are used, as usual in Dev Containers |
| Python | `/opt/venv/bin/python` — the same interpreter that runs the CLI |
| Ports | `8765` is forwarded for the Studio, with a notification when it opens |
| Lifecycle | the container starts when you open the folder and stops when you close the window (`shutdownAction: stopCompose`) |

## Files

| File | Purpose |
| --- | --- |
| `devcontainer.json` | the definition: compose files, service, extensions, settings, forwarded ports |
| `docker-compose.devcontainer.yml` | override that mounts the repository, keeps the container alive and leaves the restart policy at `"no"` |
| `post-create.sh` | one-time check that prints the environment, the useful commands and anything missing |

## Everyday use

```bash
make check                 # ruff + mypy + fast tests
make test                  # pytest only
insilico-trial demo --patients 200 --epochs 3
insilico-trial studio --host 0.0.0.0 --port 8765    # open the forwarded port
```

The **project's own containers** (Studio, Spark, demo) are started and stopped from
the host, so they are never tied to the lifetime of your editor:

```bash
bash scripts/project.sh start      # Linux, macOS, WSL, Git Bash
.\scripts\project.ps1 start        # Windows PowerShell
```

## Optional: run Docker from inside the container

The default setup deliberately does **not** mount the Docker socket. If you want to
build and run the project's images from the integrated terminal as well, add this to
`devcontainer.json` and rebuild the container:

```json
"features": {
  "ghcr.io/devcontainers/features/docker-outside-of-docker:1": {}
}
```

Note what that means: anything running in the container can then control the Docker
daemon of the host, which is equivalent to root on the host. Enable it on a trusted
machine only.

## Several developers, one Linux server

The usual setup for a team: VS Code runs on a Windows laptop, the code and the
containers live on a shared Linux server.

1. On the laptop install **Remote - SSH** (`ms-vscode-remote.remote-ssh`) **and**
   Dev Containers, and add the server to `~/.ssh/config`:

   ```sshconfig
   Host insilico
       HostName server.example.com
       User alice
       IdentityFile ~/.ssh/id_ed25519
   ```

2. `F1 -> Remote-SSH: Connect to Host -> insilico`, then open **your own clone**
   (`~/work/alice/InSilicoTrial_MAS`) - not a directory shared with a colleague.
3. `F1 -> Dev Containers: Reopen in Container`. The container is built and runs **on
   the server**; the editor, the browser and the port forwarding live on the laptop.
4. Set your own namespace once, in `.env` next to the clone:

   ```dotenv
   COMPOSE_PROJECT_NAME=insilico-alice
   INSILICO_STUDIO_PORT=8765
   ```

   Containers, network and the results volume are then prefixed with your name, so
   two developers never share state. The start scripts choose a free port
   automatically when `INSILICO_STUDIO_PORT` is not set.

Closing the VS Code window stops the dev container (`shutdownAction: stopCompose`);
the project's own stack keeps running until `scripts/project.sh stop`.

## Synchronising a team

Git is the only shared source of truth; everything else is either derived or local.

| Item | How it stays in sync |
| --- | --- |
| Code | `git pull --ff-only`, feature branches, pull requests |
| Toolchain | the container image is built from the same `Dockerfile`; rebuild after it changes |
| Python dependencies | `pyproject.toml` ranges, updated by Dependabot |
| Local settings | `.env` per developer (never committed); `.env.example` is the template |
| Results | per-developer volume; share a report with `docker compose cp` |
| Reproducibility | the same seed gives identical rows on different machines (`make verify-repro`) |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Reopen in Container" is missing | Install the **Dev Containers** extension (`ms-vscode-remote.remote-containers`) and make sure Docker is running |
| The container builds but the workspace looks empty | The repository must be opened as the workspace folder; the compose override mounts `./` at `/app` |
| Files are not writable | `updateRemoteUserUID` is enabled; rebuild the container so the user id matches your account |
| `import insilico_trial_mas` fails | Run `pip install -e ".[dev]"` in the container terminal |
| Spark tests are skipped | They need a JVM: rebuild with the `spark` service, or run `bash scripts/bootstrap.sh --spark` on the host |
| Port 8765 forwarded to the wrong stack | Two developers share the port: set `INSILICO_STUDIO_PORT` per developer in `.env` |
| DeepSeek Harness unreachable from the browser | Forward its port through VS Code (Ports tab) or open an SSH tunnel - see `Run_Stop_Project.docx` |
