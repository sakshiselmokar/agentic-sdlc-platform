"""
dev_agent.py — Developer agent (v3 ticket-loop refactor).

Core change from v2:
  - Receives ONE ticket (state.current_ticket_id) instead of all tickets at once.
  - Knows about already-committed code (state.sprint_committed_files) so it can
    build on top of existing files rather than rewriting from scratch every time.
  - Writes ticket_id into the artifact's metadata so the orchestrator and
    QA agent can correlate artifacts back to their ticket.
  - On success: merges produced files into sprint_committed_files and advances
    current_ticket_id to None (letting orchestrator pick next one).
  - On skip (all attempts exhausted): marks ticket as "completed" with fail status
    and clears current_ticket_id so pipeline can continue.
"""

import json
import logging
import time
import ast
from langchain_core.messages import HumanMessage, SystemMessage
from orchestrator.state import PlatformState, Artifact
from orchestrator.llm_client import get_agent_llm

logger = logging.getLogger(__name__)

MAX_TICKET_ATTEMPTS = 3


# ── System prompt ──────────────────────────────────────────────────────────────

DEV_SYSTEM_PROMPT = """You are a senior software engineer. You write clean, production-ready Python code.

You will receive:
  1. A SINGLE Scrum ticket to implement (title, description, acceptance criteria).
  2. The existing codebase files already committed (may be empty for the first ticket).
  3. The project BA spec summary for context.

Your job: implement ONLY this one ticket. Do not add code for other tickets.

Output ONLY valid JSON — no markdown, no explanation, no preamble.

Schema:
{
  "language": "python",
  "framework": "fastapi",
  "files": [
    {
      "path": "relative/path/to/file.py",
      "content": "full file content as a string"
    }
  ],
  "dependencies": ["fastapi", "uvicorn", "sqlalchemy"],
  "run_command": "uvicorn main:app --reload",
  "test_command": "pytest tests/ -v",
  "notes": "what this ticket adds/changes"
}

Rules for the files list:
- Include EVERY file that needs to exist for the app to run, not just the changed ones.
  If main.py already exists (given to you in existing codebase), include it unchanged.
  If models.py needs a new table for this ticket, include the full updated models.py.
- Always include: main.py, models.py, schemas.py, database.py, requirements.txt
- Always include: tests/test_<ticket_id>.py — a pytest file that tests ONLY this ticket's
  acceptance criteria. Use synchronous TestClient. Example:
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    def test_<criterion_slug>():
        # test the specific acceptance criterion
- Use SQLite (not PostgreSQL) for zero-setup portability
- Use python-jose for JWT, passlib for passwords, sqlalchemy for ORM
- Include CORS middleware in main.py
- File content must be complete and runnable — no placeholders, no TODOs
- Escape all double quotes inside string values with backslash
- The JSON must be valid — test mentally before outputting

CRITICAL RULES FOR TEST FILES:
- ONLY import names that are actually defined in your source files
- Use `from fastapi.testclient import TestClient` and `from main import app`
- Do NOT use pytest-asyncio async tests — use synchronous TestClient only
- Test 2-4 specific behaviours from the acceptance criteria
- Each test function name should reflect what it tests, e.g. test_user_can_register()
"""


# ── Dev agent node ─────────────────────────────────────────────────────────────

