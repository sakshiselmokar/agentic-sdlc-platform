"""
qa_agent.py — QA Engineer agent (v3 ticket-loop refactor).

Core change from v2:
  - Tests ONE ticket at a time (reads state.current_ticket_id).
  - Looks up that ticket's acceptance_criteria from the scrum artifact.
  - Runs the ticket-specific test file (tests/test_<ticket_id>.py) first,
    then the full test suite to ensure no regressions.
  - On PASS: merges the ticket's code into sprint_committed_files and marks
    the ticket complete in completed_ticket_ids, then clears current_ticket_id
    so the orchestrator picks the next ticket.
  - On FAIL: stores ticket_id in artifact metadata so dev agent
    can find the right failure context.

Flow:
  1. Resolve current ticket + get its acceptance criteria
  2. Write all files from the latest dev artifact to disk
  3. Install pinned dependencies
  4. Collection check (catch import/syntax errors early)
  5. Run ticket-specific tests first, then full suite
  6. LLM analysis on failure
  7. Return artifact + state updates
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from langchain_core.messages import HumanMessage, SystemMessage
from orchestrator.state import PlatformState, Artifact
from orchestrator.llm_client import get_agent_llm

logger = logging.getLogger(__name__)

# Pinned Python 3.13-safe versions
SAFE_REQUIREMENTS = """fastapi==0.115.4
uvicorn==0.32.0
sqlalchemy==2.0.36
pydantic==2.9.2
python-jose==3.3.0
cryptography==43.0.3
passlib==1.7.4
bcrypt==4.2.0
pytest==8.3.3
httpx==0.27.2
anyio==4.6.2
pytest-asyncio==0.24.0
starlette==0.41.2
"""

QA_ANALYSIS_PROMPT = """You are a senior QA engineer. Analyse a pytest failure and produce a JSON report.

Output ONLY valid JSON — no markdown, no explanation.

