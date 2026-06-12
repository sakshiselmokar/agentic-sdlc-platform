"""
git_agent.py — Git agent (v3 ticket-loop refactor).

Core change from v2:
  - Instead of using the last dev artifact's files, it reads
    state.sprint_committed_files — the accumulated, QA-validated files
    for the current sprint.
  - Writes sprint_number to artifact metadata so orchestrator knows
    which sprints have been committed.
  - After committing, advances state.current_sprint to the next sprint
    so the ticket loop can continue.
  - Still creates one commit per sprint with a meaningful message.
  - If sprint_committed_files is empty (edge case), falls back to the
    latest dev artifact as before.

Produces a git_log artifact with the commit history.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from orchestrator.state import PlatformState, Artifact

logger = logging.getLogger(__name__)

GITIGNORE = """__pycache__/
*.pyc
*.pyo
.env
*.db
*.sqlite3
.venv/
venv/
.pytest_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
"""


def git_agent_node(state: PlatformState) -> dict:
    """LangGraph node for the Git agent."""
    logger.info(
        f"[Git Agent] Committing sprint {state.current_sprint} "
        f"({len(state.sprint_committed_files)} accumulated files)"
    )

    ba_spec     = _get_artifact(state, "ba",    "spec")
    scrum_tasks = _get_artifact(state, "scrum", "tasks")

    # Prefer sprint_committed_files (QA-validated), fall back to latest dev artifact
    if state.sprint_committed_files:
        files    = state.sprint_committed_files
        source   = "sprint_committed_files"
    else:
        code_pkg = _get_code_artifact(state)
        if not code_pkg:
            return _fail(state, "No code artifact or committed files to commit")
        files  = code_pkg.get("files", [])
        source = "latest_dev_artifact"

    if not files:
        return _fail(state, "No files to commit")

    logger.info(f"[Git Agent] Using {len(files)} files from {source}")

    repo_dir = tempfile.mkdtemp(prefix="git_agent_repo_")
    logger.info(f"[Git Agent] Repo directory: {repo_dir}")

    try:
        _run(["git", "init", "-b", "main"], repo_dir)
        _run(["git", "config", "user.email", "sdlc-bot@agentic.dev"], repo_dir)
        _run(["git", "config", "user.name",  "Agentic SDLC Bot"], repo_dir)

        # Write .gitignore
        with open(os.path.join(repo_dir, ".gitignore"), "w", encoding="utf-8") as f:
            f.write(GITIGNORE)

        # Generate README
        readme = _build_readme(ba_spec, files, scrum_tasks, state.current_sprint)
        with open(os.path.join(repo_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(readme)

        # Write all code files
        _write_files(files, repo_dir)

        # Stage all
        _run(["git", "add", "-A"], repo_dir)

        # Build commit message from sprint tickets
        commit_msg = _build_sprint_commit_message(scrum_tasks, state)
        _run(["git", "commit", "-m", commit_msg], repo_dir)

        # Tag sprint release
        tag = f"sprint-{state.current_sprint}"
        _run(["git", "tag", "-a", tag, "-m", f"Sprint {state.current_sprint} release"], repo_dir)

        # If this is the last sprint, also add a v0.1.0 tag
        total_sprints = len(scrum_tasks.get("sprints", [])) if scrum_tasks else 1
        if state.current_sprint >= total_sprints:
            _run(["git", "tag", "-a", "v0.1.0", "-m", "Initial release"], repo_dir, check=False)

        # Get git log
        log_result = _run(["git", "log", "--oneline"], repo_dir)
        git_log    = log_result.stdout.strip()
        tag_result = _run(["git", "tag"], repo_dir)
        tags       = tag_result.stdout.strip()

        # List of completed ticket IDs this sprint
        sprint_done_tickets = _get_sprint_ticket_ids(scrum_tasks, state.current_sprint)
        completed_this_sprint = [
            tid for tid in state.completed_ticket_ids
            if tid in set(sprint_done_tickets)
        ]

        artifact = Artifact(
            agent="git",
            artifact_type="git_log",
            content=json.dumps({
                "sprint_number":      state.current_sprint,
                "git_log":            git_log,
                "tags":               tags,
                "commit_message":     commit_msg,
                "files_committed":    [f.get("path") for f in files],
                "tickets_in_sprint":  completed_this_sprint,
                "repo_path":          repo_dir,   # FIX: expose for github_agent
            }, indent=2),
            status="ok",
            metadata={
                "sprint_number": state.current_sprint,
                "files_committed": len(files),
                "tickets_committed": len(completed_this_sprint),
            },
        )

        logger.info(
            f"[Git Agent] Sprint {state.current_sprint} committed: "
            f"{len(files)} files, {len(completed_this_sprint)} tickets, tag={tag}"
        )

        # Advance to next sprint and clear sprint-level state
        next_sprint = state.current_sprint + 1

        return {
            "artifacts": state.artifacts + [artifact],
            "active_agent": "git",
            "current_decision": None,
            # Advance sprint counter and clear accumulated files for the next sprint
            "current_sprint": next_sprint,
            "sprint_committed_files": [],        }

    except Exception as e:
        logger.error(f"[Git Agent] Error: {e}", exc_info=True)
        shutil.rmtree(repo_dir, ignore_errors=True)  # safe to delete on failure
        return _fail(state, str(e))
    # NOTE: do NOT rmtree repo_dir on success — github_agent needs it.
    # The OS will reclaim the temp dir on process exit.


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_sprint_ticket_ids(scrum_tasks: dict | None, sprint_number: int) -> list[str]:
    if not scrum_tasks:
        return []
    for sp in scrum_tasks.get("sprints", []):
        if sp["sprint_number"] == sprint_number:
            return sp.get("ticket_ids", [])
    return []


def _build_sprint_commit_message(scrum_tasks: dict | None, state: PlatformState) -> str:
    """Build a meaningful commit message listing the tickets in this sprint."""
    sprint_number = state.current_sprint

    if not scrum_tasks:
        return f"feat: Sprint {sprint_number} — automated SDLC commit"

    # Get sprint goal
    sprint_goal = ""
    sprint_ids  = []
    for sp in scrum_tasks.get("sprints", []):
        if sp["sprint_number"] == sprint_number:
            sprint_goal = sp.get("goal", "")
            sprint_ids  = sp.get("ticket_ids", [])
            break

    completed_this_sprint = [tid for tid in state.completed_ticket_ids if tid in set(sprint_ids)]

    lines = [f"feat: Sprint {sprint_number} — {sprint_goal}" if sprint_goal
             else f"feat: Sprint {sprint_number}"]

    if completed_this_sprint:
        lines.append("")
        lines.append("Tickets completed:")
        id_to_ticket = {t["id"]: t for t in scrum_tasks.get("tickets", [])}
        for tid in completed_this_sprint:
            title = id_to_ticket.get(tid, {}).get("title", tid)
            lines.append(f"  - {tid}: {title}")

    lines.append("")
    lines.append("Generated by Agentic SDLC Platform")
    return "\n".join(lines)


def _run(cmd: list, cwd: str, check: bool = True):
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command {cmd} failed: {result.stderr}")
    return result


def _write_files(files: list, repo_dir: str):
    for f in files:
        path    = f.get("path", "")
        content = f.get("content", "")
        if not path:
            continue
        if "\\n" in content and "\n" not in content.replace("\\n", ""):
            content = content.replace("\\n", "\n")
        content   = content.replace("\\t", "\t")
        full_path = os.path.join(repo_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fp:
            fp.write(content)


def _build_readme(
    ba_spec: dict | None,
    files: list,
    scrum_tasks: dict | None,
    current_sprint: int,
) -> str:
    if not ba_spec:
        return "# Project\n\nAuto-generated by Agentic SDLC Platform.\n"

    title   = ba_spec.get("project_title", "Project")
    summary = ba_spec.get("summary", "")
    tech    = ba_spec.get("tech_stack_hints", [])

    # Find run/test commands from files
    run_cmd  = "uvicorn main:app --reload"
    test_cmd = "pytest tests/ -v"

    stories = ba_spec.get("user_stories", [])
    story_lines = [
        f"- **{us['id']}**: As a {us['role']}, I want to {us['goal']}"
        for us in stories
    ]

    lines = [
        f"# {title}",
        "",
        f"> {summary}",
        "",
        "## Auto-generated by Agentic SDLC Platform",
        "",
        "## Quick Start",
        "",
        "```bash",
        "pip install -r requirements.txt",
        run_cmd,
        "```",
        "",
        "## Run Tests",
        "",
        "```bash",
        test_cmd,
        "```",
        "",
    ]

    if story_lines:
        lines += ["## Features", ""] + story_lines + [""]

    if tech:
        lines += ["## Tech Stack", ""] + [f"- {t}" for t in tech] + [""]

    if scrum_tasks:
        sprints = scrum_tasks.get("sprints", [])
        if sprints:
            lines += ["## Sprint Progress", ""]
            for sp in sprints:
                done_marker = " [done]" if sp["sprint_number"] <= current_sprint else ""
                lines.append(
                    f"**Sprint {sp['sprint_number']}**{done_marker} — {sp['goal']}"
                )
            lines.append("")

    return "\n".join(lines)


def _get_code_artifact(state: PlatformState) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == "developer" and a.artifact_type == "code" and a.status == "ok":
            try:
                return json.loads(a.content)
            except Exception:
                return None
    return None


def _get_artifact(state: PlatformState, agent: str, artifact_type: str) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == agent and a.artifact_type == artifact_type and a.status == "ok":
            try:
                return json.loads(a.content)
            except Exception:
                return None
    return None


def _fail(state: PlatformState, reason: str) -> dict:
    logger.error(f"[Git Agent] {reason}")
    return {
        "artifacts": state.artifacts + [Artifact(
            agent="git",
            artifact_type="git_log",
            content=json.dumps({"error": reason, "git_log": "", "commits": []}),
            status="fail",
            metadata={
                "error": reason,
                "sprint_number": state.current_sprint,
            },
        )],
        "active_agent": "git",
        "current_decision": None,
    }
