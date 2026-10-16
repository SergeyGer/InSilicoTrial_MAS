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
| Init process | `init: true` runs tini as PID 1, so processes that VS Code, terminals and test runs leave behind are reaped instead of piling up as zombies |
| Healthcheck | none: the Studio probe belongs to the `runtime` image, and a dev container that never serves HTTP must not report itself unhealthy |

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

## One developer, several laptops

Everything that matters lives on the server — the clone, the containers, the results
volume, the DeepSeek Harness state — so a second laptop is a *client*, not a second
environment.

| Item | Where it lives | Per laptop? |
| --- | --- | --- |
| Source code | the clone on the server | no — both laptops open the same folder |
| Containers, volumes, ports | the server's Docker | no |
| `COMPOSE_PROJECT_NAME`, `INSILICO_STUDIO_PORT` | `.env` on the server | no |
| Git identity and branches | the server's `~/.gitconfig` and the clone | no |
| SSH key, VS Code, extensions | the laptop | **yes** — the only per-laptop setup |

Adding a laptop takes about ten minutes:

1. Generate a key **on that laptop**
   (`ssh-keygen -t ed25519 -C "laptop-home-2026-10"` - the comment names the device)
   and append its public part to `~/.ssh/authorized_keys` on the server. One key per
   device can be revoked on its own; `scripts/ssh_keys.sh list|add|revoke|check` does it
   and prints the fingerprint-to-device table.
2. Copy the `Host insilico` block into `%USERPROFILE%\.ssh\config`, keep
   `ForwardAgent yes`.
3. Install VS Code and the extensions this repository recommends
   (`.vscode/extensions.json` prompts automatically: Remote - SSH, Dev Containers,
   Python, Ruff, Docker). Turning on Settings Sync with your Microsoft or GitHub
   account keeps both laptops identical without copying files.
4. Connect, open the same server folder, `Dev Containers: Reopen in Container`.

Leaving one laptop and continuing on the other:

```bash
# before you walk away
git status                 # commit or push what you want to keep
git push -u origin HEAD    # the branch is now visible from the second laptop
bash scripts/project.sh stop   # optional: containers are not needed while you are away

# on the other laptop
git pull --ff-only
make check
```

Uncommitted work exists only on the machine you left, so commit to a work-in-progress
branch before switching. Both laptops may be attached to the same dev container at the
same time — they share the same files, so avoid running two heavy builds at once.

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

## Reaching DeepSeek Harness (port 3080) from a container window

DeepSeek Harness listens on the **host** loopback (`127.0.0.1:3080`). A dev container
runs in its own network namespace, so forwarding 3080 in a container window points at
the container's loopback, where nothing is listening. Measured in this repository:

| Probe from inside the dev container | Bridge network (default) | `network_mode: host` |
| --- | --- | --- |
| `127.0.0.1:3080` | `ConnectionRefused` | reachable |
| `172.17.0.1:3080` (docker gateway) | `Timeout` | not applicable |

Three ways to get to the UI, in order of convenience:

1. **SSH tunnel from the laptop** - independent of VS Code mode, works always:

   ```bash
   ssh -N -L 3080:127.0.0.1:3080 root@<server>     # then open http://localhost:3080
   ```

2. **Host networking for the dev container** - uncomment `network_mode: host` in
   `docker-compose.devcontainer.yml` and rebuild; VS Code's Ports panel then forwards
   the host's port, and `http://localhost:3080` works from the container window too.

3. **Forward it from a host window instead** - open the folder with Remote-SSH (not
   "Reopen in Container") in a second window and forward 3080 there.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Reopen in Container" is missing | Install the **Dev Containers** extension (`ms-vscode-remote.remote-containers`) and make sure Docker is running |
| The container builds but the workspace looks empty | The repository must be opened as the workspace folder; the compose override mounts `./` at `/app` |
| Files are not writable | `updateRemoteUserUID` is enabled; rebuild the container so the user id matches your account |
| `import insilico_trial_mas` fails | Run `pip install -e ".[dev]"` in the container terminal |
| Spark tests are skipped | They need a JVM: rebuild with the `spark` service, or run `bash scripts/bootstrap.sh --spark` on the host |
| Port 8765 forwarded to the wrong stack | Two developers share the port: set `INSILICO_STUDIO_PORT` per developer in `.env` |
| DeepSeek Harness unreachable from the browser | It listens on the host loopback; forward it from a host window or use an SSH tunnel (section above) |
| `ssh: Could not resolve hostname` in a tunnel command | The documentation used `server.example.com` and `alice` as placeholders: substitute your real host and user |
