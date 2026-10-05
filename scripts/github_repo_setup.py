"""Apply the repository settings that GitHub only exposes through its API.

The About box (description, website, topics) and the branch protection rules are
not versioned anywhere by default, which means they drift and cannot be reviewed.
This script makes them reproducible: it reads the intended state from constants
below, compares it with the live repository, and applies the difference.

What it cannot do: the **social preview image** has no API. Upload
``.github/social-preview.png`` manually in *Settings → Social preview* (GitHub
renders Open Graph cards from it).

Usage::

    export GITHUB_TOKEN=ghp_...            # scopes: repo (or public_repo), read:org
    python scripts/github_repo_setup.py --dry-run
    python scripts/github_repo_setup.py
    python scripts/github_repo_setup.py --protect-main     # adds branch protection
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

API = "https://api.github.com"

REPO = "SergeyGer/InSilicoTrial_MAS"

#: About box. GitHub truncates the description at 350 characters.
DESCRIPTION = (
    "Multi-agent in-silico clinical trial simulation on Databricks & AWS: synthetic patient "
    "personas (PK/PD + ML + LLM), Spark/Delta/MLflow pipeline, biostatistical readout, DSMB rules."
)

#: GitHub renders this in the About box; point it at the documentation tree.
HOMEPAGE = "https://github.com/SergeyGer/InSilicoTrial_MAS/tree/main/docs"

TOPICS: tuple[str, ...] = (
    "clinical-trials",
    "in-silico-trials",
    "multi-agent-systems",
    "synthetic-data",
    "digital-twins",
    "pharmacokinetics",
    "pharmacodynamics",
    "databricks",
    "apache-spark",
    "delta-lake",
    "mlflow",
    "unity-catalog",
    "aws",
    "terraform",
    "langchain",
    "python",
)

FEATURES = {
    "has_issues": True,
    "has_discussions": True,
    "has_wiki": False,
    "has_projects": False,
    "has_downloads": True,
}

#: Status checks that must pass before `main` can be merged into.
REQUIRED_CHECKS: tuple[str, ...] = (
    "Lint and type-check",
    "Analyze Python",
    "Tests (Python 3.10)",
    "Tests (Python 3.11)",
    "Tests (Python 3.12)",
    "Simulation plausibility and reproducibility",
    "Docker image and compose stack",
    "Spark engine (local[*] driver)",
    "Terraform and bundle validation",
)


def request(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    """Call the GitHub REST API and return ``(status, parsed_body)``."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "insilico-trial-mas-repo-setup",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = {"message": detail}
        return exc.code, parsed


def current_state(token: str) -> dict[str, Any]:
    status, body = request("GET", f"/repos/{REPO}", token)
    if status != 200:
        raise SystemExit(f"cannot read {REPO}: HTTP {status} {body}")
    return body


def apply_about(token: str, *, dry_run: bool) -> None:
    payload = {"description": DESCRIPTION[:350], "homepage": HOMEPAGE, **FEATURES}
    print("About box:")
    print(json.dumps(payload, indent=2))
    if dry_run:
        return
    status, body = request("PATCH", f"/repos/{REPO}", token, payload)
    print(f"  PATCH /repos/{REPO} -> {status}" + ("" if status < 300 else f" {body}"))

    topic_status, topic_body = request("PUT", f"/repos/{REPO}/topics", token, {"names": list(TOPICS)})
    print(f"  PUT /repos/{REPO}/topics -> {topic_status}")
    if topic_status >= 300:
        print(f"  topics rejected: {topic_body}")
    else:
        print(f"  topics: {', '.join(topic_body.get('names', []))}")


def apply_branch_protection(token: str, *, dry_run: bool) -> None:
    payload = {
        "required_status_checks": {"strict": True, "contexts": list(REQUIRED_CHECKS)},
        "enforce_admins": False,
        # A pull request is required, but zero approvals: the repository has a
        # single maintainer, and GitHub does not let an author approve their own
        # PR. Raise `required_approving_review_count` to 1 and flip
        # `require_code_owner_reviews` to True as soon as a second maintainer
        # joins (CODEOWNERS already lists the sensitive paths).
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
        },
        "restrictions": None,
        "required_linear_history": True,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "required_conversation_resolution": True,
    }
    print(f"Branch protection for main (required checks: {len(REQUIRED_CHECKS)}):")
    print(json.dumps(payload["required_status_checks"], indent=2))
    if dry_run:
        return
    status, body = request("PUT", f"/repos/{REPO}/branches/main/protection", token, payload)
    if status < 300:
        print(f"  PUT /repos/{REPO}/branches/main/protection -> {status}")
        return
    print(f"  branch protection rejected (HTTP {status}): {body.get('message')}")
    print("  Note: status checks must have run at least once, and private repositories need a paid plan.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the GitHub About box, topics and branch protection")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="environment variable holding the token")
    parser.add_argument("--dry-run", action="store_true", help="print the intended state without calling the API")
    parser.add_argument("--protect-main", action="store_true", help="also configure branch protection for main")
    parser.add_argument("--show", action="store_true", help="print the live repository state and exit")
    args = parser.parse_args()

    token = os.environ.get(args.token_env) or os.environ.get("GH_TOKEN", "")
    if not token and not args.dry_run:
        print(
            f"no token found: export {args.token_env}=<personal access token with 'repo' scope>",
            file=sys.stderr,
        )
        return 2

    if args.show:
        state = current_state(token)
        keep = ("full_name", "description", "homepage", "topics", "default_branch", "has_issues", "has_discussions")
        print(json.dumps({key: state.get(key) for key in keep}, indent=2))
        return 0

    apply_about(token, dry_run=args.dry_run)
    if args.protect_main:
        apply_branch_protection(token, dry_run=args.dry_run)

    print("")
    print("Manual step (no API exists): Settings -> Social preview -> upload .github/social-preview.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
