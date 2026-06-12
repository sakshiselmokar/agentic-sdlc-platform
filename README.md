# Agentic SDLC Platform

> An end-to-end autonomous software development platform where AI agents handle every role in the software development lifecycle — from requirements to deployed code — with zero manual intervention.

Built with **LangGraph + FastAPI + OpenRouter free models**.

---

## What it does

You give it a plain-English project description. It does everything else:

```
"Build a task management REST API with user auth"
           ↓
  BA Agent         → structured spec, user stories, acceptance criteria
  Scrum Agent      → epics, tickets, story points, sprint plan
  Developer Agent  → working FastAPI code (7 files)
  QA Agent         → runs pytest, reports pass/fail with root cause
  Git Agent        → real git repo, commits per sprint, v0.1.0 tag
  DevOps Agent     → Dockerfile, docker-compose.yml, Makefile
           ↓
  Complete project ready to run
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Orchestrator                        │
│   LLM decides next agent → Python validates → executes  │
└──────────┬──────────────────────────────────────────────┘
           │
    ┌──────▼──────────────────────────────────────┐
    │           LangGraph StateGraph               │
    │                                             │
    │  BA → Scrum → Dev → QA → Git → DevOps      │
    │         ↑___________↓  (fix loop)           │
    └─────────────────────────────────────────────┘
           │
    ┌──────▼──────────────────────────────────────┐
    │           Shared Platform State              │
    │   artifacts · messages · errors · decisions  │
    └─────────────────────────────────────────────┘
```

### Routing: truly agentic

The orchestrator LLM decides what agent runs next. Python safety rules validate the decision and override only when a loop is detected:

- LLM says `qa` but QA already failed 3 times → override to `devops`
- LLM says `developer` but dev ran 6 times → override to `devops`  
- LLM says `devops` but QA hasn't run yet → override to `qa`
- Otherwise → **LLM decision accepted** ✓

---

## Project structure

```
agentic_platform/
│
├── orchestrator/
│   ├── state.py              # PlatformState, Artifact, OrchestratorDecision (Pydantic)
│   ├── orchestrator_node.py  # LangGraph node + hybrid LLM/Python routing
│   ├── prompts.py            # System prompt + dynamic user prompt builder
│   └── llm_client.py         # OpenRouter client (free models, cached)
│
├── agents/
│   ├── ba_agent.py           # Business Analyst — parses input → structured spec
│   ├── scrum_agent.py        # Scrum Master — spec → epics, tickets, sprint plan
│   ├── dev_agent.py          # Developer — tickets → FastAPI code (7 files)
│   ├── qa_agent.py           # QA Engineer — runs pytest, collection check, LLM analysis
│   ├── git_agent.py          # Git — creates repo, commits per sprint, tags release
│   ├── devops_agent.py       # DevOps — Dockerfile, docker-compose, Makefile
│   └── stub_agents.py        # Finalize + error handler nodes
│
├── api/
│   └── main.py               # FastAPI server — /run, /run/stream, /health
│
├── graph.py                  # LangGraph StateGraph — wires all nodes + edges
├── run_cli.py                # CLI runner with rich output
├── requirements.txt
└── .env.example
```

---

## What changed from the original (v1 → v2)

| File | What changed |
|------|-------------|
| `agents/git_agent.py` | **NEW** — creates real git repo, commits per sprint, tags v0.1.0, generates README |
| `agents/devops_agent.py` | **NEW** — generates Dockerfile, docker-compose.yml, Makefile, .env.example |
| `agents/qa_agent.py` | Added **collection check** — runs `pytest --collect-only` first; extracts exact ImportError and sends to Dev agent before wasting a full test run |
| `agents/dev_agent.py` | Improved QA feedback loop — detects `collection_failed` and passes exact import error with `CRITICAL:` prefix; added safe test pattern rules to system prompt |
| `orchestrator/orchestrator_node.py` | Restored **truly agentic hybrid routing** — LLM decides, Python validates; added git agent routing (QA pass → git → devops); fixed finalize condition |
| `orchestrator/state.py` | Added `"git"` to `AgentName` literal type |
| `orchestrator/prompts.py` | Artifact summary now **deduplicated** — shows only latest per agent, keeps orchestrator context window short |
| `graph.py` | Registered `git_agent` and `devops_agent` nodes; added edges |
| `run_cli.py` | Added `_print_git_detail()` and `_print_devops_detail()` printers; improved QA section shows `⛔ collection failed` clearly |

