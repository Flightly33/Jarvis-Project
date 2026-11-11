from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Config
from jarvis.core import JarvisCore
from jarvis.server.app import COOKIE, create_app

TOKEN = "test-token-123"


@pytest.fixture
def core(paths, home: Path, workspace: Path) -> JarvisCore:
    cfg = Config()
    cfg.brain.backend = "demo"
    cfg.safety.approved_dirs = [str(workspace)]
    cfg.safety.approval_timeout_seconds = 5
    cfg.voice.enabled = False
    return JarvisCore(paths, cfg)


@pytest.fixture
def client(core: JarvisCore):
    app = create_app(core, None, token=TOKEN, port=8765, extra_hosts=("testserver",))
    with TestClient(app) as c:
        c.cookies.set(COOKIE, TOKEN)
        yield c


def until(ws, kind: str, limit: int = 400) -> list[dict]:
    """Read events until one of the given type arrives; returns everything read."""
    seen = []
    for _ in range(limit):
        event = ws.receive_json()
        seen.append(event)
        if event["type"] == kind:
            return seen
    raise AssertionError(f"no {kind} event; saw {[e['type'] for e in seen]}")


def ws_headers() -> dict[str, str]:
    return {"cookie": f"{COOKIE}={TOKEN}"}


# ------------------------------------------------------------------ access


def test_requests_without_the_session_secret_are_rejected(core):
    app = create_app(core, None, token=TOKEN, port=8765, extra_hosts=("testserver",))
    with TestClient(app) as anonymous:
        assert anonymous.get("/").status_code == 401
        assert anonymous.get("/api/state").status_code == 401
        assert anonymous.post("/api/message", json={"text": "run rm -rf x"}).status_code == 401
        assert anonymous.post("/api/stop").status_code == 401
        assert anonymous.get("/static/app.js").status_code == 401
        assert anonymous.get("/?token=wrong", follow_redirects=False).status_code == 403


