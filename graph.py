"""
graph.py — Complete LangGraph wiring with ticket-loop support.
recursion_limit set in api/main.py
"""

import logging
from langgraph.graph import StateGraph, END

from orchestrator.state import PlatformState
from orchestrator.orchestrator_node import orchestrator_node, route_after_orchestrator
from agents.ba_agent import ba_agent_node
from agents.scrum_agent import scrum_agent_node
from agents.dev_agent import dev_agent_node
from agents.qa_agent import qa_agent_node
from agents.git_agent import git_agent_node
from agents.devops_agent import devops_agent_node
from agents.stub_agents import finalize_node, error_handler_node

try:
    from agents.clarifier_agent import clarifier_agent_node
    HAS_CLARIFIER = True
except ImportError:
    HAS_CLARIFIER = False

try:
    from agents.github_agent import github_agent_node
    HAS_GITHUB = True
except ImportError:
    HAS_GITHUB = False

logger = logging.getLogger(__name__)


def _passthrough_clarifier(state): return {"active_agent": "clarifier", "current_decision": None}
def _passthrough_github(state):    return {"active_agent": "github",    "current_decision": None}


def route_after_clarifier(state: PlatformState) -> str:
    if getattr(state, "awaiting_clarification", False) and not getattr(state, "clarification_answers", {}):
        logger.info("[Graph] Clarifier paused — awaiting user answers")
        return "awaiting_user"
    return "orchestrator"


def build_graph():
    graph = StateGraph(PlatformState)

    graph.add_node("orchestrator",  orchestrator_node)
    graph.add_node("ba_agent",      ba_agent_node)
    graph.add_node("scrum_agent",   scrum_agent_node)
    graph.add_node("dev_agent",     dev_agent_node)
    graph.add_node("qa_agent",      qa_agent_node)
    graph.add_node("git_agent",     git_agent_node)
    graph.add_node("devops_agent",  devops_agent_node)
    graph.add_node("finalize",      finalize_node)
    graph.add_node("error_handler", error_handler_node)
    graph.add_node("clarifier",     clarifier_agent_node if HAS_CLARIFIER else _passthrough_clarifier)
    graph.add_node("github_agent",  github_agent_node    if HAS_GITHUB    else _passthrough_github)

    graph.set_entry_point("clarifier")

    graph.add_conditional_edges("clarifier", route_after_clarifier, {
        "orchestrator":  "orchestrator",
        "awaiting_user": END,
    })

    graph.add_conditional_edges("orchestrator", route_after_orchestrator, {
        "ba_agent":      "ba_agent",
        "scrum_agent":   "scrum_agent",
        "dev_agent":     "dev_agent",
        "qa_agent":      "qa_agent",
        "git_agent":     "git_agent",
        "github_agent":  "github_agent",
        "devops_agent":  "devops_agent",
        "finalize":      "finalize",
        "error_handler": "error_handler",
        "orchestrator":  "orchestrator",
    })

    # All agents return to orchestrator
    for node in ["ba_agent", "scrum_agent", "dev_agent", "qa_agent",
                 "git_agent", "github_agent", "devops_agent"]:
        graph.add_edge(node, "orchestrator")

    graph.add_edge("finalize",      END)
    graph.add_edge("error_handler", END)

    compiled = graph.compile()
    logger.info("[Graph] Compiled — patcher removed, clean pipeline ✓")
    return compiled


_graph = None
def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
