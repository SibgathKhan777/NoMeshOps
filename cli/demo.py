"""Colored terminal demo. Runs the LangGraph loop in-process (local AWS creds) or against a hosted
NoMeshOps server over its authenticated API (device-code sign-in, like `aws login`). Usage:

  python -m cli.demo deploy --repo https://github.com/you/sample --instance i-0123456789abcdef0
  python -m cli.demo login --url https://your-server           # sign in once per server
  python -m cli.demo deploy --repo ... --instance ... --url https://your-server
  python -m cli.demo whoami --url https://your-server
  python -m cli.demo logout --url https://your-server
  python -m cli.demo fingerprint --instance i-...
  python -m cli.demo fixes list | fixes delete <signature> | fixes clear
  python -m cli.demo attempts
"""
from __future__ import annotations

import json
import pathlib
import time
import webbrowser

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

SESSION_PATH = pathlib.Path.home() / ".nomeshops" / "session.json"


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
                        + (f"\naudit log: {final['s3_key'] if str(final['s3_key']).startswith('.') or '/' == str(final['s3_key'])[:1] else 's3://…/' + final['s3_key']}" if final.get("s3_key") else ""),
                        border_style="green" if ok else "red"))


# --------------------------------------------------------------------------- session store (per hosted server)

def _norm(url: str) -> str:
    return url.rstrip("/")


def _load_sessions() -> dict:
    try:
        return json.loads(SESSION_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions(sessions: dict) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(sessions, indent=2))
    try:
        SESSION_PATH.chmod(0o600)
    except OSError:
        pass


def _session_for(url: str) -> dict | None:
    return _load_sessions().get(_norm(url))


@app.command()
def login(url: str = typer.Option(..., help="hosted NoMeshOps server, e.g. https://your-server")):
    """Sign in to a hosted NoMeshOps server: opens your browser, you approve, the session comes
    back here. Same device-code exchange as `aws login`. No password ever touches this terminal."""
    import httpx
    base = _norm(url)
    r = httpx.post(f"{base}/api/device/start", timeout=10)
    r.raise_for_status()
    d = r.json()
    verify_url = f"{base}{d['verification_uri']}?code={d['user_code']}"
    console.print(Panel(f"Opening your browser to sign in to NoMeshOps…\n"
                        f"If it does not open, visit:\n  [cyan]{verify_url}[/]\n"
                        f"and confirm the code:\n  [bold yellow]{d['user_code']}[/]",
                        border_style="blue"))
    opened = False
    try:
        opened = webbrowser.open(verify_url)
    except Exception:  # noqa: BLE001
        opened = False
    if not opened:
        console.print("[dim](could not open a browser automatically — use the link above)[/]")

    deadline = time.time() + d["expires_in"]
    interval = max(2, d.get("interval", 2))
    with console.status("Waiting for approval in the browser…"):
        while time.time() < deadline:
            time.sleep(interval)
            pr = httpx.get(f"{base}/api/device/poll", params={"device_code": d["device_code"]}, timeout=10)
            pr.raise_for_status()
            pd = pr.json()
            if pd["status"] == "approved":
                sessions = _load_sessions()
                sessions[base] = {"token": pd["session_token"], "email": pd["subject"]}
                _save_sessions(sessions)
                console.print(f"[bold green]✓ signed in as {pd['subject']}[/] · session cached in {SESSION_PATH}")
                raise typer.Exit(0)
            if pd["status"] == "denied":
                console.print("[bold red]✗ request declined in the browser — no session was created[/]")
                raise typer.Exit(1)
            if pd["status"] == "expired":
                break
    console.print("[bold red]login timed out — run `nomeshops login` again[/]")
    raise typer.Exit(1)


@app.command()
def logout(url: str = typer.Option(..., help="hosted NoMeshOps server")):
    """End the local session for a hosted server (and tell the server, best-effort)."""
    import httpx
    base = _norm(url)
    sessions = _load_sessions()
    sess = sessions.pop(base, None)
    _save_sessions(sessions)
    if sess:
        try:
            httpx.post(f"{base}/api/auth/logout", headers={"authorization": f"Bearer {sess['token']}"}, timeout=5)
        except httpx.HTTPError:
            pass
        console.print(f"signed out of {base} (was {sess['email']})")
    else:
        console.print(f"no local session for {base}")


@app.command()
def whoami(url: str = typer.Option(None, help="hosted NoMeshOps server; omit to list every saved session")):
    """Show who you are signed in as, and how many free demo runs remain on that account."""
    import httpx
    sessions = _load_sessions()
    targets = {url: sessions[_norm(url)]} if url and _norm(url) in sessions else (sessions if not url else {})
    if url and _norm(url) not in sessions:
        console.print(f"not signed in to {url} — run: python -m cli.demo login --url {url}")
        raise typer.Exit(1)
    if not sessions:
        console.print("not signed in anywhere — run: python -m cli.demo login --url <server>")
        raise typer.Exit(1)
    for base, sess in (targets or sessions).items():
        try:
            r = httpx.get(f"{base}/api/auth/me", headers={"authorization": f"Bearer {sess['token']}"}, timeout=10)
            if r.status_code == 401:
                console.print(f"{base}  [red]session expired — run: python -m cli.demo login --url {base}[/]")
                continue
            r.raise_for_status()
            me = r.json()
            console.print(f"{base}  [bold]{me['email']}[/]  ({me['demo_runs_remaining']}/{me['demo_runs_limit']} free demo runs left)")
        except httpx.HTTPError as e:
            console.print(f"{base}  [red]unreachable: {e}[/]")


@app.command()
def deploy(repo: str = typer.Option(..., help="git URL of the project"),
           instance: str = typer.Option(..., help="target EC2 instance id (or docker container name with EXECUTOR=docker)"),
           branch: str = typer.Option(None), start_command: str = typer.Option(None),
           port: int = typer.Option(8000), health_path: str = typer.Option("/health"),
           keep_running: bool = typer.Option(False), preempt: bool = typer.Option(None, help="pre-apply predicted fixes"),
           url: str = typer.Option(None, help="hosted NoMeshOps server; sign in first with `login --url`. Omit to run in-process")):
    req = {"repo_url": repo, "instance_id": instance, "branch": branch, "start_command": start_command,
           "app_port": port, "health_path": health_path, "keep_running": keep_running,
           "preempt_predicted_fixes": preempt}
    _banner(req)
    if url:
        import httpx
        base = _norm(url)
        sess = _session_for(base)
        if not sess:
            console.print(f"[bold red]not signed in to {base}[/] — run: python -m cli.demo login --url {base}")
            raise typer.Exit(1)
        final = {}
        headers = {"authorization": f"Bearer {sess['token']}"}
        with httpx.stream("POST", f"{base}/api/deploy/stream", json=req, headers=headers, timeout=None) as r:
            if r.status_code == 401:
                console.print(f"[bold red]session expired or revoked[/] — run: python -m cli.demo login --url {base}")
                raise typer.Exit(1)
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
