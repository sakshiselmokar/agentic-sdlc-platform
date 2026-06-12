"""
orchestrator_node.py — Central routing brain v3.3
Fixes applied vs original:
  - current_ticket_id written to state before routing
  - Ticket-level inner loop (dev→qa→patch per ticket)
  - Sprint boundary git commit detection
  - Exhausted ticket detection from artifact counts
  - 3-attempt inline retry with escalating strictness
  - 5-strategy JSON parser handles any free-model preamble
  - sprint_git_fail >= 2 → skip git and advance
"""

import json
import logging
import re
import time
from langchain_core.messages import HumanMessage, SystemMessage
from .state import PlatformState, OrchestratorDecision
from .llm_client import get_orchestrator_llm
from .prompts import ORCHESTRATOR_SYSTEM_PROMPT, build_orchestrator_user_prompt

logger = logging.getLogger(__name__)

MAX_TICKET_ATTEMPTS = 3
VALID_AGENTS = {"ba","scrum","developer","qa","git","github","devops","done","clarifier"}


# ── Main orchestrator node ─────────────────────────────────────────────────────

def orchestrator_node(state: PlatformState) -> dict:
    logger.info(
        f"[Orchestrator] Artifacts: {len(state.artifacts)} | Sprint: {state.current_sprint} | "
        f"Ticket: {state.current_ticket_id} | Done: {len(state.completed_ticket_ids)} | "
        f"Retries: {state.retry_count}"
    )

    llm           = get_orchestrator_llm()
    base_messages = [
        SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT),
        HumanMessage(content=build_orchestrator_user_prompt(state)),
    ]

    time.sleep(0.3)

    decision = None
    last_raw  = ""

    for attempt in range(3):
        if attempt == 0:
            messages = base_messages
        elif attempt == 1:
            messages = base_messages + [HumanMessage(content=(
                "IMPORTANT: Your previous response could not be parsed. "
                "Output ONLY the raw JSON object. Start with { end with }. "
                "No text outside the braces."
            ))]
        else:
            messages = [
                SystemMessage(content=(
                    "Output ONLY valid JSON. No explanation. No markdown. Start with { end with }.\n"
                    'Schema: {"next_agent":"<ba|scrum|developer|qa|git|github|devops|done>",'
                    '"reasoning":"<one sentence>","priority":"normal","context_for_agent":"<instruction>"}'
                )),
                HumanMessage(content=build_orchestrator_user_prompt(state)),
            ]

        try:
            response = llm.invoke(messages)
            last_raw  = response.content
        except Exception as e:
            logger.error(f"[Orchestrator] LLM error attempt {attempt+1}: {e}")
            time.sleep(1)
            if attempt == 2:
                return {"errors": state.errors + [f"Orchestrator LLM error: {e}"],
                        "retry_count": state.retry_count + 1}
            continue

        decision = _parse_decision(last_raw)
        if decision:
            break
        logger.warning(f"[Orchestrator] Parse failed attempt {attempt+1}/3 | Raw: {last_raw[:120]!r}")
        time.sleep(0.5)

    if decision is None:
        logger.error(f"[Orchestrator] All parse attempts failed | Last: {last_raw[:200]!r}")
        return {"errors": state.errors + [f"Parse failure: {last_raw[:80]}"],
                "retry_count": state.retry_count + 1}

    logger.info(f"[Orchestrator] LLM suggests → {decision.next_agent} | {decision.reasoning}")

    state_updates: dict = {
        "current_decision":  decision,
        "active_agent":      decision.next_agent,
        "retry_count":       0,
        "messages": [HumanMessage(
            content=f"Orchestrator → {decision.next_agent}: {decision.context_for_agent}"
        )],
    }

    # Advance current_ticket_id when the current ticket is fully settled.
    # "Settled" = completed (QA passed) OR exhausted (dev maxed + QA confirmed all failures).
    # _get_exhausted uses the same artifact-count logic as the router section 5, so they
    # always agree: the router only sends here after exhaustion is confirmed.
    scrum_tasks = _get_scrum_tasks(state)
    if scrum_tasks:
        all_tickets = scrum_tasks.get("tickets", [])
        exhausted   = _get_exhausted(state, all_tickets)
        current_is_done = (
            state.current_ticket_id is None
            or state.current_ticket_id in set(state.completed_ticket_ids)
            or state.current_ticket_id in exhausted
        )
        if current_is_done:
            next_ticket = _pick_next_ticket(state, scrum_tasks)
            if next_ticket and next_ticket["id"] != state.current_ticket_id:
                logger.info(f"[Orchestrator] Advancing to ticket {next_ticket['id']}")
                state_updates["current_ticket_id"]    = next_ticket["id"]
                state_updates["ticket_attempt_count"] = 0
            elif state.current_ticket_id is None and next_ticket:
                state_updates["current_ticket_id"]    = next_ticket["id"]
                state_updates["ticket_attempt_count"] = 0
            elif next_ticket is None:
                logger.info("[Orchestrator] All tickets settled — clearing current_ticket_id")
                state_updates["current_ticket_id"] = None

    return state_updates


