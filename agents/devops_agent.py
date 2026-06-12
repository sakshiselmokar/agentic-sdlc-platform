"""
devops_agent.py — DevOps agent.

Generates a complete deployment package:
  - Dockerfile (multi-stage, production ready)
  - docker-compose.yml (app + optional services)
  - .env.example (all required env vars)
  - deploy_report.json (what was generated + how to run)

Does NOT need an LLM — it generates deterministic deployment
files from the code artifact metadata.
"""

import json
import logging
import os
import subprocess
import tempfile
from orchestrator.state import PlatformState, Artifact

logger = logging.getLogger(__name__)


def devops_agent_node(state: PlatformState) -> dict:
    """LangGraph node for the DevOps agent."""
    logger.info("[DevOps Agent] Generating deployment package...")

    code_pkg = _get_code_artifact(state)
    ba_spec = _get_artifact(state, "ba", "spec")
    qa_report = _get_qa_report(state)

    project_title = "app"
    if ba_spec:
        raw = ba_spec.get("project_title", "app")
        project_title = raw.lower().replace(" ", "-").replace("_", "-")

    run_cmd = "uvicorn main:app --host 0.0.0.0 --port 8000"
    if code_pkg:
        raw_cmd = code_pkg.get("run_command", "")
        if raw_cmd:
            run_cmd = raw_cmd.replace("--reload", "").strip()
            if "--host" not in run_cmd:
                run_cmd += " --host 0.0.0.0 --port 8000"

    # ── Generate all deployment files ─────────────────────────────────────────
    dockerfile = _generate_dockerfile(run_cmd)
    compose = _generate_compose(project_title)
    env_example = _generate_env_example()
    makefile = _generate_makefile(project_title)

    # ── Write to temp dir and verify Docker is available ──────────────────────
    work_dir = tempfile.mkdtemp(prefix="devops_agent_")
    deploy_files = [
        ("Dockerfile", dockerfile),
        ("docker-compose.yml", compose),
        (".env.example", env_example),
        ("Makefile", makefile),
    ]
    for filename, content in deploy_files:
        with open(os.path.join(work_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)

    docker_available = _check_docker()

    # ── Build report ───────────────────────────────────────────────────────────
    qa_status = "unknown"
    if qa_report:
        qa_status = qa_report.get("overall_status", "unknown")
        tests_passed = qa_report.get("tests_passed", 0)
        tests_run = qa_report.get("tests_run", 0)
    else:
        tests_passed = 0
        tests_run = 0

    deploy_report = {
        "project": project_title,
        "status": "ready" if qa_status == "pass" else "deployed_with_warnings",
        "qa_status": qa_status,
        "tests_passed": tests_passed,
        "tests_run": tests_run,
        "docker_available": docker_available,
        "deployment_files": [f for f, _ in deploy_files],
        "run_commands": {
            "local":  f"uvicorn main:app --reload",
            "docker": f"docker-compose up --build",
            "make":   "make run",
        },
        "endpoints": {
            "local":  "http://localhost:8000",
            "docs":   "http://localhost:8000/docs",
            "health": "http://localhost:8000/api/v1/health",
        },
        "generated_files": {
            "Dockerfile":         dockerfile,
            "docker-compose.yml": compose,
            ".env.example":       env_example,
            "Makefile":           makefile,
        },
        "notes": (
            "QA tests passed — production ready." if qa_status == "pass"
            else f"QA status: {qa_status}. Review test failures before deploying to production."
        ),
    }

    logger.info(f"[DevOps Agent] Package ready for: {project_title} | QA: {qa_status}")

    artifact = Artifact(
        agent="devops",
        artifact_type="deploy_log",
        content=json.dumps(deploy_report, indent=2),
        status="ok",
        metadata={
            "project": project_title,
            "qa_status": qa_status,
            "docker_available": docker_available,
            "files_generated": len(deploy_files),
        },
    )
    return {
        "artifacts": state.artifacts + [artifact],
        "active_agent": "devops",
        "current_decision": None,
    }


# ── File generators ────────────────────────────────────────────────────────────

def _generate_dockerfile(run_cmd: str) -> str:
    return f"""# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \\
    pip install --no-cache-dir -r requirements.txt

# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Own the files
RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\
  CMD python -c "import httpx; httpx.get('http://localhost:8000/api/v1/health').raise_for_status()"

CMD ["{run_cmd.split()[0]}", {", ".join(f'"{p}"' for p in run_cmd.split()[1:])}]
"""


def _generate_compose(project: str) -> str:
    return f"""version: "3.9"

services:
  app:
    build: .
    container_name: {project}_app
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - ./data:/app/data          # persist SQLite DB
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c",
             "import httpx; httpx.get('http://localhost:8000/api/v1/health').raise_for_status()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

# Uncomment to add Redis for caching / rate limiting
#  redis:
#    image: redis:7-alpine
#    container_name: {project}_redis
#    ports:
#      - "6379:6379"
#    restart: unless-stopped
"""


def _generate_env_example() -> str:
    return """# Application
SECRET_KEY=change-me-to-a-long-random-string-in-production
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# Database
DATABASE_URL=sqlite:///./app.db

# Server
HOST=0.0.0.0
PORT=8000
DEBUG=false

# CORS (comma-separated origins)
ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8080
"""


def _generate_makefile(project: str) -> str:
    return f"""# {project} — Makefile

.PHONY: install run test docker-build docker-run docker-stop clean

install:
\tpip install -r requirements.txt

run:
\tuvicorn main:app --reload --host 0.0.0.0 --port 8000

test:
\tpytest tests/ -v

docker-build:
\tdocker-compose build

docker-run:
\tdocker-compose up -d

docker-stop:
\tdocker-compose down

logs:
\tdocker-compose logs -f app

clean:
\tfind . -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null || true
\tfind . -name "*.pyc" -delete 2>/dev/null || true
\trm -f *.db
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _check_docker() -> bool:
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_code_artifact(state: PlatformState) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == "developer" and a.artifact_type == "code" and a.status == "ok":
            try:
                return json.loads(a.content)
            except json.JSONDecodeError:
                return None
    return None


def _get_qa_report(state: PlatformState) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == "qa" and a.artifact_type == "test_report":
            try:
                return json.loads(a.content)
            except json.JSONDecodeError:
                return None
    return None


def _get_artifact(state: PlatformState, agent: str, artifact_type: str) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == agent and a.artifact_type == artifact_type and a.status == "ok":
            try:
                return json.loads(a.content)
            except json.JSONDecodeError:
                return None
    return None
