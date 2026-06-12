"""
run_cli.py — CLI runner for the agentic SDLC platform.

Usage:
  python run_cli.py
  python run_cli.py "Build a todo app with user auth"
"""

import sys
import json
import logging
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
console = Console()


# ── Detail printers ────────────────────────────────────────────────────────────

def _print_ba_detail(artifacts):
    for a in artifacts:
        if a.agent == "ba" and a.status == "ok":
            try:
                spec = json.loads(a.content)
                console.print("\n[bold cyan]━━━ BA Spec Detail ━━━[/bold cyan]")
                console.print(f"[bold]Project:[/bold] {spec.get('project_title', '')}")
                console.print(f"[bold]Summary:[/bold] {spec.get('summary', '')}\n")
                stories = spec.get("user_stories", [])
                console.print(f"[bold]User Stories ({len(stories)})[/bold]")
                for us in stories:
                    console.print(f"  [cyan]{us['id']}[/cyan] As a [italic]{us['role']}[/italic], I want to {us['goal']}")
                    for ac in us.get("acceptance_criteria", []):
                        console.print(f"    [green]✓[/green] {ac}")
                console.print(f"\n[bold]Edge Cases[/bold]")
                for ec in spec.get("edge_cases", []):
                    console.print(f"  [yellow]⚠[/yellow] {ec}")
                tech = spec.get("tech_stack_hints", [])
                if tech:
                    console.print(f"\n[bold]Tech Stack:[/bold] {', '.join(tech)}")
            except Exception as e:
                console.print(f"[red]Could not parse BA spec: {e}[/red]")
            break


def _print_scrum_detail(artifacts):
    for a in artifacts:
        if a.agent == "scrum" and a.status == "ok":
            try:
                plan = json.loads(a.content)
                console.print("\n[bold cyan]━━━ Scrum Plan Detail ━━━[/bold cyan]")
                console.print(f"[bold]Epics ({len(plan.get('epics', []))})[/bold]")
                for ep in plan.get("epics", []):
                    console.print(f"  [magenta]{ep['id']}[/magenta] {ep['title']}")
                console.print(f"\n[bold]Tickets ({len(plan.get('tickets', []))})[/bold]")
                for tk in plan.get("tickets", []):
                    pts = tk.get("story_points", "?")
                    pri = tk.get("priority", "")
                    color = {"high": "red", "medium": "yellow", "low": "green"}.get(pri, "white")
                    console.print(f"  [cyan]{tk['id']}[/cyan] [{color}]{pri}[/{color}] [dim]{pts}pt[/dim] {tk['title']}")
                console.print(f"\n[bold]Sprints[/bold]")
                for sp in plan.get("sprints", []):
                    console.print(f"  [bold]Sprint {sp['sprint_number']}[/bold] ({sp.get('total_points', 0)} pts) — {sp['goal']}")
                    for tid in sp.get("ticket_ids", []):
                        console.print(f"    • {tid}")
            except Exception as e:
                console.print(f"[red]Could not parse Scrum plan: {e}[/red]")
            break


def _print_dev_detail(artifacts):
    for a in artifacts:
        if a.agent == "developer" and a.status == "ok":
            try:
                pkg = json.loads(a.content)
                console.print("\n[bold cyan]━━━ Developer Output ━━━[/bold cyan]")
                console.print(f"[bold]Language:[/bold] {pkg.get('language','?')} / {pkg.get('framework','?')}")
                console.print(f"[bold]Run:[/bold]  {pkg.get('run_command') or '(not specified)'}")
                console.print(f"[bold]Test:[/bold] {pkg.get('test_command') or '(not specified)'}")
                console.print(f"\n[bold]Files generated ({len(pkg.get('files', []))})[/bold]")
                for f in pkg.get("files", []):
                    content = f.get("content", "")
                    lines = content.count("\\n") if ("\\n" in content and "\n" not in content) else content.count("\n")
                    console.print(f"  [green]✓[/green] {f['path']}  [dim]({lines} lines)[/dim]")
                if pkg.get("notes"):
                    console.print(f"\n[bold]Notes:[/bold] {pkg['notes']}")
            except Exception as e:
                console.print(f"[red]Could not parse dev output: {e}[/red]")
            break


def _print_qa_detail(artifacts):
    for a in artifacts:
        if a.agent == "qa":
            try:
                report = json.loads(a.content)
                status = report.get("overall_status", "?")
                color = "green" if status == "pass" else "red"
                tests_run = report.get("tests_run", 0)
                tests_passed = report.get("tests_passed", 0)
                collection_failed = report.get("collection_failed", False)

                console.print(f"\n[bold cyan]━━━ QA Test Report ━━━[/bold cyan]")
                console.print(f"[bold]Result:[/bold] [{color}]{status.upper()}[/{color}]")

                if collection_failed:
                    console.print(f"[red bold]⛔ Test collection failed — import error in generated code[/red bold]")
                    console.print(f"[dim]Exact error passed back to Dev agent for targeted fix[/dim]")
                elif tests_run == 0:
                    console.print(f"[yellow]⚠ No tests were executed[/yellow]")
                else:
                    console.print(f"[bold]Tests:[/bold] {tests_passed} passed / {tests_run} total")

                if report.get("root_cause"):
                    console.print(f"\n[bold]Root cause:[/bold] {report['root_cause']}")
                if report.get("failure_summary"):
                    console.print(f"\n[bold]Failure detail:[/bold]")
                    console.print(report["failure_summary"][:400])
                recs = report.get("recommendations", [])
                if recs:
                    console.print(f"\n[bold]Recommendations[/bold]")
                    for r in recs:
                        console.print(f"  → {r}")
            except Exception as e:
                console.print(f"[red]Could not parse QA report: {e}[/red]")
            break


