"""
ba_agent.py — Business Analyst agent.

Replaces the BA stub. Takes raw project input and produces a
structured spec with user stories, acceptance criteria, and edge cases.
"""

import json
import logging
import time
from langchain_core.messages import HumanMessage, SystemMessage
from orchestrator.state import PlatformState, Artifact
from orchestrator.llm_client import get_agent_llm

logger = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

BA_SYSTEM_PROMPT = """You are a senior Business Analyst. Produce a concise structured spec.

Output ONLY valid JSON — no markdown, no explanation, no preamble. Keep it SHORT.

Schema:
{
  "project_title": "short name",
  "summary": "1-2 sentence overview",
  "user_stories": [
    {
      "id": "US-001",
      "role": "user type",
      "goal": "action they want",
      "acceptance_criteria": ["criterion 1", "criterion 2"]
    }
  ],
  "edge_cases": ["edge case 1", "edge case 2"],
  "tech_stack_hints": ["technology 1", "technology 2"]
}

Rules:
- Produce EXACTLY 5 user stories (no more)
- Each user story needs EXACTLY 3 acceptance criteria (no more)
- Include EXACTLY 4 edge cases (no more)
- Keep all strings SHORT — under 100 characters each
- The entire JSON must fit in 2000 tokens
- Do NOT include non_functional_requirements or out_of_scope fields
"""


# ── BA agent node ──────────────────────────────────────────────────────────────

def ba_agent_node(state: PlatformState) -> dict:
    """LangGraph node for the BA agent."""
    logger.info("[BA Agent] Parsing requirements into structured spec...")

    # Get context from orchestrator decision
    context = ""
    if state.current_decision:
        context = f"\nAdditional context from orchestrator: {state.current_decision.context_for_agent}"

    user_prompt = f"""Project description:
{state.project_input}
{context}

Produce the full structured spec JSON now."""

    llm = get_agent_llm(temperature=0.3)

    for attempt in range(3):
        try:
            response = llm.invoke([
                SystemMessage(content=BA_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            raw = response.content.strip()

            spec = _parse_spec(raw)
            if spec:
                logger.info(
                    f"[BA Agent] Spec generated: {len(spec.get('user_stories', []))} user stories, "
                    f"{len(spec.get('edge_cases', []))} edge cases"
                )
                artifact = Artifact(
                    agent="ba",
                    artifact_type="spec",
                    content=json.dumps(spec, indent=2),
                    status="ok",
                    metadata={
                        "user_story_count": len(spec.get("user_stories", [])),
                        "edge_case_count": len(spec.get("edge_cases", [])),
                        "project_title": spec.get("project_title", ""),
                    },
                )
                return {
                    "artifacts": state.artifacts + [artifact],
                    "active_agent": "ba",
                    "current_decision": None,
                }

        except Exception as e:
            logger.warning(f"[BA Agent] Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(1)

    # All attempts failed — return a fail artifact so orchestrator can retry
    logger.error("[BA Agent] Failed to produce spec after 3 attempts")
    artifact = Artifact(
        agent="ba",
        artifact_type="spec",
        content="{}",
        status="fail",
        metadata={"error": "Failed to parse LLM output after 3 attempts"},
    )
    return {
        "artifacts": state.artifacts + [artifact],
        "active_agent": "ba",
        "current_decision": None,
    }


# ── Internal: parse LLM output ─────────────────────────────────────────────────

def _parse_spec(raw: str) -> dict | None:
    """Strip fences, extract JSON via brace-matching, validate required keys."""
    if not raw or not raw.strip():
        logger.warning("[BA Agent] Empty LLM response")
        return None

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip()

    # Strategy 1: direct parse
    try:
        data = json.loads(cleaned)
        return _validate(data)
    except json.JSONDecodeError:
        pass

    # Strategy 2: brace matching — extract first complete {...} block
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
        return _validate(data)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"[BA Agent] JSON parse error: {e}")
        return None


def _validate(data: dict) -> dict | None:
    required = {"project_title", "summary", "user_stories"}
    if not required.issubset(data.keys()):
        logger.warning(f"[BA Agent] Missing required keys: {required - data.keys()}")
        return None
    if not data.get("user_stories"):
        logger.warning("[BA Agent] Empty user_stories list")
        return None
    return data