# ── Router ─────────────────────────────────────────────────────────────────────

def route_after_orchestrator(state: PlatformState) -> str:
    if state.retry_count >= state.max_retries:
        logger.error("[Router] Max retries → error_handler")
        return "error_handler"
    if state.current_decision is None:
        return "orchestrator"

    def count(agent, status=None):
        return sum(1 for a in state.artifacts
                   if a.agent == agent and (status is None or a.status == status))

    ba_ok     = count("ba",     "ok")
    scrum_ok  = count("scrum",  "ok")
    github_ok = count("github", "ok")
    devops_n  = count("devops")

    # 1. Bootstrap
    if ba_ok == 0:
        return "ba_agent"
    if scrum_ok == 0:
        return "scrum_agent"

    scrum_tasks = _get_scrum_tasks(state)
    if not scrum_tasks:
        return "scrum_agent"

    all_tickets = scrum_tasks.get("tickets", [])
    all_sprints = scrum_tasks.get("sprints", [])

    # 2. Done check
    if state.current_decision.next_agent == "done" or devops_n >= 1:
        return "finalize"

    # 3. All tickets done → post-sprint
    remaining = _get_remaining_tickets(state, all_tickets, all_sprints)
    if len(remaining) == 0:
        git_ok_any = count("git", "ok")
        if git_ok_any == 0:
            git_fail = count("git", "fail")
            if git_fail >= 2:
                return "github_agent" if github_ok == 0 else "devops_agent"
            return "git_agent"
        # Allow one retry on github failure, then move on to devops
        github_fail = count("github", "fail")
        if github_fail >= 2:
            logger.warning(f"[Router] GitHub push failed {github_fail}x → skipping to devops")
            return "devops_agent"
        if github_ok == 0:
            return "github_agent"
        if devops_n == 0:
            return "devops_agent"
        return "finalize"

    # 4. Sprint boundary
    current_sprint_ids = _get_sprint_ticket_ids(all_sprints, state.current_sprint)
    # A sprint is done when every ticket is either QA-passed (completed) or exhausted (skipped).
    # Using only completed_ticket_ids caused git to never fire when some tickets failed.
    exhausted_ids       = _get_exhausted(state, all_tickets)
    settled_ids         = set(state.completed_ticket_ids) | exhausted_ids
    sprint_done_count   = sum(1 for tid in settled_ids if tid in set(current_sprint_ids))
    current_sprint_done = (len(current_sprint_ids) > 0
                           and sprint_done_count == len(current_sprint_ids))
    sprint_git_ok   = sum(1 for a in state.artifacts if a.agent == "git" and a.status == "ok"
                          and a.metadata.get("sprint_number") == state.current_sprint)
    sprint_git_fail = sum(1 for a in state.artifacts if a.agent == "git" and a.status == "fail"
                          and a.metadata.get("sprint_number") == state.current_sprint)

    if current_sprint_done and sprint_git_ok == 0:
        if sprint_git_fail >= 2:
            logger.warning(f"[Router] Sprint {state.current_sprint}: git failed {sprint_git_fail}x → skip")
            return "github_agent" if github_ok == 0 else "devops_agent"
        logger.info(f"[Router] Sprint {state.current_sprint} complete ({sprint_done_count}/{len(current_sprint_ids)}) → git_agent")
        return "git_agent"

    # 5. Ticket inner loop
    ticket_id = state.current_ticket_id
    if ticket_id is None:
        next_t = _pick_next_ticket(state, scrum_tasks)
        if next_t:
            return "orchestrator"
        return "devops_agent"

    dev_arts = _arts_for(state, "developer", ticket_id)
    qa_arts  = _arts_for(state, "qa",        ticket_id)

    # Only count dev artifacts that produced real code (status ok)
    dev_ok_arts = [a for a in dev_arts if a.status == "ok"]
    dev_ok      = len(dev_ok_arts)
    dev_fail    = len(dev_arts) - dev_ok
    qa_n        = len(qa_arts)
    last_dev_ok = dev_ok_arts[-1] if dev_ok_arts else None
    last_qa     = qa_arts[-1]     if qa_arts     else None

    logger.info(f"[Router] Ticket {ticket_id}: dev_ok={dev_ok} dev_fail={dev_fail} qa={qa_n}")

    # The SDLC cycle is strictly: dev → qa → dev → qa → dev → qa → skip
    # Rule: QA must run after EVERY successful dev output before we can retry or skip.
    # We use artifact counts to determine position in the cycle:
    #   dev_ok == 0 and dev_fail < MAX  → need first dev attempt
    #   dev_ok == 0 and dev_fail >= MAX → all parses failed, skip
    #   dev_ok > qa_n                   → dev produced new code, QA must run now
    #   dev_ok == qa_n, last_qa pass    → ticket done
    #   dev_ok == qa_n, last_qa fail, dev_ok < MAX → QA gave feedback, dev retries
    #   dev_ok == qa_n, last_qa fail, dev_ok >= MAX → exhausted, skip

    # 5a. No parseable code yet
    if dev_ok == 0:
        if dev_fail >= MAX_TICKET_ATTEMPTS:
            logger.warning(f"[Router] Ticket {ticket_id}: {MAX_TICKET_ATTEMPTS} dev parse failures → skip")
            return "orchestrator"
        return "dev_agent"

    # 5b. Dev produced code that QA hasn't seen yet → MUST run QA
    if dev_ok > qa_n:
        logger.info(f"[Router] Ticket {ticket_id}: new dev output (dev_ok={dev_ok} > qa={qa_n}) → qa_agent")
        return "qa_agent"

    # 5c. QA has seen every dev output (dev_ok == qa_n)
    if last_qa.status == "ok":
        # QA passed — ticket done, orchestrator advances pointer
        return "orchestrator"

    # 5d. QA failed — dev retries with the error report
    if dev_ok < MAX_TICKET_ATTEMPTS:
        logger.info(
            f"[Router] Ticket {ticket_id}: QA failed → dev_agent retry "
            f"({dev_ok + 1}/{MAX_TICKET_ATTEMPTS}) with QA error report"
        )
        return "dev_agent"

    # 5e. dev_ok >= MAX and QA still failing → ticket exhausted, skip
    logger.warning(
        f"[Router] Ticket {ticket_id}: {MAX_TICKET_ATTEMPTS} dev attempts all QA-failed → skip"
    )
    return "orchestrator"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_scrum_tasks(state: PlatformState) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == "scrum" and a.artifact_type == "tasks" and a.status == "ok":
            try: return json.loads(a.content)
            except: return None
    return None