Schema:
{
  "overall_status": "pass | fail",
  "tests_run": 5,
  "tests_passed": 3,
  "tests_failed": 2,
  "failure_summary": "brief description of what failed",
  "root_cause": "likely cause of the failure",
  "recommendations": ["fix suggestion 1", "fix suggestion 2"],
  "blocking": true,
  "collection_failed": false
}
"""


# ── Main QA agent node ─────────────────────────────────────────────────────────

def qa_agent_node(state: PlatformState) -> dict:
    logger.info(f"[QA Agent] Starting — ticket: {state.current_ticket_id}")

    # ── Get current ticket info ───────────────────────────────────────────────
    ticket_id   = state.current_ticket_id
    ticket      = _get_current_ticket(state, ticket_id)
    ticket_name = ticket["title"] if ticket else "unknown"

    if ticket_id is None:
        return _fail(state, "No current_ticket_id set — cannot run QA", None)

    # ── Get the code artifact for this ticket ─────────────────────────────────
    code_pkg = _get_code_artifact_for_ticket(state, ticket_id)
    if not code_pkg:
        # Fall back to any latest ok code artifact
        code_pkg = _get_any_code_artifact(state)
    if not code_pkg:
        return _fail(state, f"No code artifact found for ticket {ticket_id}", ticket_id)

    files = code_pkg.get("files", [])
    if not files:
        return _fail(state, f"Code artifact for ticket {ticket_id} has no files", ticket_id)

    # ── Write files to temp directory ─────────────────────────────────────────
    work_dir = tempfile.mkdtemp(prefix="qa_agent_")
    logger.info(f"[QA Agent] Ticket {ticket_id} | Work dir: {work_dir}")

    try:
        _write_files(files, work_dir)
        logger.info(f"[QA Agent] Wrote {len(files)} files")

        _install_deps(work_dir)

        # ── Collection check ──────────────────────────────────────────────────
        collection_ok, collection_error = _check_collection(work_dir, ticket_id)

        if not collection_ok:
            logger.warning(f"[QA Agent] Collection failed for ticket {ticket_id}")
            report = {
                "overall_status": "fail",
                "tests_run": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "failure_summary": collection_error,
                "root_cause": _extract_root_cause(collection_error),
                "recommendations": _extract_fix_recommendations(collection_error),
                "blocking": True,
                "collection_failed": True,
                "ticket_id": ticket_id,
                "acceptance_criteria_tested": _get_acceptance_criteria(ticket),
            }
            artifact = Artifact(
                agent="qa",
                artifact_type="test_report",
                content=json.dumps(report, indent=2),
                status="fail",
                metadata={
                    "ticket_id": ticket_id,
                    "ticket_title": ticket_name,
                    "collection_failed": True,
                    "tests_run": 0,
                },
            )
            logger.info(f"[QA Agent] Ticket {ticket_id}: collection_failed")
            return {
                "artifacts": state.artifacts + [artifact],
                "active_agent": "qa",
                "current_decision": None,
                "current_ticket_id": ticket_id,
            }

        # ── Run tests ─────────────────────────────────────────────────────────
        # First run the ticket-specific test file, then the full suite
        ticket_test_result = _run_ticket_tests(work_dir, ticket_id)
        full_test_result   = _run_all_tests(work_dir)

        # If ticket-specific tests pass but regression exists, report that
        ticket_passed  = ticket_test_result["failed"] == 0 and ticket_test_result["errors"] == 0
        full_passed    = full_test_result["failed"]   == 0 and full_test_result["errors"]   == 0

        # Overall: both must pass for the ticket to be "done"
        overall = "pass" if (ticket_passed and full_passed) else "fail"

        logger.info(
            f"[QA Agent] Ticket {ticket_id}: "
            f"ticket-tests={ticket_test_result['passed']}p/{ticket_test_result['failed']}f | "
            f"full={full_test_result['passed']}p/{full_test_result['failed']}f | "
            f"overall={overall}"
        )

        if overall == "fail":
            # Use whichever result has more failures for the LLM analysis
            failing_result = full_test_result if full_test_result["failed"] > 0 else ticket_test_result
            report = _analyse_failure(failing_result, code_pkg, ticket, state)
        else:
            total_run    = full_test_result["passed"] + full_test_result["failed"]
            total_passed = full_test_result["passed"]
            report = {
                "overall_status": "pass",
                "tests_run": total_run,
                "tests_passed": total_passed,
                "tests_failed": 0,
                "failure_summary": "",
                "root_cause": "",
                "recommendations": [],
                "blocking": False,
                "collection_failed": False,
                "ticket_id": ticket_id,
                "acceptance_criteria_tested": _get_acceptance_criteria(ticket),
                "acceptance_criteria_passed": True,
            }

        artifact = Artifact(
            agent="qa",
            artifact_type="test_report",
            content=json.dumps(report, indent=2),
            status="ok" if overall == "pass" else "fail",
            metadata={
                "ticket_id": ticket_id,
                "ticket_title": ticket_name,
                "tests_run": report["tests_run"],
                "tests_passed": report["tests_passed"],
                "tests_failed": report["tests_failed"],
                "overall_status": overall,
                "collection_failed": False,
            },
        )

        logger.info(
            f"[QA Agent] Ticket {ticket_id}: {overall} — "
            f"{report['tests_passed']}/{report['tests_run']} passed"
        )

        state_update = {
            "artifacts": state.artifacts + [artifact],
            "active_agent": "qa",
            "current_decision": None,
            "current_ticket_id": ticket_id,
        }

        if overall == "pass":
            # Merge this ticket's files into sprint_committed_files
            merged_files = _merge_files(state.sprint_committed_files, files)
            state_update["sprint_committed_files"] = merged_files

            # Mark ticket complete and clear current_ticket_id
            state_update["completed_ticket_ids"] = state.completed_ticket_ids + [ticket_id]
            state_update["current_ticket_id"]    = None
            state_update["ticket_attempt_count"] = 0

            logger.info(
                f"[QA Agent] Ticket {ticket_id} PASSED — "
                f"merged {len(files)} files, "
                f"{len(state_update['completed_ticket_ids'])} tickets done total"
            )

        return state_update

    except Exception as e:
        logger.error(f"[QA Agent] Unexpected error for ticket {ticket_id}: {e}", exc_info=True)
        return _fail(state, str(e), ticket_id)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── File merge helper ──────────────────────────────────────────────────────────

def _merge_files(existing: list, new_files: list) -> list:
    """
    Merge new_files into existing, replacing files at the same path.
    This accumulates the growing codebase as each ticket is completed.
    """
    existing_map = {f["path"]: f for f in existing}
    for f in new_files:
        path = f.get("path")
        if path:
            existing_map[path] = f
    return list(existing_map.values())


# ── Ticket helpers ─────────────────────────────────────────────────────────────

def _get_current_ticket(state: PlatformState, ticket_id: str | None) -> dict | None:
    if not ticket_id:
        return None
    for a in reversed(state.artifacts):
        if a.agent == "scrum" and a.artifact_type == "tasks" and a.status == "ok":
            try:
                tasks = json.loads(a.content)
                for t in tasks.get("tickets", []):
                    if t["id"] == ticket_id:
                        return t
            except Exception:
                pass
    return None


def _get_acceptance_criteria(ticket: dict | None) -> list[str]:
    if not ticket:
        return []
    return ticket.get("acceptance_criteria", [])


# ── Test runners ───────────────────────────────────────────────────────────────

def _run_ticket_tests(work_dir: str, ticket_id: str) -> dict:
    """Run only the test file specific to this ticket."""
    # Ticket test file naming: tests/test_TK_001.py (dashes become underscores)
    safe_id   = ticket_id.replace("-", "_").lower()
    test_file = os.path.join(work_dir, "tests", f"test_{safe_id}.py")

    if not os.path.exists(test_file):
        # Fall back to any test file that mentions the ticket id
        tests_dir   = os.path.join(work_dir, "tests")
        found_files = []
        if os.path.exists(tests_dir):
            for fname in os.listdir(tests_dir):
                if fname.endswith(".py") and safe_id in fname.lower():
                    found_files.append(os.path.join("tests", fname))
        if not found_files:
            # No ticket-specific test — run all tests
            return _run_all_tests(work_dir)
        test_path = found_files[0]
    else:
        test_path = os.path.join("tests", f"test_{safe_id}.py")

    logger.info(f"[QA Agent] Running ticket test: {test_path}")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-v", "--tb=short", "--no-header", "-q"],
        capture_output=True, text=True, timeout=60, cwd=work_dir,
    )
    output = result.stdout + result.stderr
    passed, failed, errors = _parse_pytest_output(output, result.returncode)
    return {"passed": passed, "failed": failed, "errors": errors, "output": output}


def _run_all_tests(work_dir: str) -> dict:
    """Run the full test suite (regression check)."""
    tests_dir = os.path.join(work_dir, "tests")
    if not os.path.exists(tests_dir):
        return {"passed": 0, "failed": 0, "errors": 0, "output": "No tests/ directory"}

    logger.info("[QA Agent] Running full test suite...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "--no-header", "-q"],
        capture_output=True, text=True, timeout=90, cwd=work_dir,
    )
    output = result.stdout + result.stderr
    passed, failed, errors = _parse_pytest_output(output, result.returncode)
    return {"passed": passed, "failed": failed, "errors": errors, "output": output}


def _parse_pytest_output(output: str, returncode: int) -> tuple:
    passed = failed = errors = 0
    for line in output.split("\n"):
        nums = re.findall(r"(\d+)\s+(passed|failed|error)", line)
        for count, kind in nums:
            if "passed" in kind:
                passed = int(count)
            elif "failed" in kind:
                failed = int(count)
            elif "error" in kind:
                errors = int(count)
    if returncode == 0 and passed == 0 and failed == 0:
        passed = 1  # treat as passing if pytest exited 0 with no counts
    return passed, failed, errors


# ── Collection check ───────────────────────────────────────────────────────────

def _check_collection(work_dir: str, ticket_id: str) -> tuple[bool, str]:
    tests_dir = os.path.join(work_dir, "tests")
    if not os.path.exists(tests_dir):
        return False, "No tests/ directory found in generated code"

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "--tb=short"],
        capture_output=True, text=True, timeout=30, cwd=work_dir,
    )
    output = result.stdout + result.stderr

    if result.returncode == 0:
        return True, ""

    error_text = output[-1500:] if len(output) > 1500 else output
    key_lines  = []
    for line in output.split("\n"):
        ls = line.strip()
        if any(k in ls for k in ["ImportError", "ModuleNotFoundError", "SyntaxError",
                                   "cannot import name", "No module named"]):
            key_lines.append(ls)

    if key_lines:
        error_text = "\n".join(key_lines) + "\n\nFull output:\n" + output[-800:]

    return False, error_text


def _extract_root_cause(error_text: str) -> str:
    for line in error_text.split("\n"):
        line = line.strip()
        if "ImportError" in line or "ModuleNotFoundError" in line:
            return line
        if "cannot import name" in line:
            return line
        if "SyntaxError" in line:
            return line
    return "Test collection failed — see failure_summary for details"


def _extract_fix_recommendations(error_text: str) -> list[str]:
    recs = []
    if "cannot import name" in error_text:
        match = re.search(r"cannot import name '(\w+)' from '([\w.]+)'", error_text)
        if match:
            name, module = match.group(1), match.group(2)
            recs.append(f"Add '{name}' to {module}.py — it is imported in tests but not defined")
        else:
            recs.append("Fix missing import — check that all names imported in tests are defined in source files")
    elif "ModuleNotFoundError" in error_text:
        match = re.search(r"No module named '([\w.]+)'", error_text)
        if match:
            recs.append(f"Module '{match.group(1)}' not found — ensure {match.group(1)}.py exists")
        recs.append("Check that all imported modules exist as .py files")
    elif "SyntaxError" in error_text:
        recs.append("Fix syntax error in test file or source file")
    if not recs:
        recs.append("Fix the import/collection error shown in failure_summary")
    return recs


# ── File I/O ───────────────────────────────────────────────────────────────────

def _write_files(files: list, work_dir: str):
    for f in files:
        path    = f.get("path", "")
        content = f.get("content", "")
        if not path:
            continue
        if "\\n" in content and "\n" not in content.replace("\\n", ""):
            content = content.replace("\\n", "\n")
        content   = content.replace("\\t", "\t")
        full_path = os.path.join(work_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fp:
            fp.write(content)


def _install_deps(work_dir: str):
    req_path = os.path.join(work_dir, "requirements.txt")
    with open(req_path, "w", encoding="utf-8") as f:
        f.write(SAFE_REQUIREMENTS)
    logger.info("[QA Agent] Installing pinned dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req_path,
         "--quiet", "--break-system-packages"],
        capture_output=True, text=True, timeout=240,
    )
    if result.returncode != 0:
        logger.warning(f"[QA Agent] pip issues: {result.stderr[:200]}")


# ── LLM failure analysis ───────────────────────────────────────────────────────

def _analyse_failure(test_result: dict, code_pkg: dict, ticket: dict | None, state: PlatformState) -> dict:
    llm          = get_agent_llm(temperature=0.1)
    output_snip  = test_result.get("output", "")[-1200:]
    ticket_id    = state.current_ticket_id or "unknown"
    criteria_str = "\n".join(f"  - {ac}" for ac in _get_acceptance_criteria(ticket))

    prompt = (
        f"Ticket: {ticket_id}\n"
        f"Acceptance criteria that should pass:\n{criteria_str}\n\n"
        f"Pytest output:\n{output_snip}\n\n"
        f"Produce the JSON report now."
    )

    try:
        response = llm.invoke([
            SystemMessage(content=QA_ANALYSIS_PROMPT),
            HumanMessage(content=prompt),
        ])
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1]).strip()
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.index("{")
            depth, end = 0, start
            for i, ch in enumerate(raw[start:], start):
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0: end = i; break
            report = json.loads(raw[start:end + 1])

        report["ticket_id"]                   = ticket_id
        report["acceptance_criteria_tested"]  = _get_acceptance_criteria(ticket)
        return report

    except Exception as e:
        logger.warning(f"[QA Agent] LLM analysis failed: {e}")
        return {
            "overall_status": "fail",
            "tests_run": test_result.get("passed", 0) + test_result.get("failed", 0),
            "tests_passed": test_result.get("passed", 0),
            "tests_failed": test_result.get("failed", 0),
            "failure_summary": test_result.get("output", "")[-400:],
            "root_cause": "Could not analyse automatically",
            "recommendations": ["Review generated code manually"],
            "blocking": True,
            "collection_failed": False,
            "ticket_id": ticket_id,
            "acceptance_criteria_tested": _get_acceptance_criteria(ticket),
        }


# ── Artifact finders ───────────────────────────────────────────────────────────

def _get_code_artifact_for_ticket(state: PlatformState, ticket_id: str) -> dict | None:
    for a in reversed(state.artifacts):
        if (a.agent == "developer" and a.artifact_type == "code"
                and a.status == "ok"
                and a.metadata.get("ticket_id") == ticket_id):
            try:
                return json.loads(a.content)
            except Exception:
                return None
    return None


def _get_any_code_artifact(state: PlatformState) -> dict | None:
    for a in reversed(state.artifacts):
        if a.agent == "developer" and a.artifact_type == "code" and a.status == "ok":
            try:
                return json.loads(a.content)
            except Exception:
                return None
    return None


# ── Error result builder ───────────────────────────────────────────────────────

def _fail(state: PlatformState, reason: str, ticket_id: str | None) -> dict:
    logger.error(f"[QA Agent] {reason}")
    report = {
        "overall_status": "fail",
        "tests_run": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "failure_summary": reason,
        "root_cause": reason,
        "recommendations": ["Fix the issue and retry"],
        "blocking": True,
        "collection_failed": True,
        "ticket_id": ticket_id or "",
    }
    return {
        "artifacts": state.artifacts + [Artifact(
            agent="qa",
            artifact_type="test_report",
            content=json.dumps(report, indent=2),
            status="fail",
            metadata={"error": reason, "ticket_id": ticket_id or ""},
        )],
        "active_agent": "qa",
        "current_decision": None,
        "current_ticket_id": ticket_id,
    }
