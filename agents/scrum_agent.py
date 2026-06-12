"""
scrum_agent.py — Scrum Master agent.

Reads the BA spec artifact and produces:
  - Epics broken into stories
  - Story point estimates
  - Sprint plan (Sprint 1, Sprint 2, ...)
  - Individual ticket list ready for a task tracker
"""

import json
import logging
import time
from langchain_core.messages import HumanMessage, SystemMessage
from orchestrator.state import PlatformState, Artifact
from orchestrator.llm_client import get_agent_llm

logger = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

SCRUM_SYSTEM_PROMPT = """You are a senior Scrum Master and project manager at a software company.

You receive a structured BA spec and produce a full sprint plan with individual tickets.

Output ONLY valid JSON — no markdown, no explanation, no preamble.

Schema:
{
  "epics": [
    {
      "id": "EP-001",
      "title": "short epic name",
      "description": "what this epic covers"
    }
  ],
  "tickets": [
    {
      "id": "TK-001",
      "epic_id": "EP-001",
      "title": "short task title",
      "type": "feature | bug | chore | test",
      "description": "what needs to be done",
      "acceptance_criteria": ["specific done condition 1", "specific done condition 2"],
      "story_points": 1,
      "priority": "high | medium | low",
      "dependencies": ["TK-000"]
    }
  ],
  "sprints": [
    {
      "sprint_number": 1,
      "goal": "what this sprint delivers",
      "ticket_ids": ["TK-001", "TK-002"],
      "total_points": 8
    }
  ],
  "velocity_assumption": 8,
  "total_sprints": 2
}

Rules:
- Create 2-4 epics that logically group the work
- Create 8-15 tickets covering all user stories from the spec
- story_points must be Fibonacci: 1, 2, 3, 5, 8, 13
- Sprint capacity = velocity_assumption points per sprint
- Order tickets by dependencies — nothing depends on something in a later sprint
- type "test" tickets are for setting up test infrastructure (not unit tests for each feature)
- Include at least one "chore" ticket for project setup (repo, CI, env config)
- dependencies array is empty [] if no dependencies
"""


# ── Scrum agent node ───────────────────────────────────────────────────────────

def scrum_agent_node(state: PlatformState) -> dict:
    """LangGraph node for the Scrum Master agent."""
    logger.info("[Scrum Agent] Building sprint plan from BA spec...")

    # Extract BA spec from artifacts
    ba_spec = _get_ba_spec(state)
    if not ba_spec:
        logger.error("[Scrum Agent] No BA spec found in artifacts")
        return _fail_artifact(state, "No BA spec available to plan from")

    # Context from orchestrator
    context = ""
    if state.current_decision:
        context = f"\nOrchestrator note: {state.current_decision.context_for_agent}"

    user_prompt = f"""Here is the BA spec to turn into a sprint plan:

{json.dumps(ba_spec, indent=2)}
{context}

Produce the full sprint plan JSON now."""

    llm = get_agent_llm(temperature=0.2)

    for attempt in range(3):
        try:
            response = llm.invoke([
                SystemMessage(content=SCRUM_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            raw = response.content.strip()

            plan = _parse_plan(raw)
            if plan:
                ticket_count = len(plan.get("tickets", []))
                sprint_count = len(plan.get("sprints", []))
                logger.info(
                    f"[Scrum Agent] Plan ready: {ticket_count} tickets across "
                    f"{sprint_count} sprints"
                )

                artifact = Artifact(
                    agent="scrum",
                    artifact_type="tasks",
                    content=json.dumps(plan, indent=2),
                    status="ok",
                    metadata={
                        "ticket_count": ticket_count,
                        "sprint_count": sprint_count,
                        "epic_count": len(plan.get("epics", [])),
                        "total_points": sum(
                            s.get("total_points", 0)
                            for s in plan.get("sprints", [])
                        ),
                    },
                )
                return {
                    "artifacts": state.artifacts + [artifact],
                    "active_agent": "scrum",
                    "current_decision": None,
                }

        except Exception as e:
            logger.warning(f"[Scrum Agent] Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(1)

    return _fail_artifact(state, "Failed to generate sprint plan after 3 attempts")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_ba_spec(state: PlatformState) -> dict | None:
    """Find the most recent successful BA spec artifact."""
    for artifact in reversed(state.artifacts):
        if artifact.agent == "ba" and artifact.artifact_type == "spec" and artifact.status == "ok":
            try:
                return json.loads(artifact.content)
            except json.JSONDecodeError:
                return None
    return None


def _parse_plan(raw: str) -> dict | None:
    """Strip fences, parse JSON, validate required keys."""
    cleaned = raw
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip()

    try:
        data = json.loads(cleaned)
        required = {"epics", "tickets", "sprints"}
        if not required.issubset(data.keys()):
            logger.warning(f"[Scrum Agent] Missing keys: {required - data.keys()}")
            return None
        if not data["tickets"]:
            logger.warning("[Scrum Agent] Empty ticket list")
            return None
        return data
    except json.JSONDecodeError as e:
        logger.warning(f"[Scrum Agent] JSON parse error: {e}")
        return None


def _fail_artifact(state: PlatformState, reason: str) -> dict:
    artifact = Artifact(
        agent="scrum",
        artifact_type="tasks",
        content="{}",
        status="fail",
        metadata={"error": reason},
    )
    return {
        "artifacts": state.artifacts + [artifact],
        "active_agent": "scrum",
        "current_decision": None,
    }
