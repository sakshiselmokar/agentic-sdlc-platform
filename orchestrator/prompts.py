"""prompts.py — Orchestrator system prompt and dynamic user prompt."""

from .state import PlatformState


ORCHESTRATOR_SYSTEM_PROMPT = """You are the Orchestrator of an autonomous software development platform.

## Available agents
- ba        → Business Analyst. Parses requirements → structured spec.
- scrum     → Scrum Master. Breaks spec into sprint tickets.
- developer → Writes code for ONE ticket only.
- qa        → Tests ONE ticket's code against its acceptance criteria.
- git       → Commits all sprint files to a git repo.
- github    → Pushes repo to GitHub.
- devops    → Generates Dockerfile + CI/CD pipeline.
- done      → Signal project is complete.

## Real SDLC flow — follow this exactly
1. No BA spec → "ba"
2. BA done, no sprint plan → "scrum"
3. Sprint plan done → for each ticket:
   a. Pick next pending ticket → "developer" (ONE ticket at a time)
   b. Developer produced code → "qa"
   c. Developer FAILED to generate code (no files) → "developer" retry
   d. QA pass → next ticket → "developer"
   e. QA fail → "developer" (retry with error report, up to 3 total attempts)
4. All sprint tickets done or skipped → "git"
5. All sprints done → "github" → "devops" → "done"

## ABSOLUTE RULES — violating these causes infinite loops
- NEVER suggest "developer" or "qa" for a ticket listed under PASSED or SKIPPED/EXHAUSTED.
- NEVER suggest "ba" if BA spec already exists.
- NEVER suggest "scrum" if scrum plan already exists.
- If github has failed 2+ times → suggest "devops" instead, do NOT retry github.
- Once you see PASSED and SKIPPED tickets covering all sprint tickets → suggest "git".

## CRITICAL OUTPUT RULE
You MUST respond with ONLY a raw JSON object.
- Your response MUST start with { (opening brace). NO text before it.
- Your response MUST end with } (closing brace). NO text after it.
- NO markdown (no ```json). NO explanation. ONLY the JSON.

{
  "next_agent": "<ba|scrum|developer|qa|git|github|devops|done>",
  "reasoning": "<one sentence>",
  "priority": "<high|normal|low>",
  "context_for_agent": "<specific instruction for the next agent>"
}"""


def build_orchestrator_user_prompt(state: PlatformState) -> str:
    artifact_lines = []
    seen = {}
    for a in state.artifacts:
        seen[(a.agent, a.artifact_type)] = a
    for a in seen.values():
        tid = a.metadata.get("ticket_id", "")
        extra = f" [ticket:{tid}]" if tid else ""
        artifact_lines.append(f"  [{a.agent.upper()}] {a.artifact_type} → {a.status}{extra}")

    artifact_summary = "\n".join(artifact_lines) if artifact_lines else "  (none yet)"

    # Build explicit pipeline status to reduce LLM confusion
    ba_done    = any(a.agent == "ba"    and a.status == "ok" for a in state.artifacts)
    scrum_done = any(a.agent == "scrum" and a.status == "ok" for a in state.artifacts)
    git_done   = any(a.agent == "git"   and a.status == "ok" for a in state.artifacts)

    pipeline_status = []
    pipeline_status.append(f"  BA spec:    {'✓ DONE — do NOT call ba again' if ba_done else '✗ needed'}")
    pipeline_status.append(f"  Scrum plan: {'✓ DONE — do NOT call scrum again' if scrum_done else '✗ needed'}")
    pipeline_status.append(f"  Git commit: {'✓ DONE' if git_done else '✗ pending'}")

    # Show dev/QA failure hints only for the currently ACTIVE ticket
    if state.current_ticket_id:
        dev_fail_arts = [a for a in state.artifacts
                         if a.agent == "developer" and a.status == "fail"
                         and a.metadata.get("ticket_id") == state.current_ticket_id]
        if dev_fail_arts:
            pipeline_status.append(f"  ⚠ Dev FAILED to generate code for {state.current_ticket_id} — retry developer")

        qa_fail_arts = [a for a in state.artifacts
                        if a.agent == "qa" and a.status == "fail"
                        and a.metadata.get("ticket_id") == state.current_ticket_id]
        if qa_fail_arts:
            pipeline_status.append(f"  ⚠ QA FAILED for {state.current_ticket_id} — retry developer")

    # Show exhausted tickets explicitly so the LLM never suggests retrying them
    from .orchestrator_node import _get_exhausted, _get_scrum_tasks
    scrum_tasks_now = _get_scrum_tasks(state)
    exhausted_ids: set = set()
    if scrum_tasks_now:
        exhausted_ids = _get_exhausted(state, scrum_tasks_now.get("tickets", []))
    if exhausted_ids:
        pipeline_status.append(
            f"  ⛔ SKIPPED/EXHAUSTED — do NOT retry these: {', '.join(sorted(exhausted_ids))}"
        )
    if state.completed_ticket_ids:
        pipeline_status.append(
            f"  ✅ PASSED — do NOT retry these: {', '.join(state.completed_ticket_ids)}"
        )

    ticket_lines = []
    if state.current_ticket_id:
        ticket_lines.append(f"  Current ticket: {state.current_ticket_id}")
    if state.completed_ticket_ids:
        ticket_lines.append(f"  Completed ({len(state.completed_ticket_ids)}): {', '.join(state.completed_ticket_ids)}")
    ticket_lines.append(f"  Current sprint: {state.current_sprint}")
    ticket_lines.append(f"  Accumulated files: {len(state.sprint_committed_files)}")

    error_lines = ""
    if state.errors:
        error_lines = "\nRecent errors:\n" + "\n".join(f"  - {e}" for e in state.errors[-3:])

    return f"""Project: {state.project_input[:300]}

Pipeline status:
{chr(10).join(pipeline_status)}

Artifacts produced:
{artifact_summary}

Ticket progress:
{chr(10).join(ticket_lines)}
{error_lines}

Respond with ONLY the JSON object. Start with {{"""