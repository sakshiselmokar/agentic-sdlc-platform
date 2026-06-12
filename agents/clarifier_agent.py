"""
clarifier_agent.py — Requirement Ambiguity Detector.

Runs BEFORE the BA agent. Scores the project input on specificity (0–100).
If score < 60, generates 3 targeted clarifying questions and pauses the
pipeline — the API returns these questions to the frontend so the user
can answer before continuing.

If score >= 60, pipeline continues automatically with a green-light signal.

Scoring rubric (each dimension 0–20):
  - Domain clarity       (what kind of app / system?)
  - User/actor clarity   (who uses it?)
  - Feature specificity  (concrete features, not just "make it good")
  - Tech hints           (any stack, language, or platform mentioned?)
  - Scale/constraint     (any NFRs, limits, or deployment context?)
"""

import json
import logging
import time
from langchain_core.messages import HumanMessage, SystemMessage
from orchestrator.state import PlatformState, Artifact
from orchestrator.llm_client import get_agent_llm

logger = logging.getLogger(__name__)


CLARIFIER_SYSTEM_PROMPT = """You are a senior requirements analyst. Your job is to evaluate
how specific and actionable a project description is, then generate targeted questions if it's too vague.

Output ONLY valid JSON — no markdown, no explanation.

Schema:
{
  "clarity_score": 0-100,
  "score_breakdown": {
    "domain_clarity": 0-20,
    "user_actor_clarity": 0-20,
    "feature_specificity": 0-20,
    "tech_hints": 0-20,
    "scale_constraints": 0-20
  },
  "verdict": "proceed" | "clarify",
  "confidence_summary": "1-2 sentences explaining the score",
  "clarifying_questions": [
    {
      "id": "Q1",
      "question": "the question text",
      "why_it_matters": "brief reason",
      "example_answers": ["answer A", "answer B", "answer C"]
    }
  ]
}

Rules:
- verdict is "proceed" if clarity_score >= 60, else "clarify"
- If verdict is "proceed", clarifying_questions can be empty []
- If verdict is "clarify", produce EXACTLY 3 clarifying questions
- Questions must be specific and actionable — NOT generic ("what features do you want?")
- example_answers must be concrete realistic options, not placeholders
- Keep all strings under 200 characters

Scoring guide:
- 0-30:  "Build me an app" — dangerously vague
- 31-59: Core domain known but missing critical details
- 60-79: Enough to start, some assumptions needed
- 80-100: Well-specified, minimal assumptions
"""


def clarifier_agent_node(state: PlatformState) -> dict:
    """LangGraph node for the Clarifier agent. Runs before BA."""
    logger.info("[Clarifier] Scoring input ambiguity...")

    # If clarification answers have been provided, skip re-evaluation
    if state.clarification_answers:
        logger.info("[Clarifier] Answers provided, proceeding to BA.")
        artifact = Artifact(
            agent="clarifier",
            artifact_type="clarity_check",
            content=json.dumps({
                "clarity_score": 100,
                "verdict": "proceed",
                "confidence_summary": "User provided clarification answers.",
                "clarifying_questions": [],
                "score_breakdown": {},
            }),
            status="ok",
            metadata={"verdict": "proceed", "score": 100},
        )
        return {
            "artifacts": state.artifacts + [artifact],
            "active_agent": "clarifier",
            "current_decision": None,
        }

    # Already clarified in this run — don't re-check
    for a in state.artifacts:
        if a.agent == "clarifier" and a.status == "ok":
            logger.info("[Clarifier] Already ran, skipping.")
            return {"active_agent": "clarifier", "current_decision": None}

    user_prompt = f"""Project description to evaluate:

\"\"\"{state.project_input}\"\"\"

Score the specificity of this description and decide if we need clarification."""

    llm = get_agent_llm(temperature=0.2)

    for attempt in range(3):
        try:
            response = llm.invoke([
                SystemMessage(content=CLARIFIER_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            raw = response.content.strip()
            result = _parse_result(raw)
            if result:
                score = result.get("clarity_score", 0)
                verdict = result.get("verdict", "clarify")
                logger.info(f"[Clarifier] Score: {score}/100 → {verdict}")

                artifact = Artifact(
                    agent="clarifier",
                    artifact_type="clarity_check",
                    content=json.dumps(result, indent=2),
                    status="ok",
                    metadata={
                        "clarity_score": score,
                        "verdict": verdict,
                        "question_count": len(result.get("clarifying_questions", [])),
                    },
                )
                return {
                    "artifacts": state.artifacts + [artifact],
                    "active_agent": "clarifier",
                    "current_decision": None,
                    # Signal pipeline to pause if clarification needed
                    "awaiting_clarification": verdict == "clarify",
                }

        except Exception as e:
            logger.warning(f"[Clarifier] Attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(1)

    # Failed to evaluate — proceed anyway (fail open, not closed)
    logger.warning("[Clarifier] Could not evaluate, proceeding with pipeline.")
    artifact = Artifact(
        agent="clarifier",
        artifact_type="clarity_check",
        content=json.dumps({"clarity_score": 70, "verdict": "proceed",
                            "confidence_summary": "Evaluation failed, proceeding.",
                            "clarifying_questions": []}),
        status="ok",
        metadata={"verdict": "proceed", "score": 70},
    )
    return {
        "artifacts": state.artifacts + [artifact],
        "active_agent": "clarifier",
        "current_decision": None,
    }


def _parse_result(raw: str) -> dict | None:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(cleaned)
        if "clarity_score" in data and "verdict" in data:
            return data
    except json.JSONDecodeError:
        pass
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
        if "clarity_score" in data and "verdict" in data:
            return data
    except (ValueError, json.JSONDecodeError):
        pass
    return None
