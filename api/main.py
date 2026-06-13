"""
api/main.py — FastAPI server exposing the agentic platform.

Endpoints:
  POST /run              → submit a project, run the full pipeline
  POST /run/stream       → same but streams SSE events
  POST /clarify          → resume a paused pipeline with user answers
  POST /clarify/stream   → same but streams SSE
  GET  /health           → liveness check
  GET  /graph/info       → shows the compiled graph structure
"""

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from graph import get_graph
from orchestrator.state import PlatformState

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

from fastapi.staticfiles import StaticFiles
# AFTER
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Agentic SDLC Platform...")
    get_graph()
    logger.info("Graph compiled. Ready.")
    yield
    logger.info("Shutting down.")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Agentic SDLC Platform",
    description="End-to-end autonomous software development — Clarifier → BA → Scrum → Dev → QA → Git → GitHub → DevOps",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# app.mount("/", StaticFiles(directory=".", html=True), name="static")


# ── Request / Response models ──────────────────────────────────────────────────

class ProjectRequest(BaseModel):
    project_input: str
    max_retries: int = 3


class ClarifyRequest(BaseModel):
    project_input: str
    answers: dict[str, str]   # {"Q1": "REST API", "Q2": "JWT", "Q3": "PostgreSQL"}
    max_retries: int = 3


class PipelineResponse(BaseModel):
    run_id: str
    status: str                     # "complete" | "awaiting_clarification"
    final_output: str | None
    artifacts: list[dict]
    errors: list[str]
    duration_seconds: float
    clarifying_questions: list[dict] | None = None   # populated when paused


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_clarifying_questions(final_state: PlatformState) -> list[dict] | None:
    """Pull questions from the clarifier artifact if pipeline paused."""
    for a in final_state.artifacts:
        if a.agent == "clarifier" and a.artifact_type == "clarity_check" and a.status == "ok":
            try:
                data = json.loads(a.content)
                if data.get("verdict") == "clarify":
                    return data.get("clarifying_questions", [])
            except Exception:
                pass
    return None