def _print_git_detail(artifacts):
    for a in artifacts:
        if a.agent == "git" and a.status == "ok":
            try:
                report = json.loads(a.content)
                console.print(f"\n[bold cyan]━━━ Git Repository ━━━[/bold cyan]")
                console.print(f"[bold]Project:[/bold] {report.get('project_title', '?')}  [bold]Tag:[/bold] {report.get('tag', '?')}")
                console.print(f"[bold]Files:[/bold] {report.get('file_count', 0)}  [bold]Lines:[/bold] {report.get('line_count', 0)}")
                console.print(f"\n[bold]Commits[/bold]")
                for c in report.get("commits", []):
                    console.print(f"  [green]●[/green] {c['message']}")
                git_log = report.get("git_log", "")
                if git_log:
                    console.print(f"\n[bold]git log --oneline[/bold]")
                    for line in git_log.split("\n")[:8]:
                        console.print(f"  [dim]{line}[/dim]")
                console.print(f"\n[dim]Repo: {report.get('repo_path', '')}[/dim]")
            except Exception as e:
                console.print(f"[red]Could not parse git report: {e}[/red]")
            break


def _print_devops_detail(artifacts):
    for a in artifacts:
        if a.agent == "devops" and a.status == "ok":
            try:
                report = json.loads(a.content)
                status = report.get("status", "?")
                color = "green" if status == "ready" else "yellow"
                console.print(f"\n[bold cyan]━━━ DevOps Deployment Package ━━━[/bold cyan]")
                console.print(f"[bold]Status:[/bold] [{color}]{status}[/{color}]  [bold]QA:[/bold] {report.get('qa_status','?')}  [bold]Tests:[/bold] {report.get('tests_passed',0)}/{report.get('tests_run',0)}")
                console.print(f"[bold]Docker:[/bold] {'✓ available' if report.get('docker_available') else '✗ not found — install Docker to build'}")
                console.print(f"\n[bold]Files generated[/bold]")
                for f in report.get("deployment_files", []):
                    console.print(f"  [green]✓[/green] {f}")
                cmds = report.get("run_commands", {})
                console.print(f"\n[bold]Run commands[/bold]")
                for mode, cmd in cmds.items():
                    console.print(f"  [cyan]{mode}:[/cyan] {cmd}")
                endpoints = report.get("endpoints", {})
                console.print(f"\n[bold]Endpoints[/bold]")
                for name, url in endpoints.items():
                    console.print(f"  [blue]{name}:[/blue] {url}")
                if report.get("notes"):
                    console.print(f"\n[dim]{report['notes']}[/dim]")
            except Exception as e:
                console.print(f"[red]Could not parse devops report: {e}[/red]")
            break


# ── Main runner ────────────────────────────────────────────────────────────────

def run(project_input: str):
    from graph import get_graph

    console.print(Panel(f"[bold]Project Input[/bold]\n{project_input}", style="blue"))
    graph = get_graph()
    console.print("\n[bold yellow]Running pipeline...[/bold yellow]\n")

    seen_artifacts = set()
    final_state = None

    for event in graph.stream(
        {
            "project_input": project_input,
            "artifacts": [],
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "max_retries": 3,
        },
        stream_mode="values",
        config={"recursion_limit": 40},
    ):
        final_state = event
        decision = event.get("current_decision")
        artifacts = event.get("artifacts", [])

        if decision:
            console.print(
                f"  [purple]Orchestrator[/purple] → [green]{decision.next_agent}[/green]"
                f"  |  {decision.reasoning}"
            )

        for a in artifacts:
            key = (a.agent, a.artifact_type, a.status)
            if key not in seen_artifacts:
                seen_artifacts.add(key)
                sc = "green" if a.status == "ok" else "red"
                console.print(
                    f"  [cyan]{a.agent}_agent[/cyan] produced: "
                    f"[bold]{a.artifact_type}[/bold] [[{sc}]{a.status}[/{sc}]]"
                )

    if final_state is None:
        console.print("[red]Pipeline produced no output.[/red]")
        return

    artifacts = final_state.get("artifacts", [])
    final_output = final_state.get("final_output")
    errors = final_state.get("errors", [])

    # Summary table — latest per agent+type
    table = Table(title="Pipeline Summary", show_header=True)
    table.add_column("Agent", style="cyan")
    table.add_column("Artifact type")
    table.add_column("Status")
    table.add_column("Preview")

    seen = {}
    for a in artifacts:
        seen[(a.agent, a.artifact_type)] = a
    for a in seen.values():
        sc = "green" if a.status == "ok" else "red"
        table.add_row(a.agent, a.artifact_type, f"[{sc}]{a.status}[/{sc}]", a.content[:60] + "...")

    console.print("\n")
    console.print(table)

    if final_output:
        console.print(Panel(final_output, title="Final Output", style="green"))
    if errors:
        console.print(Panel("\n".join(errors), title="Errors", style="red"))

    # Detailed outputs — latest successful per agent (QA shown even if fail)
    latest = {}
    for a in artifacts:
        if a.status == "ok":
            latest[a.agent] = a
    for a in artifacts:
        if a.agent == "qa":
            latest["qa"] = a  # always show QA

    all_artifacts = list(latest.values())
    _print_ba_detail(all_artifacts)
    _print_scrum_detail(all_artifacts)
    _print_dev_detail(all_artifacts)
    _print_qa_detail(all_artifacts)
    _print_git_detail(all_artifacts)
    _print_devops_detail(all_artifacts)


if __name__ == "__main__":
    default_input = (
        "Build a simple REST API for a task management app. "
        "Users can create, update, delete, and list tasks. "
        "Each task has a title, description, status, and due date."
    )
    project = sys.argv[1] if len(sys.argv) > 1 else default_input
    run(project)
