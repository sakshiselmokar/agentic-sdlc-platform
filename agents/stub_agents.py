"""
stub_agents.py — Placeholder nodes for agents not yet built.

Each stub logs a message and returns a fake "ok" artifact
so the graph can run end-to-end for testing Layer 1.
Layers 2-6 will replace these one by one.
"""

import logging
from orchestrator.state import PlatformState, Artifact

logger = logging.getLogger(__name__)


def _stub(agent_name: str, artifact_type: str, content: str):
    """Factory that creates a stub node function for a given agent."""
    def node(state: PlatformState) -> dict:
        logger.info(f"[{agent_name.upper()} STUB] Running stub — real agent coming in later layer.")
        artifact = Artifact(
            agent=agent_name,
            artifact_type=artifact_type,
            content=content,
            status="ok",
        )
        return {
            "artifacts": state.artifacts + [artifact],
            "active_agent": agent_name,
            "current_decision": None,  # clears decision so orchestrator re-evaluates
        }
    node.__name__ = f"{agent_name}_agent"
    return node


ba_agent_node        = _stub("ba",        "spec",         "STUB: BA spec will go here.")
scrum_agent_node     = _stub("scrum",     "tasks",        "STUB: Sprint tasks will go here.")
dev_agent_node       = _stub("developer", "code",         "STUB: Code will go here.")
qa_agent_node        = _stub("qa",        "test_report",  "STUB: Test report will go here.")
devops_agent_node    = _stub("devops",    "deploy_log",   "STUB: Deploy log will go here.")


def finalize_node(state: PlatformState) -> dict:
    """Terminal node — assembles final output."""
    logger.info("[FINALIZE] Pipeline complete.")

    # Pick the best artifact per agent (last ok, or last fail if no ok)
    best = {}
    for a in state.artifacts:
        key = (a.agent, a.artifact_type)
        if key not in best or a.status == "ok":
            best[key] = a

    summary_lines = [f"=== Project Complete ===", f"Input: {state.project_input[:100]}...", ""]
    for a in best.values():
        summary_lines.append(f"[{a.agent.upper()}] {a.artifact_type} [{a.status}]: {a.content[:80]}...")
    return {"final_output": "\n".join(summary_lines)}


def error_handler_node(state: PlatformState) -> dict:
    """Called when max retries exceeded."""
    logger.error(f"[ERROR HANDLER] Pipeline failed. Errors: {state.errors}")
    return {
        "final_output": f"Pipeline failed after max retries.\nErrors:\n" +
                        "\n".join(state.errors)
    }