def _get_sprint_ticket_ids(all_sprints, sprint_number):
    for sp in all_sprints:
        if sp["sprint_number"] == sprint_number:
            return sp.get("ticket_ids", [])
    return []

def _get_remaining_tickets(state, all_tickets, all_sprints):
    done = set(state.completed_ticket_ids) | _get_exhausted(state, all_tickets)
    remaining = []
    for sp in sorted(all_sprints, key=lambda s: s["sprint_number"]):
        ids = set(sp.get("ticket_ids", []))
        for t in all_tickets:
            if t["id"] in ids and t["id"] not in done:
                remaining.append(t)
    return remaining

def _get_exhausted(state, all_tickets):
    """
    A ticket is exhausted when the full dev→qa cycle has completed MAX_TICKET_ATTEMPTS
    times and QA never passed. This mirrors the router's section 5 logic exactly:
      - dev_ok >= MAX  (produced real code MAX times)
      - qa_n >= dev_ok (QA ran after every dev output)
      - no qa passed
    OR: all dev attempts failed to parse (dev_ok == 0, dev_fail >= MAX).
    """
    exhausted = set()
    for t in all_tickets:
        tid      = t["id"]
        dev_arts = [a for a in state.artifacts
                    if a.agent == "developer" and a.metadata.get("ticket_id") == tid]
        qa_arts  = [a for a in state.artifacts
                    if a.agent == "qa"        and a.metadata.get("ticket_id") == tid]
        dev_ok   = sum(1 for a in dev_arts if a.status == "ok")
        dev_fail = len(dev_arts) - dev_ok
        qa_n     = len(qa_arts)
        qa_ok    = any(a.status == "ok" for a in qa_arts)

        if qa_ok:
            continue  # ticket passed QA — not exhausted

        if dev_ok == 0 and dev_fail >= MAX_TICKET_ATTEMPTS:
            # All dev attempts failed to parse — nothing ever got to QA
            exhausted.add(tid)
        elif dev_ok >= MAX_TICKET_ATTEMPTS and qa_n >= dev_ok:
            # Every dev output was QA'd and none passed
            exhausted.add(tid)
    return exhausted

