"""Offline tests for the hosted account system: signup/login, password hashing, and the
per-account demo-run quota enforced by /api/demo/deploy."""
import shutil

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    import dataclasses
    import app.web as web
    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, local_data_dir=str(tmp_path)))
    web._account_sessions.clear()
    from app.main import app as fastapi_app
    return TestClient(fastapi_app)


def test_signup_then_me(client):
    r = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "longenough"})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert r.json()["demo_runs_remaining"] == 3
    me = client.get("/api/auth/me", headers={"authorization": f"Bearer {tok}"})
    assert me.status_code == 200 and me.json()["email"] == "a@b.com"


def test_duplicate_signup_rejected(client):
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "longenough"})
    r = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "different1"})
    assert r.status_code == 409


def test_wrong_password_rejected(client):
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "correcthorse"})
    r = client.post("/api/auth/login", json={"email": "a@b.com", "password": "wrongwrong"})
    assert r.status_code == 401


def test_short_password_rejected(client):
    r = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "short"})
    assert r.status_code == 400


def test_demo_requires_auth(client):
    r = client.get("/api/demo/deploy?scenario=clean&target=aws-ubuntu")
    assert r.status_code == 401


def test_demo_quota_enforced(client, monkeypatch):
    tok = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "longenough"}).json()["token"]

    def fake_stream(req):
        yield "finalize", {"events": [], "deploy_success": True, "final_status": "success"}
    monkeypatch.setattr("app.web.stream_deploy", fake_stream)

    for i in range(3):
        r = client.get(f"/api/demo/deploy?scenario=clean&target=aws-ubuntu&token={tok}")
        assert r.status_code == 200, f"run {i+1} should be allowed"
    r4 = client.get(f"/api/demo/deploy?scenario=clean&target=aws-ubuntu&token={tok}")
    assert r4.status_code == 403
    me = client.get("/api/auth/me", headers={"authorization": f"Bearer {tok}"})
    assert me.json() == {"email": "a@b.com", "demo_runs_used": 3, "demo_runs_limit": 3, "demo_runs_remaining": 0}


def test_password_hash_uses_unique_salt(client):
    import app.web as web
    h1 = web._hash_password("samepassword")
    h2 = web._hash_password("samepassword")
    assert h1 != h2, "each hash must use a fresh salt"
    assert web._verify_password("samepassword", h1)
    assert not web._verify_password("wrongpassword", h1)


def test_demo_deploy_rejects_disallowed_host(client, monkeypatch):
    tok = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "longenough"}).json()["token"]
    r = client.get(f"/api/demo/deploy?target=aws-ubuntu&repo=https://evil.example.com/x.git&token={tok}")
    assert r.status_code == 400
    assert "github.com" in r.json()["detail"]


def test_demo_deploy_accepts_arbitrary_allowed_repo(client, monkeypatch):
    tok = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "longenough"}).json()["token"]
    calls = {}

    def fake_stream(req):
        calls["req"] = req
        yield "finalize", {"events": [], "deploy_success": True, "final_status": "success"}
    monkeypatch.setattr("app.web.stream_deploy", fake_stream)

    r = client.get(
        "/api/demo/deploy?target=aws-ubuntu&repo=https://github.com/someone/their-own-project.git"
        f"&branch=main&health_path=/healthz&start_command=python+run.py&token={tok}"
    )
    assert r.status_code == 200
    req = calls["req"]
    assert req["repo_url"] == "https://github.com/someone/their-own-project.git"
    assert req["branch"] == "main"
    assert req["health_path"] == "/healthz"
    assert req["start_command"] == "python run.py"


def test_demo_deploy_default_start_command_is_none_for_autodetect(client, monkeypatch):
    tok = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "longenough"}).json()["token"]
    calls = {}

    def fake_stream(req):
        calls["req"] = req
        yield "finalize", {"events": [], "deploy_success": True, "final_status": "success"}
    monkeypatch.setattr("app.web.stream_deploy", fake_stream)

    r = client.get(f"/api/demo/deploy?target=aws-ubuntu&repo=https://github.com/x/y.git&token={tok}")
    assert r.status_code == 200
    assert calls["req"]["start_command"] is None, "no start_command given -> must be None so the agent auto-detects, not a hardcoded uvicorn guess"


def test_demo_examples_and_targets_endpoints(client):
    ex = client.get("/api/demo/examples").json()["examples"]
    assert {"typo", "psycopg2", "clean", "crash"} == {e["id"] for e in ex}
    targets = client.get("/api/demo/targets").json()["targets"]
    assert len(targets) == 6
    assert {"AWS EC2", "Google Cloud", "Azure / DigitalOcean", "Oracle Cloud / on-prem", "Fly.io / lightweight VPS"} == {t["cloud"] for t in targets}
