from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from newsletter_mcp.config import load_settings
from newsletter_mcp.pipeline import approve_run, next_schedule, repackage_run, run_pipeline
from newsletter_mcp.registry import MCPHub
from newsletter_mcp.state import StateStore

app = typer.Typer(no_args_is_help=True, help="Newsletter automation with nine MCP servers and configurable local/cloud model modes.")
console = Console()


def execute(awaitable):
    try:
        return asyncio.run(awaitable)
    except KeyboardInterrupt:
        console.print("Cancelled.")
        raise typer.Exit(130)
    except Exception as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1)


@app.command()
def run(config: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
        provider: str = typer.Option("", "--provider", help="ollama, groq, hybrid, gemini, openai or claude; defaults to configuration"),
        demo: bool = typer.Option(False, "--demo", help="Synthetic, network-free fixtures through all nine MCP servers")):
    """Research, draft and package one edition. Never sends email."""
    settings = load_settings(config)
    result = execute(run_pipeline(settings, demo=demo, provider=provider or None,
                                  progress=lambda phase: console.print(f"  {phase}")))
    console.print(f"\nRun: {result['run_id']}\nStatus: {result['status']}")
    console.print(f"Artifacts: {settings.workspace / 'issues' / result['run_id']}")
    if result["warnings"]:
        console.print(f"Review {len(result['warnings'])} warnings in quality_report.json.")
    console.print("No email has been sent.")


@app.command()
def doctor(config: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
           demo: bool = typer.Option(False, "--demo")):
    """Test MCP initialization, tool discovery and local credential presence, not provider API access."""
    settings = load_settings(config)
    async def inspect():
        async with MCPHub(settings, demo=demo) as hub:
            return hub.status
    statuses = execute(inspect())
    table = Table(title="MCP connection check - 'ready' is not a live API authorization test")
    table.add_column("Server")
    table.add_column("State")
    table.add_column("Tools")
    for name, status in statuses.items():
        table.add_row(name, status["state"], ", ".join(status.get("tools", [])))
    console.print(table)
    console.print(f"LLM: {settings.llm.provider}. Model inference is checked during a live run.")
    console.print("Optional failures do not block the core workflow. Set required: true for stricter execution.")


@app.command()
def serve(config: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
          host: str = typer.Option("127.0.0.1", "--host"),
          port: int = typer.Option(8000, "--port", min=1, max=65535)):
    """Open the local dashboard. Non-loopback binding requires DASHBOARD_TOKEN."""
    import uvicorn
    from newsletter_mcp.webapp import create_app
    settings = load_settings(config)
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("DASHBOARD_TOKEN"):
        raise typer.BadParameter("Set DASHBOARD_TOKEN before binding beyond loopback")
    console.print(f"Dashboard: http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port)


@app.command()
def approve(run_id: str, reviewer: str = typer.Option(..., "--reviewer"),
            config: Path = typer.Option(Path("config.yaml"), "--config", "-c")):
    """Record editorial approval for unchanged artifacts. Demo editions cannot be approved."""
    result = execute(approve_run(load_settings(config), run_id, reviewer))
    console.print(f"Approved {result['run_id']} by {result['reviewer']}. No email was sent.")


@app.command()
def render(run_id: str, config: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
           provider: str = typer.Option("", "--provider", help="Explicit model-mode override for Phase 3")):
    """Retry Phase 3 from saved Markdown; uses the selected LLM or explicitly enabled Jinja."""
    result = execute(repackage_run(load_settings(config), run_id, provider=provider or None))
    console.print(f"Repackaged {result['run_id']}; review again before approval.")


@app.command()
def runs(config: Path = typer.Option(Path("config.yaml"), "--config", "-c")):
    """List recent editions and their review states."""
    settings = load_settings(config)
    table = Table(title="Newsletter runs")
    for header in ("Run", "State", "Provider", "Sources"):
        table.add_column(header)
    for row in StateStore(settings.workspace).list_runs():
        table.add_row(row["id"], row["status"], row["provider"], str(row["details"].get("source_count", "-")))
    console.print(table)


@app.command()
def schedule(config: Path = typer.Option(Path("config.yaml"), "--config", "-c")):
    """Run a foreground weekly scheduler. Keep this process running; restart after configuration changes."""
    settings = load_settings(config)
    store = StateStore(settings.workspace)
    due = next_schedule(settings)
    console.print(f"Next run: {due.isoformat()}. Scheduling generates drafts only; it never sends email.")
    try:
        while True:
            remaining = (due - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                time.sleep(min(60, remaining))
                continue
            key = due.isoformat()
            if store.claim_schedule(key):
                try:
                    result = asyncio.run(run_pipeline(settings, progress=console.print))
                    console.print(f"Scheduled edition ready for review: {result['run_id']}")
                except Exception as exc:
                    console.print(f"Scheduled run failed: {exc}", markup=False)
            due = next_schedule(settings)
            console.print(f"Next run: {due.isoformat()}")
    except KeyboardInterrupt:
        console.print("Scheduler stopped. Missed runs are not automatically replayed.")


if __name__ == "__main__":
    app()
