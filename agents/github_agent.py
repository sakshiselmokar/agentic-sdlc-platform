"""
github_agent.py — GitHub Push + PR Agent  (v5 — clean rewrite)

Runs AFTER git_agent. Takes the local repo and pushes it to GitHub.

Strategy
--------
- One shared repo:  GITHUB_REPO  (e.g. "yourname/agentic-sdlc-projects")
- One branch per run: <project-slug>-<timestamp>
- Auto-creates the repo if it does not exist yet
- Opens a PR with BA spec + QA results as description

Setup (once)
------------
1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Tick the "repo" checkbox  (full repo control)
4. Copy the token
5. In your .env:
       GITHUB_TOKEN=ghp_xxxxxxxxxxxx
       GITHUB_REPO=your-username/agentic-sdlc-projects
"""

import json
import logging
import os
import subprocess
import re
import time
from datetime import datetime

import requests
from orchestrator.state import PlatformState, Artifact

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


# ── Main node ──────────────────────────────────────────────────────────────────

def github_agent_node(state: PlatformState) -> dict:
    """LangGraph node: push generated code to GitHub and open a PR."""
    logger.info("[GitHub Agent] Starting push...")

    # ── Validate config ────────────────────────────────────────────────────────
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo_full = os.getenv("GITHUB_REPO", "").strip()

    if not token:
        return _fail(state,
            "GITHUB_TOKEN is not set. "
            "Fix: https://github.com/settings/tokens → New classic token → tick 'repo' → "
            "add GITHUB_TOKEN=ghp_... to your .env file."
        )
    if not repo_full or "/" not in repo_full:
        return _fail(state,
            "GITHUB_REPO is not set or invalid. "
            "Set GITHUB_REPO=your-username/repo-name in your .env file."
        )

    owner, repo_name = repo_full.split("/", 1)
    headers = {
        "Authorization": f"token {token}",   # works for both classic and fine-grained PATs
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # ── Pull artifacts from state ──────────────────────────────────────────────
    git_report  = _get_artifact(state, "git",       "git_log")
    ba_spec     = _get_artifact(state, "ba",        "spec")
    qa_report   = _get_artifact(state, "qa",        "test_report")
    scrum_plan  = _get_artifact(state, "scrum",     "tasks")
    code_pkg    = _get_artifact(state, "developer", "code")

    if not git_report:
        return _fail(state, "No git artifact found — git_agent must run before github_agent.")

    repo_path = git_report.get("repo_path")
    if not repo_path or not os.path.isdir(repo_path):
        return _fail(state, f"Local git repo path missing or deleted: {repo_path}")

    # ── Verify token identity (cheap call — helps with clear error messages) ───
    whoami = _api_get(headers, f"{GITHUB_API}/user")
    if whoami is None:
        return _fail(state,
            "GitHub token is invalid or expired. "
            "Regenerate at https://github.com/settings/tokens and update GITHUB_TOKEN in .env."
        )
    token_user = whoami.get("login", "unknown")
    logger.info(f"[GitHub Agent] Authenticated as: {token_user}")

    # ── Ensure repo exists (create if not) ────────────────────────────────────
    repo_ok, err = _ensure_repo(owner, repo_name, headers, token_user)
    if not repo_ok:
        return _fail(state, err)

    # ── Build branch name ──────────────────────────────────────────────────────
    project_title = (ba_spec or {}).get("project_title", "project")
    slug          = _slugify(project_title)
    timestamp     = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    branch_name   = f"{slug}-{timestamp}"

    # ── Get default branch so we know what to PR into ─────────────────────────
    repo_info = _api_get(headers, f"{GITHUB_API}/repos/{owner}/{repo_name}")
    if repo_info is None:
        return _fail(state, f"Could not read repo metadata for {owner}/{repo_name}")
    default_branch = repo_info.get("default_branch", "main")

    # ── Configure remote and push ──────────────────────────────────────────────
    remote_url = f"https://x-access-token:{token}@github.com/{owner}/{repo_name}.git"

    # Set or update the remote (safe on retries)
    try:
        _git(["git", "remote", "add", "origin", remote_url], repo_path)
    except Exception:
        _git(["git", "remote", "set-url", "origin", remote_url], repo_path)

    # Create the branch locally
    try:
        _git(["git", "checkout", "-b", branch_name], repo_path)
    except Exception as e:
        # Branch might already exist on a retry
        if "already exists" not in str(e).lower():
            return _fail(state, f"Could not create branch '{branch_name}': {e}")

    # Push — if the token lacks `workflow` scope, GitHub rejects pushes that include
    # .github/workflows/ files. Detect this, strip those files, recommit, and retry
    # once rather than failing the whole pipeline over a token scope issue.
    def _do_push(branch: str) -> tuple[bool, str]:
        try:
            _git(["git", "push", "origin", branch, "--force"], repo_path, timeout=90)
            return True, ""
        except Exception as e:
            return False, str(e)

    push_ok, push_err = _do_push(branch_name)

    if not push_ok and "workflow" in push_err.lower():
        logger.warning(
            "[GitHub Agent] Push rejected: token missing 'workflow' scope. "
            "Removing .github/workflows/ and retrying. "
            "To fix permanently: add the 'workflow' scope to your GITHUB_TOKEN."
        )
        import glob as _glob
        workflows_dir = os.path.join(repo_path, ".github", "workflows")
        if os.path.isdir(workflows_dir):
            import shutil
            shutil.rmtree(workflows_dir)
            try:
                _git(["git", "add", "-A"], repo_path)
                _git(["git", "commit", "--allow-empty", "-m", "ci: remove workflow files (token scope)"], repo_path)
            except Exception:
                pass
        push_ok, push_err = _do_push(branch_name)

    if not push_ok:
        err_str = push_err
        if any(k in err_str.lower() for k in ("403", "forbidden", "authentication", "authorization")):
            return _fail(state,
                f"Push rejected (403 Forbidden). Your token needs the 'repo' scope. "
                f"Go to https://github.com/settings/tokens → find your token → edit → "
                f"tick the 'repo' checkbox → regenerate → update GITHUB_TOKEN in .env. "
                f"Raw error: {err_str[:300]}"
            )
        if any(k in err_str.lower() for k in ("404", "not found", "repository not found")):
            return _fail(state,
                f"Repository '{owner}/{repo_name}' not found on GitHub. "
                f"Check GITHUB_REPO in your .env — format must be 'username/repo-name'. "
                f"Raw error: {err_str[:300]}"
            )
        return _fail(state, f"git push failed: {err_str[:400]}")

    logger.info(f"[GitHub Agent] Pushed branch: {branch_name}")

    # ── Open Pull Request ──────────────────────────────────────────────────────
    pr_body = _build_pr_body(ba_spec, qa_report, scrum_plan, code_pkg, git_report)
    pr_url, pr_number = None, None

    try:
        resp = requests.post(
            f"{GITHUB_API}/repos/{owner}/{repo_name}/pulls",
            headers=headers,
            json={
                "title": f"feat: {project_title} — auto-generated by Agentic SDLC",
                "head":  branch_name,
                "base":  default_branch,
                "body":  pr_body,
            },
            timeout=20,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            pr_url    = data["html_url"]
            pr_number = data["number"]
            logger.info(f"[GitHub Agent] PR #{pr_number} opened: {pr_url}")
        elif resp.status_code == 422:
            # Common: "A pull request already exists for this branch" — not an error
            logger.warning("[GitHub Agent] PR already exists for this branch (422) — skipping PR creation")
            pr_url = f"https://github.com/{owner}/{repo_name}/tree/{branch_name}"
        else:
            logger.error(f"[GitHub Agent] PR creation failed {resp.status_code}: {resp.text[:200]}")
            pr_url = f"https://github.com/{owner}/{repo_name}/tree/{branch_name}"
    except Exception as e:
        logger.error(f"[GitHub Agent] PR creation exception: {e}")
        pr_url = f"https://github.com/{owner}/{repo_name}/tree/{branch_name}"

    # ── Return success artifact ────────────────────────────────────────────────
    commit_count = git_report.get("commit_count", 0)
    if not isinstance(commit_count, int):
        commit_count = len(git_report.get("commits", []))

    report = {
        "repo_url":    f"https://github.com/{owner}/{repo_name}",
        "branch":      branch_name,
        "branch_url":  f"https://github.com/{owner}/{repo_name}/tree/{branch_name}",
        "pr_url":      pr_url,
        "pr_number":   pr_number,
        "project_title": project_title,
        "commit_count": commit_count,
        "file_count":   git_report.get("file_count", 0),
    }

    logger.info(f"[GitHub Agent] ✓ Push complete → {report['branch_url']}")

    return {
        "artifacts": state.artifacts + [Artifact(
            agent="github",
            artifact_type="github_push",
            content=json.dumps(report, indent=2),
            status="ok",
            metadata={
                "pr_url":   pr_url,
                "branch":   branch_name,
                "repo_url": report["repo_url"],
            },
        )],
        "active_agent":    "github",
        "current_decision": None,
    }


# ── Repo management ────────────────────────────────────────────────────────────

def _ensure_repo(owner: str, repo_name: str, headers: dict, token_user: str) -> tuple[bool, str]:
    """
    Make sure the remote repo exists.
    Returns (True, "") on success, (False, error_message) on failure.
    """
    check = requests.get(f"{GITHUB_API}/repos/{owner}/{repo_name}", headers=headers, timeout=15)

    if check.status_code == 200:
        logger.info(f"[GitHub Agent] Repo already exists: {owner}/{repo_name}")
        return True, ""

    if check.status_code == 403:
        return False, (
            f"Token cannot access '{owner}/{repo_name}' (403 Forbidden). "
            f"Your token needs the 'repo' scope. "
            f"Fix: https://github.com/settings/tokens → your token → edit → tick 'repo' → save."
        )

    if check.status_code == 404:
        # Repo doesn't exist — try to create it
        logger.info(f"[GitHub Agent] Repo not found — creating: {owner}/{repo_name}")

        # If pushing to another user's namespace, we can't create it
        if owner.lower() != token_user.lower():
            return False, (
                f"Repo '{owner}/{repo_name}' does not exist and cannot be auto-created because "
                f"the token belongs to '{token_user}', not '{owner}'. "
                f"Either create the repo manually on GitHub, or set GITHUB_REPO={token_user}/{repo_name} "
                f"in your .env to use your own namespace."
            )

        create_resp = requests.post(
            f"{GITHUB_API}/user/repos",
            headers=headers,
            json={
                "name":        repo_name,
                "description": "🤖 Auto-generated by Agentic SDLC Platform",
                "private":     False,
                "auto_init":   True,   # creates main branch so push has a base
            },
            timeout=20,
        )

        if create_resp.status_code in (200, 201):
            logger.info(f"[GitHub Agent] Repo created: {owner}/{repo_name}")
            time.sleep(3)  # GitHub needs a moment to initialise the default branch
            return True, ""

        if create_resp.status_code == 403:
            return False, (
                f"Token cannot create repos (403 Forbidden). "
                f"\n\nIf you are using a CLASSIC token: "
                f"https://github.com/settings/tokens → your token → edit → tick the 'repo' checkbox → Save. "
                f"\nIf you are using a FINE-GRAINED token: "
                f"https://github.com/settings/tokens → your token → edit → "
                f"under 'Repository permissions' set 'Administration' to Read & Write, "
                f"and 'Contents' to Read & Write → Save. "
                f"\nEasiest fix: generate a new CLASSIC token with full 'repo' scope and update GITHUB_TOKEN in .env."
            )

        if create_resp.status_code == 422:
            # Repo already exists (race condition) — that's fine
            logger.info(f"[GitHub Agent] Repo appeared mid-check (422) — treating as exists")
            time.sleep(1)
            return True, ""

        return False, (
            f"Failed to create repo '{owner}/{repo_name}': "
            f"GitHub API returned {create_resp.status_code}: {create_resp.text[:200]}"
        )

    return False, f"Unexpected GitHub API status {check.status_code} checking repo: {check.text[:200]}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _api_get(headers: dict, url: str) -> dict | None:
    """GET with auth; returns parsed JSON or None on error."""
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"[GitHub Agent] GET {url} → {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"[GitHub Agent] GET {url} exception: {e}")
        return None


def _git(cmd: list, cwd: str, timeout: int = 30):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr[:400]}")
    return result


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:40].strip("-")