def dev_agent_node(state: PlatformState) -> dict:
    """LangGraph node for the Developer agent (one ticket at a time)."""

    ba_spec     = _get_artifact(state, "ba",    "spec")
    scrum_tasks = _get_artifact(state, "scrum", "tasks")

    if not ba_spec:
        return _fail(state, "No BA spec found", None)
    if not scrum_tasks:
        return _fail(state, "No Scrum tasks found", None)

    # ── Select or confirm current ticket ─────────────────────────────────────
    ticket = _resolve_current_ticket(state, scrum_tasks)

    if ticket is None:
        # No pending tickets — signal pipeline to move on
        logger.info("[Dev Agent] No pending tickets. All done.")
        return {
            "active_agent": "developer",
            "current_decision": None,
            "current_ticket_id": None,
        }

    ticket_id = ticket["id"]
    logger.info(f"[Dev Agent] Working on ticket {ticket_id}: {ticket['title']}")

    # ── Check if we've hit max attempts for this ticket ───────────────────────
    attempt = state.ticket_attempt_count
    if attempt >= MAX_TICKET_ATTEMPTS:
        logger.warning(
            f"[Dev Agent] Ticket {ticket_id} exceeded max attempts ({MAX_TICKET_ATTEMPTS}). "
            f"Skipping and marking complete."
        )
        return _skip_ticket(state, ticket_id)

    # ── Build prompt ──────────────────────────────────────────────────────────
    existing_files_summary = _summarize_existing_files(state.sprint_committed_files)
    qa_feedback            = _get_qa_failure_for_ticket(state, ticket_id)
    retry_section          = ""

    if qa_feedback:
        retry_section = f"""
## IMPORTANT: Previous implementation of this ticket had test failures
{qa_feedback}

Fix ALL issues above. Pay close attention to imports, exported names, and logic errors.
"""

    acceptance_criteria = "\n".join(
        f"  - {ac}" for ac in ticket.get("acceptance_criteria", [])
    )
    dependencies_note = ""
    deps = ticket.get("dependencies", [])
    if deps:
        dep_tickets = [t for t in scrum_tasks.get("tickets", []) if t["id"] in deps]
        dep_summaries = [f"{t['id']}: {t['title']}" for t in dep_tickets]
        dependencies_note = (
            f"\n\nThis ticket depends on: {', '.join(dep_summaries)}. "
            f"Those features should already be in the existing codebase below."
        )

    user_prompt = f"""## Project context
Title: {ba_spec.get('project_title', '')}
Summary: {ba_spec.get('summary', '')}

## THE TICKET TO IMPLEMENT NOW
ID: {ticket.get('id', 'UNKNOWN')}
Type: {ticket.get('type', 'feature')}
Title: {ticket.get('title', 'Untitled Ticket')}
Description: {ticket.get('description', '')}
Story Points: {ticket.get('story_points', 0)}
Priority: {ticket.get('priority', 'Medium')}

Acceptance Criteria (your tests MUST verify each of these):
{acceptance_criteria}{dependencies_note}

## Existing codebase (already committed — include all these files in your output unchanged unless this ticket modifies them)
{existing_files_summary}
{retry_section}
Implement ONLY this ticket. Output only the JSON object.
"""

    llm = get_agent_llm(temperature=0.2)

    for attempt_num in range(3):
        try:
            logger.info(f"[Dev Agent] Ticket {ticket_id} — LLM attempt {attempt_num + 1}/3")
            response = llm.invoke([
                SystemMessage(content=DEV_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            raw = response.content.strip()

            if not raw:
                logger.warning(f"[Dev Agent] Empty response on LLM attempt {attempt_num + 1}")
                time.sleep(2)
                continue

            code_pkg = _parse_code(raw)
            if code_pkg:
                file_count = len(code_pkg.get("files", []))
                logger.info(
                    f"[Dev Agent] Ticket {ticket_id}: generated {file_count} files"
                )

                artifact = Artifact(
                    agent="developer",
                    artifact_type="code",
                    content=json.dumps(code_pkg, indent=2),
                    status="ok",
                    metadata={
                        "ticket_id": ticket_id,
                        "ticket_title": ticket["title"],
                        "file_count": file_count,
                        "language": code_pkg.get("language", "python"),
                        "framework": code_pkg.get("framework", "fastapi"),
                        "files": [f["path"] for f in code_pkg.get("files", [])],
                        "attempt": attempt,
                    },
                )

                return {
                    "artifacts": state.artifacts + [artifact],
                    "active_agent": "developer",
                    "current_decision": None,
                    "current_ticket_id": ticket_id,
                    "ticket_attempt_count": attempt + 1,
                    # Don't update sprint_committed_files here — QA must pass first
                    # The qa_agent will signal success and we merge files then
                }
            else:
                logger.warning(f"[Dev Agent] Parse failed on LLM attempt {attempt_num + 1}")
                time.sleep(2)

        except Exception as e:
            logger.warning(f"[Dev Agent] Ticket {ticket_id} attempt error: {e}")
            time.sleep(2)

    return _fail(state, f"Failed to generate code for ticket {ticket_id} after 3 LLM attempts", ticket_id)


# ── Ticket management helpers ──────────────────────────────────────────────────

def _resolve_current_ticket(state: PlatformState, scrum_tasks: dict) -> dict | None:
    """
    Return the ticket we should work on:
    - If state.current_ticket_id is set and not complete → return that ticket
    - Otherwise find the next pending ticket from scrum plan
    """
    all_tickets = scrum_tasks.get("tickets", [])
    all_sprints = scrum_tasks.get("sprints", [])
    done        = set(state.completed_ticket_ids)

    if state.current_ticket_id and state.current_ticket_id not in done:
        # Validate it still exists
        for t in all_tickets:
            if t["id"] == state.current_ticket_id:
                return t

    # Pick next pending ticket (respecting sprint order + dependencies)
    for sp in sorted(all_sprints, key=lambda s: s["sprint_number"]):
        sprint_ids     = set(sp.get("ticket_ids", []))
        sprint_tickets = [t for t in all_tickets if t["id"] in sprint_ids]
        for ticket in _order_by_deps(sprint_tickets, all_tickets):
            if ticket["id"] not in done:
                return ticket

    return None


def _order_by_deps(sprint_tickets: list, all_tickets: list) -> list:
    """Topological sort of sprint tickets by their dependency list."""
    id_to_ticket = {t["id"]: t for t in all_tickets}
    sprint_ids   = {t["id"] for t in sprint_tickets}
    ordered      = []
    visited      = set()

    def visit(tid: str):
        if tid in visited or tid not in sprint_ids:
            return
        visited.add(tid)
        ticket = id_to_ticket.get(tid)
        if ticket:
            for dep in ticket.get("dependencies", []):
                if dep in sprint_ids:
                    visit(dep)
            ordered.append(ticket)

    for t in sprint_tickets:
        visit(t["id"])

    ordered_ids = {t["id"] for t in ordered}
    for t in sprint_tickets:
        if t["id"] not in ordered_ids:
            ordered.append(t)

    return ordered


def _skip_ticket(state: PlatformState, ticket_id: str) -> dict:
    """Mark a ticket as done-with-fail so the pipeline can advance."""
    logger.warning(f"[Dev Agent] Skipping ticket {ticket_id} after max attempts.")
    artifact = Artifact(
        agent="developer",
        artifact_type="code",
        content=json.dumps({"skipped": True, "ticket_id": ticket_id}),
        status="fail",
        metadata={"ticket_id": ticket_id, "skipped": True},
    )
    return {
        "artifacts": state.artifacts + [artifact],
        "active_agent": "developer",
        "current_decision": None,
        "current_ticket_id": None,
        "ticket_attempt_count": 0,
        "completed_ticket_ids": state.completed_ticket_ids + [ticket_id],
    }


# ── Context building helpers ───────────────────────────────────────────────────

def _summarize_existing_files(sprint_committed_files: list) -> str:
    """Show the dev agent a summary of already-committed files so it knows what exists."""
    if not sprint_committed_files:
        return "(no files committed yet — this is the first ticket, start from scratch)"

    lines = [f"The following {len(sprint_committed_files)} files already exist in the codebase:\n"]
    for f in sprint_committed_files:
        path    = f.get("path", "")
        content = f.get("content", "")
        # Show first 30 lines to give context without overflowing the prompt
        preview_lines = content.split("\n")[:30]
        preview       = "\n".join(preview_lines)
        if len(content.split("\n")) > 30:
            preview += f"\n... ({len(content.split(chr(10))) - 30} more lines)"
        lines.append(f"### File: {path}\n```python\n{preview}\n```\n")

    return "\n".join(lines)


def _get_qa_failure_for_ticket(state: PlatformState, ticket_id: str) -> str:
    """Get the most recent QA failure specifically for this ticket."""
    for a in reversed(state.artifacts):
        if a.agent == "qa" and a.status == "fail" and a.metadata.get("ticket_id") == ticket_id:
            try:
                report = json.loads(a.content)
                parts  = []

                if report.get("collection_failed"):
                    parts.append("CRITICAL: Pytest could not even collect (import) the test file.")
                    parts.append("Fix the import error before anything else.")
                    root  = report.get("root_cause", "")
                    summ  = report.get("failure_summary", "")
                    if root:
                        parts.append(f"\nExact error: {root}")
                    if summ:
                        parts.append(f"\nFull output:\n{summ[:800]}")
                    recs = report.get("recommendations", [])
                    for r in recs:
                        parts.append(f"  - {r}")
                else:
                    summ = report.get("failure_summary", "")
                    root = report.get("root_cause", "")
                    recs = report.get("recommendations", [])
                    if summ:
                        parts.append(f"Test failure:\n{summ[:500]}")
                    if root:
                        parts.append(f"Root cause: {root}")
                    if recs:
                        parts.append("Fix: " + "; ".join(recs[:3]))

                return "\n".join(parts)
            except Exception:
                return ""
    return ""


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _get_artifact(state: PlatformState, agent: str, artifact_type: str) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == agent and a.artifact_type == artifact_type and a.status == "ok":
            try:
                return json.loads(a.content)
            except json.JSONDecodeError:
                return None
    return None


def _parse_code(raw: str) -> dict | None:
    """Extract and validate the code JSON from LLM output."""
    cleaned = raw

    if cleaned.startswith("```"):
        lines   = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip()

    def _is_valid(data: dict) -> bool:
        files     = data.get("files", [])
        non_empty = [f for f in files if len(f.get("content", "").strip()) > 50]
        if len(non_empty) < 2:
            logger.warning(f"[Dev Agent] Only {len(non_empty)} files have real content (need ≥2)")
            return False
        return True

    # Strategy 1: direct JSON parse
    try:
        data = json.loads(cleaned)
        if _is_valid(data):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: brace-match (handles trailing text)
    try:
        start = cleaned.index("{")
        depth, end = 0, start
        for i, ch in enumerate(cleaned[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        data = json.loads(cleaned[start:end + 1])
        if _is_valid(data):
            return data
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"[Dev Agent] JSON brace-match failed: {e}")

    # Strategy 3: ast.literal_eval for Python-dict responses
    try:
        data = ast.literal_eval(cleaned)
        if isinstance(data, dict) and _is_valid(data):
            return data
    except (ValueError, SyntaxError):
        pass

    return None


def _fail(state: PlatformState, reason: str, ticket_id: str | None) -> dict:
    logger.error(f"[Dev Agent] {reason}")
    return {
        "artifacts": state.artifacts + [Artifact(
            agent="developer",
            artifact_type="code",
            content="{}",
            status="fail",
            metadata={"error": reason, "ticket_id": ticket_id or ""},
        )],
        "active_agent": "developer",
        "current_decision": None,
        "current_ticket_id": ticket_id,
    }