def _pick_next_ticket(state, scrum_tasks):
    all_tickets = scrum_tasks.get("tickets", [])
    all_sprints = scrum_tasks.get("sprints", [])
    done = set(state.completed_ticket_ids) | _get_exhausted(state, all_tickets)

    # Walk sprints in order, respecting dependencies via topo sort.
    # Do NOT short-circuit on current_ticket_id — that caused the orchestrator
    # to keep returning the same ticket when state.completed_ticket_ids hadn't
    # updated yet (stale snapshot). Always do a full forward scan.
    for sp in sorted(all_sprints, key=lambda s: s["sprint_number"]):
        ids     = set(sp.get("ticket_ids", []))
        tickets = [t for t in all_tickets if t["id"] in ids]
        for t in _topo_sort(tickets, all_tickets):
            if t["id"] not in done:
                return t
    return None

def _topo_sort(sprint_tickets, all_tickets):
    id_map     = {t["id"]: t for t in all_tickets}
    sprint_ids = {t["id"] for t in sprint_tickets}
    ordered, visited = [], set()
    def visit(tid):
        if tid in visited or tid not in sprint_ids: return
        visited.add(tid)
        for dep in id_map.get(tid, {}).get("dependencies", []):
            if dep in sprint_ids: visit(dep)
        ordered.append(id_map[tid])
    for t in sprint_tickets: visit(t["id"])
    ordered_ids = {t["id"] for t in ordered}
    for t in sprint_tickets:
        if t["id"] not in ordered_ids: ordered.append(t)
    return ordered

def _arts_for(state, agent, ticket_id):
    return [a for a in state.artifacts if a.agent == agent and a.metadata.get("ticket_id") == ticket_id]


# ── JSON parsing ───────────────────────────────────────────────────────────────

def _parse_decision(raw: str) -> OrchestratorDecision | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    # Strip known LLM safety/preamble prefixes (Qwen3 emits these before JSON)
    safety_prefixes = [
        "User Safety: safe", "User Safety:safe",
        "safety: safe", "Safety: safe",
        "Content is safe", "Input is safe",
    ]
    for prefix in safety_prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
            break

    # Strip thinking tags that some models emit
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()

    if not text:
        return None

    # Strip fences
    if "```" in text:
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```")).strip()

    # Try direct parse
    try:
        return _build_decision(json.loads(text))
    except: pass

    # Try every { } block, longest first
    for candidate in sorted(_find_json_objects(text), key=len, reverse=True):
        try:
            result = _build_decision(json.loads(candidate))
            if result: return result
        except: continue

    # Regex fallback
    m_agent   = re.search(r'"next_agent"\s*:\s*"([a-z_]+)"', text, re.I)
    m_reason  = re.search(r'"reasoning"\s*:\s*"([^"]+)"',    text, re.I)
    m_context = re.search(r'"context_for_agent"\s*:\s*"([^"]+)"', text, re.I)
    if m_agent and m_agent.group(1).lower() in VALID_AGENTS:
        logger.warning(f"[Orchestrator] Regex fallback: agent={m_agent.group(1)}")
        return OrchestratorDecision(
            next_agent=m_agent.group(1).lower(),
            reasoning=m_reason.group(1) if m_reason else "extracted via regex",
            priority="normal",
            context_for_agent=m_context.group(1) if m_context else "proceed",
        )

    logger.warning(f"[Orchestrator] All parse strategies failed | Raw: {raw[:200]}")
    return None

def _find_json_objects(text):
    results = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth, start = 0, i
            for j in range(i, len(text)):
                if text[j] == "{": depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        results.append(text[start:j+1])
                        i = j; break
        i += 1
    return results

def _build_decision(data):
    if not isinstance(data, dict): return None
    agent = str(data.get("next_agent", "")).lower().strip()
    if agent not in VALID_AGENTS: return None
    return OrchestratorDecision(
        next_agent=agent,
        reasoning=str(data.get("reasoning", ""))[:500],
        priority=data.get("priority","normal") if data.get("priority") in ("high","normal","low") else "normal",
        context_for_agent=str(data.get("context_for_agent","proceed"))[:500],
    )