def _get_artifact(state: PlatformState, agent: str, artifact_type: str) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == agent and a.artifact_type == artifact_type and a.status == "ok":
            try:
                return json.loads(a.content)
            except json.JSONDecodeError:
                return None
    return None


def _fail(state: PlatformState, reason: str) -> dict:
    logger.error(f"[GitHub Agent] FAIL: {reason}")
    return {
        "artifacts": state.artifacts + [Artifact(
            agent="github",
            artifact_type="github_push",
            content=json.dumps({"error": reason}),
            status="fail",
            metadata={"error": reason},
        )],
        "active_agent":    "github",
        "current_decision": None,
    }


# ── PR body ────────────────────────────────────────────────────────────────────

def _build_pr_body(ba_spec, qa_report, scrum_plan, code_pkg, git_report) -> str:
    lines = ["## 🤖 Auto-generated by Agentic SDLC Platform", ""]

    if ba_spec:
        lines += [
            f"### 📋 {ba_spec.get('project_title', 'Project')}",
            f"> {ba_spec.get('summary', '')}",
            "",
            "**Tech Stack:** " + " · ".join(ba_spec.get("tech_stack_hints", [])),
            "",
            "**User Stories:**",
        ]
        for us in ba_spec.get("user_stories", [])[:5]:
            lines.append(f"- `{us['id']}` As a {us['role']}, I want to {us['goal']}")
        lines.append("")

    if scrum_plan:
        tickets = scrum_plan.get("tickets", [])
        sprints = scrum_plan.get("sprints", [])
        lines += [f"### 🗂️ Sprint Plan — {len(tickets)} tickets across {len(sprints)} sprints", ""]
        for sp in sprints:
            lines.append(f"**Sprint {sp['sprint_number']}** — {sp.get('goal', '')}")
        lines.append("")

    if code_pkg:
        files = code_pkg.get("files", [])
        lines += [f"### 💻 Generated Code — {len(files)} files", ""]
        for f in files:
            loc = len(f.get("content", "").splitlines())
            lines.append(f"- `{f.get('path', '')}` ({loc} lines)")
        lines.append("")

    if qa_report:
        status  = qa_report.get("overall_status", "unknown").upper()
        icon    = "✅" if status == "PASS" else "❌"
        passed  = qa_report.get("tests_passed", 0)
        total   = qa_report.get("tests_run", 0)
        lines  += [f"### 🧪 QA — {icon} {status}  ({passed}/{total} passed)", ""]
        for r in qa_report.get("recommendations", [])[:3]:
            lines.append(f"- {r}")
        lines.append("")

    if git_report:
        lines += [
            f"### 🔀 Commits — {git_report.get('file_count', 0)} files",
            f"**Tag:** `{git_report.get('tag', 'v0.1.0')}`", "",
        ]
        for c in git_report.get("commits", []):
            lines.append(f"- {c.get('message', '')}")
        lines.append("")

    lines += [
        "---",
        "*Generated by [Agentic SDLC Platform](https://github.com/agentic-platform)*",
    ]
    return "\n".join(lines)