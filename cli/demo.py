"""Colored terminal demo. Runs the LangGraph loop in-process (local AWS creds) or against a deployed
orchestrator URL (SSE stream). Usage:

  python -m cli.demo deploy --repo https://github.com/you/sample --instance i-0123456789abcdef0
  python -m cli.demo deploy --repo ... --instance ... --url http://<ecs-express-url>
  python -m cli.demo fingerprint --instance i-...
  python -m cli.demo fixes list | fixes delete <signature> | fixes clear
  python -m cli.demo attempts
"""
from __future__ import annotations

import json
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(add_completion=False, no_args_is_help=True)
fixes_app = typer.Typer(no_args_is_help=True)
app.add_typer(fixes_app, name="fixes")
console = Console()

STYLE = {"info": "white", "warn": "yellow", "error": "bold red", "success": "bold green",
         "rules": "cyan", "kb": "green", "knowledge": "green", "llm": "magenta", "bedrock": "magenta"}
ICON = {"info": "·", "warn": "!", "error": "✗", "success": "✓", "rules": "⚙", "kb": "◆", "knowledge": "◆", "llm": "✦", "bedrock": "✦"}


def print_event(ev: dict) -> None:
    lvl = ev.get("level", "info")
    style = STYLE.get(lvl, "white")
    console.print(f"[dim]{ev.get('t', 0):7.1f}s[/dim] [{style}]{ICON.get(lvl, '·')} {ev.get('node', ''):<12}[/{style}] {ev.get('msg', '')}",
                  highlight=False, soft_wrap=True)


def _banner(req: dict) -> None:
    console.print(Panel(f"[bold]NoMeshOps[/bold] self-healing deploy\n"
                        f"repo      {req['repo_url']}\ninstance  {req['instance_id']}\n"
                        f"verify    start `{req.get('start_command') or '(auto)'}` → GET :{req['app_port']}{req['health_path']}",
                        border_style="blue"))


def _summary(final: dict) -> None:
    ok = final.get("deploy_success")
    console.print(Panel(f"[{'bold green' if ok else 'bold red'}]{'DEPLOY VERIFIED' if ok else 'DEPLOY FAILED'}[/] in {final.get('duration_s', 0)}s"
                        + (f"\n{final.get('failure_reason')}" if final.get("failure_reason") else "")
                        + (f"\nfix stored in DynamoDB: yes" if final.get("stored_fix") else "")
                        + (f"\naudit log: s3://…/{final.get('s3_key')}" if final.get("s3_key") else ""),
                        border_style="green" if ok else "red"))


@app.command()
def deploy(repo: str = typer.Option(..., help="git URL of the project"),
           instance: str = typer.Option(..., help="target EC2 instance id"),
           branch: str = typer.Option(None), start_command: str = typer.Option(None),
           port: int = typer.Option(8000), health_path: str = typer.Option("/health"),
           keep_running: bool = typer.Option(False), preempt: bool = typer.Option(None, help="pre-apply predicted fixes"),
           url: str = typer.Option(None, help="orchestrator base URL; omit to run in-process")):
    req = {"repo_url": repo, "instance_id": instance, "branch": branch, "start_command": start_command,
           "app_port": port, "health_path": health_path, "keep_running": keep_running,
           "preempt_predicted_fixes": preempt}
    _banner(req)
    if url:
        import httpx
        final = {}
        with httpx.stream("POST", f"{url.rstrip('/')}/deploy/stream", json=req, timeout=None) as r:
            r.raise_for_status()
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = json.loads(line.split(":", 1)[1])
                    if event == "log":
                        print_event(data)
                    elif event == "result":
                        final = data
        _summary(final)
        raise typer.Exit(0 if final.get("deploy_success") else 1)

    from app.graph import stream_deploy
    final = {}
    for node, delta in stream_deploy(req):
        for ev in delta.get("events", []) or []:
            print_event(ev)
        if node == "finalize":
            final = delta
    _summary(final)
    raise typer.Exit(0 if final.get("deploy_success") else 1)


@app.command()
def fingerprint(instance: str = typer.Option(...)):
    from app.nodes.fingerprint import fingerprint_instance
    fp, raw = fingerprint_instance(instance)
    t = Table(title=f"fingerprint {instance} ({raw['duration_s']}s via SSM)")
    t.add_column("field"); t.add_column("value")
    for k, v in fp.to_dict().items():
        if k != "raw":
            t.add_row(k, str(v))
    console.print(t)


@fixes_app.command("list")
def fixes_list():
    from app.nodes import knowledge
    items = knowledge.list_fixes()
    t = Table(title=f"deployment_fixes ({len(items)})")
    for c in ("error_signature", "error_type", "package", "os", "python_version", "source", "success_count", "fix_command"):
        t.add_column(c)
    for i in items:
        t.add_row(*(str(i.get(c, ""))[:70] for c in ("error_signature", "error_type", "package", "os", "python_version", "source", "success_count", "fix_command")))
    console.print(t)


@fixes_app.command("delete")
def fixes_delete(signature: str):
    from app.nodes import knowledge
    knowledge.delete_fix(signature)
    console.print(f"deleted {signature}")


@fixes_app.command("clear")
def fixes_clear(yes: bool = typer.Option(False, "--yes")):
    from app.nodes import knowledge
    items = knowledge.list_fixes(500)
    if not yes and not typer.confirm(f"delete {len(items)} fixes?"):
        raise typer.Exit(1)
    for i in items:
        knowledge.delete_fix(i["error_signature"])
    console.print(f"deleted {len(items)} fixes")


@app.command()
def attempts():
    from app.nodes.store import list_attempts
    for a in list_attempts():
        console.print(f"{a['last_modified']}  {a['size']:>8}  {a['key']}")


if __name__ == "__main__":
    app()