---

## Quick start

**1. Get a free OpenRouter API key**

Go to [openrouter.ai](https://openrouter.ai) → Sign up → Keys → Create key. Free, no credit card.

**2. Configure**

```bash
cp .env.example .env
# Edit .env and set:
# OPENROUTER_API_KEY=sk-or-v1-your-key-here
# ORCHESTRATOR_MODEL=mistralai/mistral-7b-instruct:free
# AGENT_MODEL=mistralai/mistral-7b-instruct:free
```

**3. Install dependencies**

```bash
python -m venv venv
venv\Scripts\activate     # Windows
# source venv/bin/activate  # Mac/Linux

pip install -r requirements.txt
```

**4. Run**

```bash
# CLI (shows full pipeline with rich output)
python run_cli.py "Build a task management REST API with user auth"

# Or start the API server
uvicorn api.main:app --reload --port 8000
# POST http://localhost:8000/run  {"project_input": "Build a..."}
```

---

## Free model notes

OpenRouter free tier: **50 requests/day** per model. A full pipeline run uses ~15–25 requests.

Recommended free models (set in `.env`):

| Model | Best for | Notes |
|-------|----------|-------|
| `mistralai/mistral-7b-instruct:free` | Reliable default | Most consistent JSON output |
| `meta-llama/llama-3.3-70b-instruct:free` | Better reasoning | Hits rate limits faster |
| `openrouter/auto` | Auto-selects | Spreads load across free models |

If you hit the daily limit: wait until midnight UTC, or add $5 credits to OpenRouter (gives 1000 req/day).

---

## Sample output

```
━━━ BA Spec Detail ━━━
Project: TaskAPI
User Stories (5)
  US-001 As a user, I want to create task
    ✓ valid auth token required
    ✓ task title required
    ✓ returns 201 with task ID

━━━ Scrum Plan Detail ━━━
Epics (3) · Tickets (9) · 2 Sprints

━━━ Developer Output ━━━
Language: python / fastapi
Files generated (7)
  ✓ main.py        (93 lines)
  ✓ tests/test_api.py  (36 lines)

━━━ QA Test Report ━━━
Result: FAIL
Tests: 1 passed / 3 total
Root cause: datetime.utcnow() causes JWT timezone mismatch

━━━ Git Repository ━━━
Project: TaskAPI  Tag: v0.1.0
Commits
  ● Sprint 1: Auth foundation and task creation
  ● Sprint 2: CRUD completion and edge cases

━━━ DevOps Deployment Package ━━━
Status: deployed_with_warnings  QA: fail
Files: Dockerfile · docker-compose.yml · Makefile · .env.example
Run: docker-compose up --build
```

---

## Extending the platform

Adding a new agent takes 3 steps:

1. **Create `agents/your_agent.py`** with a `your_agent_node(state) -> dict` function
2. **Register in `graph.py`** — `graph.add_node("your_agent", your_agent_node)` + add edge back to orchestrator
3. **Add to routing** in `orchestrator/orchestrator_node.py` — add to `route_map` and add a routing rule

The orchestrator LLM will automatically learn to use it from the system prompt in `orchestrator/prompts.py`.

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `GET` | `/graph/info` | Shows compiled graph nodes |
| `POST` | `/run` | Run full pipeline, returns when done |
| `POST` | `/run/stream` | Same but streams NDJSON events |

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"project_input": "Build a blog API with posts and comments"}'
```

---

## Built with

- [LangGraph](https://github.com/langchain-ai/langgraph) — agent graph orchestration
- [LangChain](https://github.com/langchain-ai/langchain) — LLM abstraction
- [FastAPI](https://fastapi.tiangolo.com) — API server
- [OpenRouter](https://openrouter.ai) — free LLM access (Mistral, Llama, Gemini, etc.)
- [Rich](https://github.com/Textualize/rich) — terminal output
- [Pydantic](https://docs.pydantic.dev) — data validation