def test_the_launch_link_trades_the_secret_for_a_cookie(core):
    app = create_app(core, None, token=TOKEN, port=8765, extra_hosts=("testserver",))
    with TestClient(app) as fresh:
        response = fresh.get(f"/?token={TOKEN}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
        page = fresh.get("/")
        assert page.status_code == 200 and "J.A.R.V.I.S." in page.text


def test_other_hostnames_are_refused(client):
    # a web page that points its own domain name at 127.0.0.1 (DNS rebinding) arrives with that name as Host
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 403


def test_cross_site_requests_are_refused(client):
    response = client.post("/api/stop", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert client.post("/api/stop", headers={"origin": "http://testserver"}).status_code == 200


def test_websocket_requires_the_secret_and_a_local_origin(client):
    with pytest.raises(Exception):  # noqa: B017, PT011 - the handshake is refused
        with client.websocket_connect("/ws", headers={"cookie": f"{COOKIE}=nope"}):
            pass
    with pytest.raises(Exception):  # noqa: B017, PT011
        with client.websocket_connect("/ws", headers=ws_headers() | {"origin": "https://evil.example"}):
            pass
    with client.websocket_connect("/ws", headers=ws_headers()) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_responses_carry_protective_headers(client):
    headers = client.get("/").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-store"


# ------------------------------------------------------------ conversation


def test_a_typed_request_runs_tools_and_streams_the_reply(client, workspace):
    with client.websocket_connect("/ws", headers=ws_headers()) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["backend"]["backend"] == "demo" and hello["history"] == []
        assert client.post("/api/message", json={"text": "create file notes.txt saying hello there"}).status_code == 202
        events = until(ws, "assistant_message")
    kinds = [e["type"] for e in events]
    assert kinds.index("user_message") < kinds.index("tool_start") < kinds.index("tool_end") < kinds.index("assistant_message")
    assert "assistant_delta" in kinds
    assert (workspace / "notes.txt").read_text() == "hello there\n"
    final = events[-1]
    assert "created notes.txt" in final["text"] and not final["error"]
    assert [m["role"] for m in client.get("/api/state").json()["history"]] == ["user", "assistant"]


def test_an_approval_appears_and_can_be_answered_from_the_interface(client, workspace):
    (workspace / "old.txt").write_text("x")
    with client.websocket_connect("/ws", headers=ws_headers()) as ws:
        ws.receive_json()
        client.post("/api/message", json={"text": "delete old.txt"})
        request = until(ws, "approval_request")[-1]["request"]
        assert request["risk"] == "changes data" and "old.txt" in request["title"]
        assert client.get("/api/state").json()["approvals"][0]["id"] == request["id"]
        assert client.post(f"/api/approvals/{request['id']}", json={"approved": False}).status_code == 200
        final = until(ws, "assistant_message")[-1]
        assert "declined" in final["text"]
        assert client.post(f"/api/approvals/{request['id']}", json={"approved": True}).status_code == 404
    assert (workspace / "old.txt").exists()


def test_stop_declines_pending_approvals_and_ends_the_request(client, workspace):
    (workspace / "old.txt").write_text("x")
    with client.websocket_connect("/ws", headers=ws_headers()) as ws:
        ws.receive_json()
        client.post("/api/message", json={"text": "delete old.txt"})
        until(ws, "approval_request")
        assert client.post("/api/stop").json() == {"stopped": True}
        kinds = [e["type"] for e in until(ws, "assistant_message")]
        assert "estop" in kinds and "approval_resolved" in kinds
    assert (workspace / "old.txt").exists() and client.get("/api/state").json()["approvals"] == []


def test_bad_request_bodies_are_rejected(client):
    assert client.post("/api/message", json={"text": ""}).status_code == 422
    assert client.post("/api/message", json={"text": "hi", "extra": 1}).status_code == 422
    assert client.post("/api/approvals/x", json={"approved": True, "scope": "forever"}).status_code == 422
    assert client.post("/api/voice/listen").status_code == 409      # voice is not running in this test


# ---------------------------------------------------------------- settings


def test_settings_can_be_changed_and_are_saved(client, core):
    response = client.patch("/api/settings", json={"autonomy": "cautious", "name": "Sam", "wake_word": "Friday"})
    assert response.status_code == 200 and response.json()["autonomy"] == "cautious"
    assert core.services.policy.autonomy == "cautious" and core.config.voice.wake_word == "friday"
    assert 'autonomy = "cautious"' in core.paths.config_file.read_text()
    assert client.patch("/api/settings", json={"wake_word": "j4rv1s!"}).status_code == 422
    assert client.patch("/api/settings", json={"autonomy": "reckless"}).status_code == 422
    assert client.patch("/api/settings", json={"approved_dirs": ["/"]}).status_code == 422     # not editable this way


def test_folders_can_be_approved_and_removed_but_never_system_ones(client, core, home, workspace):
    project = home / "Projects"
    project.mkdir()
    added = client.post("/api/folders", json={"path": str(project), "mode": "read"}).json()
    assert str(project) in added["read_only_dirs"]
    assert client.post("/api/folders", json={"path": "/", "mode": "write"}).status_code == 422
    assert client.post("/api/folders", json={"path": str(home / "missing"), "mode": "write"}).status_code == 422
    assert client.post("/api/folders", json={"path": str(core.paths.config_dir), "mode": "read"}).status_code == 422
    removed = client.request("DELETE", "/api/folders", json={"path": str(project)}).json()
    assert removed["read_only_dirs"] == []
    assert client.request("DELETE", "/api/folders", json={"path": str(workspace)}).status_code == 422   # the last one stays


def test_reminders_fire_through_the_scheduler(core):
    seen = []
    q = core.bus.subscribe()
    core.store.add_reminder("stretch", 1000.0)
    core.store.add_reminder("later", 5000.0)
    assert [r.text for r in core.scheduler.tick(now=1001.0)] == ["stretch"]
    assert core.scheduler.tick(now=1002.0) == []                       # never announced twice
    while not q.empty():
        seen.append(q.get_nowait())
    assert [e["text"] for e in seen if e["type"] == "reminder"] == ["stretch"]