def _build_sse_stream(initial_state: PlatformState):
    """Generator that runs the graph and yields SSE-formatted events."""
    graph = get_graph()

    yield f"data: {json.dumps({'type': 'pipeline_start'})}\n\n"

    all_seen_artifact_ids: set[int] = set()

    for event in graph.stream(initial_state, stream_mode="updates", config={"recursion_limit": 200}):
        for node_name, state_delta in event.items():

            # ── Clarifier paused ───────────────────────────────────────────────
            if state_delta.get("awaiting_clarification"):
                # Find questions
                questions = []
                for a in state_delta.get("artifacts", []):
                    a_dict = a if isinstance(a, dict) else a.model_dump()
                    if a_dict.get("agent") == "clarifier":
                        try:
                            clarity = json.loads(a_dict.get("content", "{}"))
                            questions = clarity.get("clarifying_questions", [])
                        except Exception:
                            pass
                payload = {
                    "type": "awaiting_clarification",
                    "clarity_score": 0,
                    "questions": questions,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                return

            # ── Orchestrator routing events ────────────────────────────────────
            if node_name == "orchestrator":
                decision = state_delta.get("current_decision")
                if decision:
                    d = decision if isinstance(decision, dict) else decision.model_dump()
                    payload = {
                        "type": "orchestrator",
                        "next_agent": d.get("next_agent"),
                        "reasoning": d.get("reasoning", ""),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

            # ── New artifact from any agent ────────────────────────────────────
            new_artifacts = state_delta.get("artifacts", [])
            for artifact in new_artifacts:
                a_dict = artifact if isinstance(artifact, dict) else artifact.model_dump()
                art_id = id(artifact)
                if art_id in all_seen_artifact_ids:
                    continue
                all_seen_artifact_ids.add(art_id)

                try:
                    detail = json.loads(a_dict.get("content", "{}"))
                except Exception:
                    detail = {}

                payload = {
                    "type": "agent_done",
                    "agent": a_dict.get("agent"),
                    "artifact_type": a_dict.get("artifact_type"),
                    "status": a_dict.get("status"),
                    "content": a_dict.get("content"),
                    "detail": detail,
                }
                yield f"data: {json.dumps(payload)}\n\n"

    yield f"data: {json.dumps({'type': 'pipeline_done', 'artifacts': [], 'errors': [], 'duration_seconds': 0})}\n\n"


# ── Endpoints ──────────────────────────────────────────────────────────────────

# @app.get("/health", methods=["GET", "HEAD"])
# def health():
#     return {"status": "ok", "service": "agentic-sdlc-platform", "version": "0.2.0"}
@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok", "service": "agentic-sdlc-platform", "version": "0.2.0"}

@app.get("/graph/info")
def graph_info():
    g = get_graph()
    return {"nodes": list(g.nodes), "note": "Conditional edges defined in graph.py"}


@app.post("/run", response_model=PipelineResponse)
def run_pipeline(request: ProjectRequest):
    """
    Runs the full pipeline synchronously.
    If input is vague, returns status='awaiting_clarification' with questions.
    """
    run_id = str(uuid.uuid4())[:8]
    logger.info(f"[{run_id}] Pipeline for: {request.project_input[:60]}...")
    start = time.time()

    initial_state = PlatformState(
        project_input=request.project_input,
        max_retries=request.max_retries,
    )

    try:
        graph = get_graph()
        final_state: PlatformState = graph.invoke(initial_state, config={"recursion_limit": 200})
    except Exception as e:
        logger.error(f"[{run_id}] Pipeline crashed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    duration = round(time.time() - start, 2)

    if final_state.awaiting_clarification:
        questions = _extract_clarifying_questions(final_state)
        return PipelineResponse(
            run_id=run_id,
            status="awaiting_clarification",
            final_output=None,
            artifacts=[],
            errors=[],
            duration_seconds=duration,
            clarifying_questions=questions,
        )

    return PipelineResponse(
        run_id=run_id,
        status="complete",
        final_output=final_state.final_output,
        artifacts=[a.model_dump() for a in final_state.artifacts],
        errors=final_state.errors,
        duration_seconds=duration,
    )


@app.post("/run/stream")
def run_pipeline_stream(request: ProjectRequest):
    """Streams pipeline events as SSE."""
    initial_state = PlatformState(
        project_input=request.project_input,
        max_retries=request.max_retries,
    )
    return StreamingResponse(
        _build_sse_stream(initial_state),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/clarify")
def clarify_and_run(request: ClarifyRequest):
    """
    Resume a paused pipeline with the user's clarification answers.
    Injects answers into project_input context and re-runs.
    """
    run_id = str(uuid.uuid4())[:8]
    logger.info(f"[{run_id}] Resuming with clarification answers.")
    start = time.time()

    # Enrich the original input with the answers
    answers_text = "\n".join([f"- {q}: {a}" for q, a in request.answers.items()])
    enriched_input = f"{request.project_input}\n\nClarification answers:\n{answers_text}"

    initial_state = PlatformState(
        project_input=enriched_input,
        max_retries=request.max_retries,
        clarification_answers=request.answers,  # skip clarifier re-check
    )

    try:
        graph = get_graph()
        final_state: PlatformState = graph.invoke(initial_state, config={"recursion_limit": 200})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    duration = round(time.time() - start, 2)

    return PipelineResponse(
        run_id=run_id,
        status="complete",
        final_output=final_state.final_output,
        artifacts=[a.model_dump() for a in final_state.artifacts],
        errors=final_state.errors,
        duration_seconds=duration,
    )


@app.post("/clarify/stream")
def clarify_and_run_stream(request: ClarifyRequest):
    """Resume a paused pipeline with answers, streaming SSE."""
    answers_text = "\n".join([f"- {q}: {a}" for q, a in request.answers.items()])
    enriched_input = f"{request.project_input}\n\nClarification answers:\n{answers_text}"

    initial_state = PlatformState(
        project_input=enriched_input,
        max_retries=request.max_retries,
        clarification_answers=request.answers,
    )

    return StreamingResponse(
        _build_sse_stream(initial_state),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Frontend ──
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_ROOT, "index.html")
if not os.path.exists(_FRONTEND):
    _FRONTEND = os.path.join(_ROOT, "index.html")

@app.get("/")
def serve_frontend():
    return FileResponse(_FRONTEND, media_type="text/html")