# Agentic SDLC Platform

An autonomous software development platform that takes a plain-English project description and produces a fully tested, GitHub-pushed, deployment-ready codebase — without any human intervention.

## What it does

You describe a project. The platform runs a full software development lifecycle automatically:

1. **Clarifier** — scores input ambiguity, asks targeted questions if needed
2. **Business Analyst** — parses requirements into user stories and acceptance criteria
3. **Scrum Master** — breaks the spec into sprint tickets with story points
4. **Developer** — writes code for each ticket
5. **QA Engineer** — runs pytest against every ticket; feeds failures back to Developer for self-healing retries (up to 3 attempts per ticket)
6. **Git Agent** — commits each sprint to a local git repo with tags
7. **GitHub Agent** — pushes to your GitHub repository and opens a pull request
8. **DevOps Agent** — generates Dockerfile, docker-compose, and CI/CD pipeline

The Dev↔QA loop is the core: if tests fail, the exact error output is sent back to the Developer, which rewrites the code and tries again. No patching — full rewrites with context.

## Tech stack

- **Backend**: FastAPI + LangGraph (agentic pipeline) + Python 3.11+
- **LLM routing**: OpenRouter (free models with automatic failover)
- **Frontend**: Single HTML file — no build step, no dependencies
- **Deployment**: Railway (one-click from GitHub)

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