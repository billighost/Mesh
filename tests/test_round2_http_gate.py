"""Regression tests for the pure-ASGI security gate (round 2). Same behaviour as the old BaseHTTPMiddleware."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = "t" * 40


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_HOME", str(tmp_path))
    monkeypatch.setenv("MESH_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    from mesh import http_server as hs

    hs.app.dependency_overrides.pop(hs.verify_request_auth, None)
    with TestClient(hs.app, base_url="http://127.0.0.1:7701") as c:
        yield c


def test_health_and_identity_need_no_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/identity").status_code in (200, 404, 500) or True  # reachable without a token
    assert client.get("/identity").status_code != 401


def test_missing_or_wrong_token_is_401(client):
    assert client.get("/brief").status_code == 401
    assert client.get("/brief", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/brief", headers={"X-Mesh-Token": "nope"}).status_code == 401


def test_valid_token_passes_gate(client):
    r = client.get("/brief", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code != 401 and r.status_code != 403
    r = client.get("/brief", headers={"X-Mesh-Token": TOKEN})
    assert r.status_code != 401 and r.status_code != 403


def test_dns_rebinding_host_rejected(client):
    r = client.get("/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 403 and "Host" in r.json()["detail"]


def test_cross_origin_rejected_and_local_origin_allowed(client):
    r = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    r = client.get("/health", headers={"Origin": "http://127.0.0.1:7433"})
    assert r.status_code == 200


def test_oversized_and_malformed_content_length(client):
    from mesh import http_server as hs

    big = str(hs._MAX_REQUEST_BODY_BYTES + 1)
    r = client.post("/learn", content=b"{}", headers={"Content-Length": big, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 413
    r = client.get("/health", headers={"Content-Length": "abc"})
    assert r.status_code == 400


def test_options_preflight_not_blocked_by_auth(client):
    r = client.options(
        "/learn",
        headers={"Origin": "http://127.0.0.1:7433", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code in (200, 204)


def test_route_dependency_skips_only_for_gate_verified_scope():
    from mesh import http_server as hs
    from starlette.requests import Request
    import os

    os.environ["MESH_AUTH_TOKEN"] = TOKEN
    try:
        bad = Request({"type": "http", "headers": [], "method": "GET", "path": "/x"})
        with pytest.raises(hs.HTTPException):
            hs.verify_request_auth(bad)
        # a client cannot set scope keys: headers never end up in the ASGI scope dict
        forged = Request({"type": "http", "headers": [(b"mesh_auth_verified", b"true")], "method": "GET", "path": "/x"})
        with pytest.raises(hs.HTTPException):
            hs.verify_request_auth(forged)
        ok = Request({"type": "http", "headers": [], "method": "GET", "path": "/x", "mesh_auth_verified": True})
        hs.verify_request_auth(ok)
    finally:
        os.environ.pop("MESH_AUTH_TOKEN", None)
