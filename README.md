# Agentic SDLC Platform

🚀 **Live Demo:** https://agentic-sdlc-platform.up.railway.app/

An autonomous multi-agent software engineering platform that transforms plain-English project requirements into tested, GitHub-pushed, deployment-ready applications with minimal human intervention.

Built using LangGraph, FastAPI, OpenRouter, and a team of specialized AI agents that collaboratively execute the entire Software Development Life Cycle (SDLC).

## Key Features

* Multi-agent architecture with specialized Business Analyst, Scrum Master, Developer, QA, Git, GitHub, and DevOps agents
* Autonomous ticket generation, planning, implementation, testing, and deployment workflows
* Self-healing Dev ↔ QA feedback loop with automatic retries based on test failures
* Retrieval-Augmented Generation (RAG) powered context-aware code generation
* OpenRouter LLM routing with automatic model failover
* Real-time execution monitoring via Server-Sent Events (SSE)
* Automated Git commits, GitHub pushes, pull requests, Dockerfile generation, and CI/CD setup
* One-click cloud deployment using Railway

## Architecture Overview

The platform simulates a complete software engineering team:

Project Idea
↓
Clarifier Agent
↓
Business Analyst Agent
↓
Scrum Master Agent
↓
Developer Agent
↓
QA Engineer Agent
↓
Git Agent
↓
GitHub Agent
↓
DevOps Agent
↓
Deployment Ready Application

Unlike traditional code-generation tools, the platform validates every generated feature through a QA feedback cycle. Failed tests are automatically returned to the Developer Agent, enabling iterative code regeneration until quality requirements are satisfied.

## Live Workflow

1. User submits a project description
2. Clarifier Agent identifies ambiguity and gathers missing requirements
3. Business Analyst generates user stories and acceptance criteria
4. Scrum Master creates sprint tickets with story points
5. Developer Agent implements ticket functionality
6. QA Agent executes tests and validates acceptance criteria
7. Failed implementations are automatically re-generated
8. Git and GitHub Agents manage version control
9. DevOps Agent creates deployment artifacts
10. Application is ready for deployment

## Tech Stack

### AI & Agent Orchestration

* LangGraph
* LangChain
* OpenRouter
* Multi-Agent Systems

### Backend

* FastAPI
* Python 3.11+
* Server-Sent Events (SSE)

### DevOps

* Docker
* GitHub Actions
* Railway

### AI Engineering

* RAG
* Vector Search
* Context Management
* Autonomous Workflow Execution


## Project structure

```
agentic_platform_v5/
├── api/
│   └── main.py              # FastAPI app + SSE streaming endpoints
├── agents/
│   ├── clarifier_agent.py
│   ├── ba_agent.py
│   ├── scrum_agent.py
│   ├── dev_agent.py
│   ├── qa_agent.py
│   ├── git_agent.py
│   ├── github_agent.py
│   ├── devops_agent.py
│   └── stub_agents.py
├── orchestrator/
│   ├── orchestrator_node.py # routing logic + ticket lifecycle
│   ├── prompts.py           # LLM prompts
│   └── state.py             # LangGraph state schema
├── graph/
│   └── graph.py             # LangGraph pipeline definition
├── index.html               # Frontend (served by FastAPI)
├── requirements.txt
├── railway.toml
└── .env                     # local secrets (never committed)
```

## Local setup

### 1. Clone and create virtualenv

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO/agentic_platform_v5
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create `.env` file

Create a file named `.env` in the `agentic_platform_v5/` directory (same folder as `api/`):

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_REPO=your-username/your-repo-name
```

**Getting these keys:**

| Key | Where to get it |
|-----|----------------|
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) — free tier available |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token → tick **repo** scope |
| `GITHUB_REPO` | Format: `username/repo-name` — the repo where code will be pushed |

### 4. Run locally

```bash
cd agentic_platform_v5
uvicorn api.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000)

## Deployment on Railway

### Step 1 — Push to GitHub (see commit steps below)

### Step 2 — Create Railway project

1. Go to [railway.app](https://railway.app) and sign in with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your repository
4. Railway auto-detects Python via nixpacks — no configuration needed

### Step 3 — Add environment variables on Railway

In your Railway project → **Variables** tab, add:

```
OPENROUTER_API_KEY = sk-or-v1-xxxxxxxxxxxxxxxx
GITHUB_TOKEN       = ghp_xxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_REPO        = your-username/your-repo-name
```

> ⚠️ Never put real keys in `railway.toml` or any committed file. Railway Variables are encrypted at rest.

### Step 4 — Deploy

Railway deploys automatically on every push to your main branch. Your app will be live at:
```
https://your-project-name.up.railway.app
```

The frontend and backend share the same URL — no CORS issues.

## How the self-healing loop works

```
Developer writes code
        ↓
QA runs pytest
        ↓
   Tests pass? ──Yes──→ Next ticket
        ↓ No
   (up to 3 attempts)
QA error report → Developer
Developer rewrites with error context
        ↓
QA runs again
        ↓
   Still failing after 3 attempts? → Skip ticket, continue
```

The QA failure output (exact pytest stderr) is injected directly into the Developer's next prompt. The Developer sees which tests failed and why, and rewrites accordingly — not a patch, a full informed retry.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the frontend HTML |
| `POST` | `/run/stream` | Start pipeline, returns SSE stream |
| `POST` | `/clarify/stream` | Submit clarification answers, returns SSE stream |

### SSE event types

```json
{ "type": "pipeline_start" }
{ "type": "awaiting_clarification", "questions": [...], "clarity_score": 42 }
{ "type": "orchestrator", "next_agent": "developer", "reasoning": "..." }
{ "type": "agent_done", "agent": "qa", "status": "ok", "detail": { ... } }
{ "type": "pipeline_done", "duration_seconds": 94.2 }
{ "type": "error", "message": "..." }
```

## Known limitations

- **OpenRouter free tier**: 50 requests/day limit — for serious use, add credits or use your own API keys
- **GitHub token scope**: needs `repo` scope; if you want CI files pushed, also tick `workflow`
- **QA runs locally**: tests execute in a temp directory on the server — don't run on untrusted inputs in production without sandboxing
- **No persistence**: pipeline state lives in memory; server restart clears it

## License

MIT