"""
Mesh Dashboard — v0.4
Redesigned: full memory management (add/edit/delete), dark UI, audit, digest, patterns.

Start: mesh-dashboard
       mesh-dashboard --namespace my-project --port 7433
"""

import json
import webbrowser
import threading
import time
from typing import Optional, List

import click
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .memory import Mesh
from .namespaces import list_namespaces, delete_namespace, namespace_stats
from .io import export_namespace

api = FastAPI(title="Mesh Dashboard", version="0.4.0")

_mesh: Optional[Mesh] = None


def get_mesh() -> Mesh:
    if _mesh is None:
        raise RuntimeError("Mesh not initialised")
    return _mesh


# ── Request models ──────────────────────────────────────────────────────────

class CreateMemoryRequest(BaseModel):
    content: str
    memory_type: str = "fact"
    confidence: float = 1.0
    tags: List[str] = []
    local_only: bool = False
    ttl_days: Optional[float] = None


class UpdateMemoryRequest(BaseModel):
    content: str
    memory_type: str = "fact"
    confidence: float = 1.0
    tags: List[str] = []
    local_only: bool = False
    ttl_days: Optional[float] = None


# ── REST API endpoints ──────────────────────────────────────────────────────

@api.get("/api/stats")
def get_stats():
    mesh = get_mesh()
    patterns = mesh.patterns()
    contradictions = mesh.contradictions()
    return {
        "namespace": mesh.namespace,
        "total_memories": mesh.count(),
        "open_contradictions": len(contradictions),
        "memory_gaps": len(patterns["memory_gaps"]),
        "stale_memories": len(patterns["stale_memories"]),
        "unused_memories": len(patterns["unused_memories"]),
    }


@api.get("/api/memories")
def get_memories(limit: int = 200):
    return get_mesh().inspect(limit=limit)


@api.post("/api/memories")
def create_memory(req: CreateMemoryRequest):
    """Add a new memory from the dashboard."""
    try:
        mesh = get_mesh()
        result = mesh.learn(
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            local_only=req.local_only,
            ttl_days=req.ttl_days if req.ttl_days and req.ttl_days > 0 else None,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.put("/api/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateMemoryRequest):
    """Update a memory: deletes old embedding and re-learns with new content."""
    try:
        mesh = get_mesh()
        mesh.forget(memory_id)
        result = mesh.learn(
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            local_only=req.local_only,
            ttl_days=req.ttl_days if req.ttl_days and req.ttl_days > 0 else None,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    get_mesh().forget(memory_id)
    return {"status": "deleted", "memory_id": memory_id}


@api.get("/api/patterns")
def get_patterns():
    return get_mesh().patterns()


@api.get("/api/contradictions")
def get_contradictions():
    return get_mesh().contradictions()


@api.post("/api/contradictions/{contradiction_id}/resolve")
def resolve_contradiction(contradiction_id: str):
    get_mesh().resolve_contradiction(contradiction_id)
    return {"status": "resolved"}


@api.get("/api/search")
def search(q: str, n: int = 20):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    return get_mesh().recall(q, n=n)


@api.get("/api/namespaces")
def get_namespaces():
    return list_namespaces()


@api.get("/api/namespaces/{namespace}/stats")
def get_namespace_stats(namespace: str):
    try:
        return namespace_stats(namespace)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.delete("/api/namespaces/{namespace}")
def api_delete_namespace(namespace: str):
    count = delete_namespace(namespace)
    return {"deleted": True, "memories_removed": count}


@api.get("/api/export/{namespace}")
def api_export_namespace(namespace: str, include_embeddings: bool = False):
    return export_namespace(namespace=namespace, include_embeddings=include_embeddings)


@api.get("/api/audit")
async def get_audit(namespace: str = "shared", limit: int = 100, action: str = "all"):
    from .audit import get_audit_log
    return get_audit_log(namespace=namespace, limit=limit, action_filter=action)


@api.get("/api/digest")
async def get_digest(namespace: str = "shared", hours: int = 24):
    from .digest import generate_digest
    return generate_digest(namespace=namespace, hours=hours)


# ── Dashboard HTML ──────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mesh Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #070b14;
  --bg-2:      #0c1220;
  --bg-3:      #111827;
  --bg-4:      #1a2236;
  --border:    rgba(148,163,184,0.07);
  --border-2:  rgba(148,163,184,0.13);
  --border-3:  rgba(148,163,184,0.22);
  --text:      #e2e8f4;
  --text-2:    #94a3b8;
  --text-3:    #475569;
  --mint:      #10d9a4;
  --mint-dim:  rgba(16,217,164,0.12);
  --mint-glow: rgba(16,217,164,0.25);
  --purple:    #a78bfa;
  --purple-dim:rgba(167,139,250,0.12);
  --amber:     #fbbf24;
  --amber-dim: rgba(251,191,36,0.12);
  --red:       #f87171;
  --red-dim:   rgba(248,113,113,0.12);
  --blue:      #60a5fa;
  --blue-dim:  rgba(96,165,250,0.12);
  --green:     #34d399;
  --green-dim: rgba(52,211,153,0.12);

  --type-fact:        #60a5fa;
  --type-preference:  #a78bfa;
  --type-context:     #10d9a4;
  --type-result:      #34d399;
  --type-instruction: #fbbf24;

  --radius: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --font: 'Outfit', sans-serif;
  --mono: 'JetBrains Mono', monospace;
}

html, body { height: 100%; }

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
  overflow-x: hidden;
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--bg-4); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--border-3); }

/* ── Layout ── */
.layout { display: flex; flex-direction: column; height: 100vh; }

/* ── Navbar ── */
nav {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 24px;
  height: 58px;
  background: var(--bg-2);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  position: relative;
  z-index: 100;
}

.nav-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 17px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text);
}

.nav-logo-icon {
  width: 28px; height: 28px;
  background: linear-gradient(135deg, var(--mint), #0ea5e9);
  border-radius: 7px;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px;
}

.nav-ns {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 10px 4px 8px;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: 20px;
  font-size: 12px;
  color: var(--text-2);
}
.nav-ns-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--mint);
  box-shadow: 0 0 6px var(--mint-glow);
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.nav-search {
  flex: 1;
  max-width: 380px;
  margin-left: 8px;
  position: relative;
}
.nav-search input {
  width: 100%;
  padding: 7px 12px 7px 34px;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  color: var(--text);
  font-family: var(--font);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s;
}
.nav-search input:focus { border-color: var(--mint); }
.nav-search input::placeholder { color: var(--text-3); }
.nav-search-icon {
  position: absolute;
  left: 10px; top: 50%;
  transform: translateY(-50%);
  color: var(--text-3);
  pointer-events: none;
  font-size: 13px;
}

.nav-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }

.btn-add {
  display: flex; align-items: center; gap: 6px;
  padding: 7px 16px;
  background: var(--mint);
  color: #030a06;
  font-family: var(--font);
  font-size: 13px;
  font-weight: 600;
  border: none;
  border-radius: var(--radius);
  cursor: pointer;
  transition: all 0.15s;
  letter-spacing: -0.01em;
}
.btn-add:hover { background: #0ec898; transform: translateY(-1px); box-shadow: 0 4px 12px var(--mint-glow); }

.btn-icon {
  padding: 7px;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  cursor: pointer;
  color: var(--text-2);
  font-size: 14px;
  transition: all 0.15s;
  display: flex; align-items: center;
}
.btn-icon:hover { background: var(--bg-4); color: var(--text); border-color: var(--border-3); }

/* ── Stats Bar ── */
.stats-bar {
  display: flex;
  gap: 1px;
  padding: 0 24px;
  background: var(--bg-2);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 20px;
  flex: 1;
  position: relative;
}
.stat-item + .stat-item::before {
  content: '';
  position: absolute;
  left: 0; top: 20%; height: 60%;
  width: 1px;
  background: var(--border);
}

.stat-num {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1;
}
.stat-num.mint  { color: var(--mint); }
.stat-num.amber { color: var(--amber); }
.stat-num.red   { color: var(--red); }
.stat-num.purple{ color: var(--purple); }
.stat-num.blue  { color: var(--blue); }
.stat-num.green { color: var(--green); }

.stat-label { font-size: 11px; color: var(--text-3); font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; }

/* ── Tabs ── */
.tabs-bar {
  display: flex;
  gap: 2px;
  padding: 10px 24px 0;
  background: var(--bg-2);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.tab {
  padding: 8px 16px;
  font-size: 13px;
  font-family: var(--font);
  font-weight: 500;
  color: var(--text-3);
  border: none;
  background: transparent;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: all 0.15s;
  display: flex; align-items: center; gap: 6px;
  border-radius: var(--radius) var(--radius) 0 0;
}
.tab:hover { color: var(--text-2); background: var(--bg-3); }
.tab.active { color: var(--mint); border-bottom-color: var(--mint); }
.tab-badge {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 5px;
  background: var(--bg-4);
  border-radius: 9px;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-2);
}
.tab.active .tab-badge { background: var(--mint-dim); color: var(--mint); }

/* ── Content Area ── */
.content { flex: 1; overflow-y: auto; padding: 20px 24px; }

.panel { display: none; }
.panel.active { display: block; }

/* ── Memory Grid / List ── */
.memories-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
  align-items: center;
  flex-wrap: wrap;
}

.filter-group { display: flex; gap: 4px; }
.filter-btn {
  padding: 5px 12px;
  font-size: 12px;
  font-family: var(--font);
  font-weight: 500;
  border: 1px solid var(--border-2);
  border-radius: 20px;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.12s;
}
.filter-btn:hover { color: var(--text-2); border-color: var(--border-3); background: var(--bg-3); }
.filter-btn.active { color: var(--mint); border-color: var(--mint); background: var(--mint-dim); }

.sort-select {
  padding: 5px 10px;
  font-size: 12px;
  font-family: var(--font);
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  color: var(--text-2);
  outline: none;
  cursor: pointer;
  margin-left: auto;
}

.memory-count {
  font-size: 12px;
  color: var(--text-3);
  margin-left: 4px;
}

/* ── Memory Card ── */
.memory-list { display: flex; flex-direction: column; gap: 8px; }

.memory-card {
  display: flex;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  transition: border-color 0.15s, box-shadow 0.15s;
  position: relative;
}
.memory-card:hover { border-color: var(--border-2); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
.memory-card.stale { border-color: rgba(251,191,36,0.25); }
.memory-card.stale:hover { border-color: var(--amber); }

.memory-stripe {
  width: 3px;
  flex-shrink: 0;
  background: var(--type-fact);
}
.memory-stripe.fact        { background: var(--type-fact); }
.memory-stripe.preference  { background: var(--type-preference); }
.memory-stripe.context     { background: var(--type-context); }
.memory-stripe.result      { background: var(--type-result); }
.memory-stripe.instruction { background: var(--type-instruction); }

.memory-body {
  flex: 1;
  padding: 13px 14px;
  min-width: 0;
}

.memory-content {
  font-family: var(--mono);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text);
  margin-bottom: 10px;
  word-break: break-word;
}

.memory-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.badge {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}
.badge-fact        { background: var(--blue-dim);   color: var(--blue); }
.badge-preference  { background: var(--purple-dim); color: var(--purple); }
.badge-context     { background: var(--mint-dim);   color: var(--mint); }
.badge-result      { background: var(--green-dim);  color: var(--green); }
.badge-instruction { background: var(--amber-dim);  color: var(--amber); }
.badge-stale       { background: rgba(251,191,36,0.18); color: var(--amber); border: 1px solid rgba(251,191,36,0.3); }
.badge-private     { background: var(--red-dim); color: var(--red); }

.meta-sep { width: 3px; height: 3px; border-radius: 50%; background: var(--text-3); flex-shrink: 0; }

.meta-text { font-size: 11px; color: var(--text-3); }

.conf-bar-wrap {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--text-3);
}
.conf-bar {
  width: 48px; height: 3px;
  background: var(--bg-4);
  border-radius: 2px;
  overflow: hidden;
}
.conf-bar-fill {
  height: 100%;
  background: var(--mint);
  border-radius: 2px;
  transition: width 0.3s;
}
.conf-bar-fill.amber { background: var(--amber); }
.conf-bar-fill.red   { background: var(--red); }

.tag-chip {
  padding: 1px 7px;
  background: var(--bg-4);
  border: 1px solid var(--border-2);
  border-radius: 8px;
  font-size: 10.5px;
  color: var(--text-3);
}

.memory-actions {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  padding: 12px 12px 12px 8px;
  opacity: 0;
  transition: opacity 0.15s;
}
.memory-card:hover .memory-actions { opacity: 1; }

.action-btn {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  background: var(--bg-3);
  cursor: pointer;
  font-size: 12px;
  color: var(--text-2);
  transition: all 0.12s;
}
.action-btn:hover { background: var(--bg-4); color: var(--text); border-color: var(--border-3); }
.action-btn.edit:hover { border-color: var(--mint); color: var(--mint); background: var(--mint-dim); }
.action-btn.delete:hover { border-color: var(--red); color: var(--red); background: var(--red-dim); }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(4px);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 24px;
}
.modal-overlay.open { display: flex; }

.modal {
  background: var(--bg-2);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-xl);
  width: 100%;
  max-width: 560px;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 25px 50px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.03);
  animation: modal-in 0.2s ease-out;
}
@keyframes modal-in {
  from { opacity: 0; transform: translateY(-12px) scale(0.98); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}

.modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 20px 24px 0;
}
.modal-title { font-size: 16px; font-weight: 600; letter-spacing: -0.02em; }
.modal-close {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  cursor: pointer;
  color: var(--text-2);
  font-size: 16px;
  transition: all 0.12s;
}
.modal-close:hover { background: var(--bg-4); color: var(--text); }

.modal-body { padding: 20px 24px 24px; display: flex; flex-direction: column; gap: 16px; }

.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; font-weight: 600; color: var(--text-2); letter-spacing: 0.02em; text-transform: uppercase; }

.field textarea, .field input[type="text"], .field input[type="number"], .field select {
  padding: 10px 12px;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: var(--radius);
  color: var(--text);
  font-family: var(--font);
  font-size: 13px;
  outline: none;
  transition: border-color 0.15s;
  width: 100%;
}
.field textarea:focus, .field input:focus, .field select:focus { border-color: var(--mint); }
.field textarea { resize: vertical; min-height: 90px; line-height: 1.6; font-family: var(--mono); font-size: 12.5px; }
.field select option { background: var(--bg-3); }

.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

.field-hint { font-size: 11px; color: var(--text-3); }

/* Slider */
.slider-wrap { display: flex; align-items: center; gap: 10px; }
.conf-slider {
  flex: 1;
  -webkit-appearance: none;
  height: 4px;
  background: var(--bg-4);
  border-radius: 2px;
  outline: none;
}
.conf-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: var(--mint);
  cursor: pointer;
  box-shadow: 0 0 6px var(--mint-glow);
}
.conf-value { font-size: 13px; font-weight: 600; color: var(--mint); min-width: 30px; text-align: right; }

/* Toggle */
.toggle-wrap { display: flex; align-items: center; gap: 10px; }
.toggle-label { font-size: 13px; color: var(--text-2); }
.toggle {
  position: relative; display: inline-block;
  width: 36px; height: 20px;
}
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute; inset: 0;
  background: var(--bg-4);
  border: 1px solid var(--border-2);
  border-radius: 10px;
  cursor: pointer;
  transition: 0.2s;
}
.toggle-slider::before {
  content: '';
  position: absolute;
  height: 14px; width: 14px;
  left: 2px; bottom: 2px;
  background: var(--text-3);
  border-radius: 50%;
  transition: 0.2s;
}
.toggle input:checked + .toggle-slider { background: var(--mint); border-color: var(--mint); }
.toggle input:checked + .toggle-slider::before { transform: translateX(16px); background: #fff; }

.modal-footer {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 0 24px 24px;
}

.btn {
  padding: 9px 20px;
  font-family: var(--font);
  font-size: 13px;
  font-weight: 600;
  border-radius: var(--radius);
  cursor: pointer;
  transition: all 0.15s;
  border: 1px solid transparent;
}
.btn-ghost {
  background: transparent;
  border-color: var(--border-2);
  color: var(--text-2);
}
.btn-ghost:hover { background: var(--bg-3); color: var(--text); border-color: var(--border-3); }
.btn-primary {
  background: var(--mint);
  color: #030a06;
  border-color: var(--mint);
}
.btn-primary:hover { background: #0ec898; box-shadow: 0 4px 12px var(--mint-glow); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

/* ── Conflict cards ── */
.conflict-card {
  background: var(--bg-2);
  border: 1px solid rgba(251,191,36,0.18);
  border-radius: var(--radius-lg);
  padding: 16px;
  margin-bottom: 10px;
}
.conflict-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 12px;
}
.conflict-pair {
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  margin-bottom: 12px;
}
.conflict-side {
  padding: 10px 12px;
  border-radius: var(--radius);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
}
.conflict-old { background: var(--red-dim); border: 1px solid rgba(248,113,113,0.2); color: var(--text-2); }
.conflict-new { background: var(--green-dim); border: 1px solid rgba(52,211,153,0.2); color: var(--text); }
.conflict-meta { font-size: 11px; color: var(--text-3); }

/* ── Pattern cards ── */
.pattern-section { margin-bottom: 28px; }
.section-header {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 12px;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.section-badge {
  padding: 2px 8px;
  background: var(--bg-4);
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
}

.pattern-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 12px 14px;
  margin-bottom: 8px;
  display: flex; align-items: center; gap: 12px;
}
.pattern-query {
  flex: 1;
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--text);
}
.pattern-stats { display: flex; gap: 16px; }
.pattern-stat { text-align: right; }
.pattern-stat-num { font-size: 15px; font-weight: 700; color: var(--text); }
.pattern-stat-label { font-size: 10px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.04em; }

/* ── Gap card ── */
.gap-card {
  background: var(--bg-2);
  border: 1px solid rgba(251,191,36,0.15);
  border-radius: var(--radius-lg);
  padding: 12px 14px;
  margin-bottom: 8px;
  display: flex; align-items: center; gap: 12px;
}
.gap-icon { font-size: 16px; flex-shrink: 0; }
.gap-body { flex: 1; }
.gap-query { font-family: var(--mono); font-size: 12.5px; color: var(--text); margin-bottom: 3px; }
.gap-meta { font-size: 11px; color: var(--text-3); }

/* ── Audit entries ── */
.audit-entry {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 12px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: 8px;
  transition: border-color 0.12s;
}
.audit-entry:hover { border-color: var(--border-2); }
.audit-icon {
  width: 32px; height: 32px;
  border-radius: var(--radius);
  display: flex; align-items: center; justify-content: center;
  font-size: 14px;
  flex-shrink: 0;
}
.audit-icon.read  { background: var(--blue-dim); }
.audit-icon.write { background: var(--mint-dim); }
.audit-body { flex: 1; min-width: 0; }
.audit-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.audit-agent { font-size: 12px; font-weight: 600; color: var(--text); }
.audit-time { font-size: 11px; color: var(--text-3); }
.audit-text { font-family: var(--mono); font-size: 12px; color: var(--text-2); line-height: 1.5; }
.audit-sub { font-size: 11px; color: var(--text-3); margin-top: 3px; font-family: var(--mono); }

/* ── Namespace table ── */
.ns-table { width: 100%; border-collapse: collapse; }
.ns-table th {
  padding: 8px 14px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-3);
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.ns-table td {
  padding: 12px 14px;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.ns-table tr:last-child td { border-bottom: none; }
.ns-table tr:hover td { background: var(--bg-3); }
.ns-name { font-weight: 600; color: var(--text); }
.ns-actions { display: flex; gap: 6px; }

/* ── Digest ── */
.digest-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 24px;
}
.digest-stat {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
}
.digest-stat-num { font-size: 28px; font-weight: 700; letter-spacing: -0.04em; margin-bottom: 2px; }
.digest-stat-label { font-size: 11px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.04em; }

/* ── Empty state ── */
.empty {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center;
  padding: 60px 20px;
  text-align: center;
}
.empty-icon { font-size: 36px; margin-bottom: 12px; opacity: 0.5; }
.empty-title { font-size: 15px; font-weight: 600; color: var(--text-2); margin-bottom: 6px; }
.empty-sub { font-size: 13px; color: var(--text-3); max-width: 300px; }

/* ── Conflict warning in modal ── */
.conflict-warn {
  padding: 10px 14px;
  background: rgba(251,191,36,0.1);
  border: 1px solid rgba(251,191,36,0.25);
  border-radius: var(--radius);
  font-size: 12px;
  color: var(--amber);
  line-height: 1.5;
}

/* ── Audit filter tabs ── */
.sub-tabs { display: flex; gap: 4px; margin-bottom: 14px; }
.sub-tab {
  padding: 5px 12px;
  font-size: 12px;
  font-family: var(--font);
  background: transparent;
  border: 1px solid var(--border-2);
  border-radius: 20px;
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.12s;
}
.sub-tab:hover { color: var(--text-2); }
.sub-tab.active { color: var(--mint); border-color: var(--mint); background: var(--mint-dim); }

/* ── Loading skeleton ── */
.skeleton {
  background: linear-gradient(90deg, var(--bg-3) 25%, var(--bg-4) 50%, var(--bg-3) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: var(--radius);
  height: 72px;
  margin-bottom: 8px;
}
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

/* ── Toasts ── */
#toast-container {
  position: fixed;
  bottom: 20px; right: 20px;
  z-index: 9999;
  display: flex; flex-direction: column; gap: 8px;
}
.toast {
  padding: 10px 16px;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-lg);
  font-size: 13px;
  color: var(--text);
  box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  animation: toast-in 0.2s ease-out;
  display: flex; align-items: center; gap: 8px;
}
.toast.success { border-color: rgba(16,217,164,0.3); }
.toast.error   { border-color: rgba(248,113,113,0.3); }
@keyframes toast-in {
  from { opacity: 0; transform: translateX(16px); }
  to   { opacity: 1; transform: translateX(0); }
}
</style>
</head>
<body>
<div class="layout">

<!-- Navbar -->
<nav>
  <div class="nav-logo">
    <div class="nav-logo-icon">◈</div>
    Mesh
  </div>
  <div class="nav-ns">
    <div class="nav-ns-dot"></div>
    <span id="ns-label">shared</span>
  </div>
  <div class="nav-search">
    <span class="nav-search-icon">⌕</span>
    <input type="text" id="search-input" placeholder="Semantic search memories…" oninput="onSearchInput(this.value)">
  </div>
  <div class="nav-right">
    <button class="btn-icon" onclick="refreshAll()" title="Refresh">↻</button>
    <button class="btn-add" onclick="openModal()">+ Add Memory</button>
  </div>
</nav>

<!-- Stats Bar -->
<div class="stats-bar" id="stats-bar">
  <div class="stat-item">
    <div>
      <div class="stat-num mint" id="stat-total">—</div>
      <div class="stat-label">Memories</div>
    </div>
  </div>
  <div class="stat-item">
    <div>
      <div class="stat-num amber" id="stat-stale">—</div>
      <div class="stat-label">Stale</div>
    </div>
  </div>
  <div class="stat-item">
    <div>
      <div class="stat-num red" id="stat-conflicts">—</div>
      <div class="stat-label">Conflicts</div>
    </div>
  </div>
  <div class="stat-item">
    <div>
      <div class="stat-num purple" id="stat-gaps">—</div>
      <div class="stat-label">Memory Gaps</div>
    </div>
  </div>
  <div class="stat-item">
    <div>
      <div class="stat-num blue" id="stat-unused">—</div>
      <div class="stat-label">Unused</div>
    </div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs-bar">
  <button class="tab active" onclick="showTab('memories', this)">
    Memories <span class="tab-badge" id="tab-badge-memories">0</span>
  </button>
  <button class="tab" onclick="showTab('patterns', this)">Patterns</button>
  <button class="tab" onclick="showTab('conflicts', this)">
    Conflicts <span class="tab-badge" id="tab-badge-conflicts">0</span>
  </button>
  <button class="tab" onclick="showTab('audit', this)">Audit Log</button>
  <button class="tab" onclick="showTab('namespaces', this)">Namespaces</button>
  <button class="tab" onclick="showTab('digest', this)">Digest</button>
</div>

<!-- Content -->
<div class="content">

  <!-- Memories Panel -->
  <div class="panel active" id="panel-memories">
    <div class="memories-toolbar">
      <div class="filter-group" id="type-filters">
        <button class="filter-btn active" onclick="setTypeFilter('all', this)">All</button>
        <button class="filter-btn" onclick="setTypeFilter('fact', this)">Fact</button>
        <button class="filter-btn" onclick="setTypeFilter('preference', this)">Preference</button>
        <button class="filter-btn" onclick="setTypeFilter('context', this)">Context</button>
        <button class="filter-btn" onclick="setTypeFilter('result', this)">Result</button>
        <button class="filter-btn" onclick="setTypeFilter('instruction', this)">Instruction</button>
      </div>
      <select class="sort-select" onchange="setSortOrder(this.value)">
        <option value="accessed">Last accessed</option>
        <option value="created">Date created</option>
        <option value="confidence">Confidence</option>
        <option value="content">Alphabetical</option>
      </select>
      <span class="memory-count" id="memory-count"></span>
    </div>
    <div class="memory-list" id="memory-list">
      <div class="skeleton"></div>
      <div class="skeleton"></div>
      <div class="skeleton" style="height:56px"></div>
    </div>
  </div>

  <!-- Patterns Panel -->
  <div class="panel" id="panel-patterns">
    <div class="pattern-section">
      <div class="section-header">
        <div class="section-title">Top Topics</div>
        <div class="section-badge" id="top-topics-count">0</div>
      </div>
      <div id="top-topics-list"></div>
    </div>
    <div class="pattern-section">
      <div class="section-header">
        <div class="section-title">Memory Gaps</div>
        <div class="section-badge" id="gaps-count">0</div>
        <span style="font-size:11px;color:var(--text-3)">— frequently asked, poorly answered</span>
      </div>
      <div id="gaps-list"></div>
    </div>
    <div class="pattern-section">
      <div class="section-header">
        <div class="section-title">Unused Memories</div>
        <div class="section-badge" id="unused-count">0</div>
        <span style="font-size:11px;color:var(--text-3)">— created 7+ days ago, never recalled</span>
      </div>
      <div id="unused-list"></div>
    </div>
  </div>

  <!-- Conflicts Panel -->
  <div class="panel" id="panel-conflicts">
    <div id="conflicts-list"></div>
  </div>

  <!-- Audit Panel -->
  <div class="panel" id="panel-audit">
    <div class="sub-tabs">
      <button class="sub-tab active" onclick="loadAudit('all', this)">All</button>
      <button class="sub-tab" onclick="loadAudit('read', this)">Reads</button>
      <button class="sub-tab" onclick="loadAudit('write', this)">Writes</button>
    </div>
    <div id="audit-list"></div>
  </div>

  <!-- Namespaces Panel -->
  <div class="panel" id="panel-namespaces">
    <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden">
      <table class="ns-table">
        <thead>
          <tr>
            <th>Namespace</th>
            <th>Memories</th>
            <th>Private</th>
            <th>Last Updated</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="namespaces-list"></tbody>
      </table>
    </div>
  </div>

  <!-- Digest Panel -->
  <div class="panel" id="panel-digest">
    <div class="digest-grid" id="digest-stats"></div>
    <div id="digest-content"></div>
  </div>

</div>
</div>

<!-- Add/Edit Memory Modal -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModalOnOverlay(event)">
  <div class="modal" id="modal">
    <div class="modal-header">
      <div class="modal-title" id="modal-title">Add Memory</div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <label>Content</label>
        <textarea id="modal-content" placeholder="What should agents remember? Be specific — 'The API rate limit is 100 req/min', not 'API stuff'."></textarea>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Type</label>
          <select id="modal-type">
            <option value="fact">Fact — objective info</option>
            <option value="preference">Preference — user/system prefs</option>
            <option value="context">Context — situational background</option>
            <option value="result">Result — outcome of a task</option>
            <option value="instruction">Instruction — rules to follow</option>
          </select>
        </div>
        <div class="field">
          <label>TTL (days, optional)</label>
          <input type="number" id="modal-ttl" placeholder="e.g. 30 — leave blank for permanent" min="1" step="1">
          <div class="field-hint">Confidence decays to 0 after this many days</div>
        </div>
      </div>
      <div class="field">
        <label>Confidence: <span id="modal-conf-value">1.0</span></label>
        <div class="slider-wrap">
          <input type="range" class="conf-slider" id="modal-conf" min="0" max="1" step="0.05" value="1"
                 oninput="document.getElementById('modal-conf-value').textContent = parseFloat(this.value).toFixed(2)">
        </div>
        <div class="field-hint">Use &lt; 0.7 for uncertain or tentative information</div>
      </div>
      <div class="field">
        <label>Tags</label>
        <input type="text" id="modal-tags" placeholder="api, auth, infra (comma-separated)">
      </div>
      <div class="field">
        <div class="toggle-wrap">
          <label class="toggle">
            <input type="checkbox" id="modal-private">
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label">Private — never exported or synced</span>
        </div>
        <div class="field-hint">Use for API keys, credentials, and sensitive config</div>
      </div>
      <div id="modal-conflict-warn" style="display:none"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="modal-submit" onclick="submitMemory()">Save Memory</button>
    </div>
  </div>
</div>

<!-- Toast Container -->
<div id="toast-container"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────

let allMemories = [];
let displayedMemories = [];
let activeTypeFilter = 'all';
let sortOrder = 'accessed';
let searchQuery = '';
let searchTimeout = null;
let editingMemoryId = null;  // null = adding new, string = editing existing
let ns = 'shared';

// ── Init ──────────────────────────────────────────────────────────────────

window.addEventListener('DOMContentLoaded', () => {
  // Read namespace from URL or default
  const urlNs = new URLSearchParams(window.location.search).get('ns');
  if (urlNs) ns = urlNs;
  document.getElementById('ns-label').textContent = ns;
  refreshAll();
  setInterval(loadStats, 30000);
});

async function refreshAll() {
  await Promise.all([loadStats(), loadMemories()]);
}

// ── Stats ─────────────────────────────────────────────────────────────────

async function loadStats() {
  const data = await fetchJSON('/api/stats');
  if (!data) return;
  document.getElementById('stat-total').textContent = data.total_memories;
  document.getElementById('stat-stale').textContent = data.stale_memories;
  document.getElementById('stat-conflicts').textContent = data.open_contradictions;
  document.getElementById('stat-gaps').textContent = data.memory_gaps;
  document.getElementById('stat-unused').textContent = data.unused_memories;
  document.getElementById('tab-badge-memories').textContent = data.total_memories;
  document.getElementById('tab-badge-conflicts').textContent = data.open_contradictions;
}

// ── Memories ──────────────────────────────────────────────────────────────

async function loadMemories() {
  const data = await fetchJSON('/api/memories?limit=200');
  if (!data) return;
  allMemories = data;
  renderMemories();
}

function renderMemories() {
  let mems = [...allMemories];

  // Filter by type
  if (activeTypeFilter !== 'all') {
    mems = mems.filter(m => m.memory_type === activeTypeFilter);
  }

  // Filter by search query (client-side text filter)
  if (searchQuery && !isSemanticSearch) {
    const q = searchQuery.toLowerCase();
    mems = mems.filter(m =>
      m.content.toLowerCase().includes(q) ||
      (m.tags && JSON.parse(m.tags || '[]').some(t => t.toLowerCase().includes(q)))
    );
  }

  // Sort
  if (sortOrder === 'created') {
    mems.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  } else if (sortOrder === 'confidence') {
    mems.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
  } else if (sortOrder === 'content') {
    mems.sort((a, b) => a.content.localeCompare(b.content));
  }
  // default: accessed_at order (already from API)

  displayedMemories = mems;
  document.getElementById('memory-count').textContent = `${mems.length} shown`;

  const el = document.getElementById('memory-list');
  if (!mems.length) {
    el.innerHTML = `<div class="empty">
      <div class="empty-icon">◎</div>
      <div class="empty-title">No memories found</div>
      <div class="empty-sub">Try a different filter or add your first memory</div>
    </div>`;
    return;
  }

  el.innerHTML = mems.map(m => {
    const tags = parseTags(m.tags);
    const isStale = m.is_stale || false;
    const isPrivate = m.local_only || false;
    const typeClass = m.memory_type || 'fact';
    const conf = m.confidence || 1.0;
    const confColor = conf >= 0.7 ? '' : conf >= 0.4 ? 'amber' : 'red';

    return `<div class="memory-card ${isStale ? 'stale' : ''}" id="mc-${esc(m.id)}">
      <div class="memory-stripe ${typeClass}"></div>
      <div class="memory-body">
        <div class="memory-content">${escHtml(m.content)}</div>
        <div class="memory-meta">
          <span class="badge badge-${typeClass}">${typeClass}</span>
          ${isStale ? '<span class="badge badge-stale">⚠ Stale</span>' : ''}
          ${isPrivate ? '<span class="badge badge-private">🔒 Private</span>' : ''}
          <div class="meta-sep"></div>
          <div class="conf-bar-wrap">
            <div class="conf-bar"><div class="conf-bar-fill ${confColor}" style="width:${Math.round(conf*100)}%"></div></div>
            <span>${conf.toFixed(2)}</span>
          </div>
          <div class="meta-sep"></div>
          <span class="meta-text">from ${escHtml(m.source_agent || 'unknown')}</span>
          ${m.created_at ? `<div class="meta-sep"></div><span class="meta-text">${fmtDate(m.created_at)}</span>` : ''}
          ${tags.length ? `<div class="meta-sep"></div>${tags.map(t => `<span class="tag-chip">${escHtml(t)}</span>`).join('')}` : ''}
        </div>
      </div>
      <div class="memory-actions">
        <button class="action-btn edit" title="Edit" onclick="openEditModal('${esc(m.id)}')">✎</button>
        <button class="action-btn delete" title="Delete" onclick="deleteMemory('${esc(m.id)}')">⌫</button>
      </div>
    </div>`;
  }).join('');
}

function setTypeFilter(type, btn) {
  activeTypeFilter = type;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderMemories();
}

function setSortOrder(order) {
  sortOrder = order;
  renderMemories();
}

let isSemanticSearch = false;

function onSearchInput(val) {
  searchQuery = val.trim();
  clearTimeout(searchTimeout);

  if (!searchQuery) {
    isSemanticSearch = false;
    renderMemories();
    return;
  }

  // Debounce: after 400ms with no typing, do semantic search
  isSemanticSearch = false;
  renderMemories(); // client-side filter first

  searchTimeout = setTimeout(async () => {
    if (searchQuery.length >= 3) {
      const results = await fetchJSON(`/api/search?q=${encodeURIComponent(searchQuery)}&n=20`);
      if (results && Array.isArray(results)) {
        isSemanticSearch = true;
        displayedMemories = results;
        document.getElementById('memory-count').textContent = `${results.length} semantic matches`;
        const el = document.getElementById('memory-list');
        if (!results.length) {
          el.innerHTML = `<div class="empty"><div class="empty-icon">◎</div><div class="empty-title">No results</div></div>`;
          return;
        }
        // Re-use render but with results directly
        const temp = allMemories;
        allMemories = results;
        activeTypeFilter = 'all';
        renderMemories();
        allMemories = temp;
      }
    }
  }, 400);
}

async function deleteMemory(id) {
  if (!confirm('Delete this memory? This cannot be undone.')) return;
  const res = await fetch(`/api/memories/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (res.ok) {
    toast('Memory deleted', 'success');
    allMemories = allMemories.filter(m => m.id !== id);
    renderMemories();
    loadStats();
  } else {
    toast('Failed to delete memory', 'error');
  }
}

// ── Modal ─────────────────────────────────────────────────────────────────

function openModal() {
  editingMemoryId = null;
  document.getElementById('modal-title').textContent = 'Add Memory';
  document.getElementById('modal-submit').textContent = 'Save Memory';
  document.getElementById('modal-content').value = '';
  document.getElementById('modal-type').value = 'fact';
  document.getElementById('modal-conf').value = 1;
  document.getElementById('modal-conf-value').textContent = '1.00';
  document.getElementById('modal-tags').value = '';
  document.getElementById('modal-ttl').value = '';
  document.getElementById('modal-private').checked = false;
  document.getElementById('modal-conflict-warn').style.display = 'none';
  document.getElementById('modal-overlay').classList.add('open');
  setTimeout(() => document.getElementById('modal-content').focus(), 100);
}

function openEditModal(id) {
  const mem = allMemories.find(m => m.id === id);
  if (!mem) return;
  editingMemoryId = id;
  document.getElementById('modal-title').textContent = 'Edit Memory';
  document.getElementById('modal-submit').textContent = 'Update Memory';
  document.getElementById('modal-content').value = mem.content || '';
  document.getElementById('modal-type').value = mem.memory_type || 'fact';
  const conf = mem.confidence || 1.0;
  document.getElementById('modal-conf').value = conf;
  document.getElementById('modal-conf-value').textContent = conf.toFixed(2);
  const tags = parseTags(mem.tags);
  document.getElementById('modal-tags').value = tags.join(', ');
  document.getElementById('modal-ttl').value = mem.ttl_days || '';
  document.getElementById('modal-private').checked = mem.local_only || false;
  document.getElementById('modal-conflict-warn').style.display = 'none';
  document.getElementById('modal-overlay').classList.add('open');
  setTimeout(() => document.getElementById('modal-content').focus(), 100);
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  editingMemoryId = null;
}

function closeModalOnOverlay(e) {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
}

async function submitMemory() {
  const content = document.getElementById('modal-content').value.trim();
  if (!content) { toast('Content is required', 'error'); return; }

  const tagsRaw = document.getElementById('modal-tags').value;
  const tags = tagsRaw.split(',').map(t => t.trim()).filter(Boolean);
  const ttlRaw = document.getElementById('modal-ttl').value;
  const ttl = ttlRaw ? parseFloat(ttlRaw) : null;

  const body = {
    content,
    memory_type: document.getElementById('modal-type').value,
    confidence: parseFloat(document.getElementById('modal-conf').value),
    tags,
    local_only: document.getElementById('modal-private').checked,
    ttl_days: ttl
  };

  const btn = document.getElementById('modal-submit');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    let url = '/api/memories';
    let method = 'POST';
    if (editingMemoryId) {
      url = `/api/memories/${encodeURIComponent(editingMemoryId)}`;
      method = 'PUT';
    }
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok) {
      toast(data.detail || 'Failed to save memory', 'error');
      return;
    }

    // Show conflict warning if any
    if (data.result && data.result.conflict) {
      const c = data.result.conflict;
      const warnEl = document.getElementById('modal-conflict-warn');
      warnEl.className = 'conflict-warn';
      warnEl.style.display = 'block';
      warnEl.innerHTML = `⚠ Possible conflict (similarity ${c.similarity.toFixed(2)})<br>
        <span style="opacity:0.8">Existing: "${escHtml(c.existing_content.slice(0, 100))}"</span>`;
    }

    toast(editingMemoryId ? 'Memory updated' : 'Memory stored', 'success');
    closeModal();
    await loadMemories();
    loadStats();
  } finally {
    btn.disabled = false;
    btn.textContent = editingMemoryId ? 'Update Memory' : 'Save Memory';
  }
}

// ── Patterns ──────────────────────────────────────────────────────────────

async function loadPatterns() {
  const data = await fetchJSON('/api/patterns');
  if (!data) return;

  const topTopics = data.top_topics || [];
  document.getElementById('top-topics-count').textContent = topTopics.length;
  const ttEl = document.getElementById('top-topics-list');
  ttEl.innerHTML = topTopics.length
    ? topTopics.map(t => `<div class="pattern-card">
        <div class="pattern-query">${escHtml(t.query_text)}</div>
        <div class="pattern-stats">
          <div class="pattern-stat">
            <div class="pattern-stat-num">${t.query_count}</div>
            <div class="pattern-stat-label">Queries</div>
          </div>
          <div class="pattern-stat">
            <div class="pattern-stat-num">${(t.avg_similarity || 0).toFixed(2)}</div>
            <div class="pattern-stat-label">Avg Match</div>
          </div>
        </div>
      </div>`).join('')
    : `<div class="empty"><div class="empty-icon">◎</div><div class="empty-title">No queries yet</div><div class="empty-sub">Queries will appear here as agents use Mesh recall</div></div>`;

  const gaps = data.memory_gaps || [];
  document.getElementById('gaps-count').textContent = gaps.length;
  const gapEl = document.getElementById('gaps-list');
  gapEl.innerHTML = gaps.length
    ? gaps.map(g => `<div class="gap-card">
        <div class="gap-icon">⚠</div>
        <div class="gap-body">
          <div class="gap-query">${escHtml(g.query_text)}</div>
          <div class="gap-meta">Asked ${g.query_count}× · avg similarity ${(g.avg_similarity || 0).toFixed(2)} — consider adding a memory for this</div>
        </div>
      </div>`).join('')
    : `<div class="empty"><div class="empty-icon">✓</div><div class="empty-title">No gaps detected</div><div class="empty-sub">All frequent queries have good memory coverage</div></div>`;

  const unused = data.unused_memories || [];
  document.getElementById('unused-count').textContent = unused.length;
  const unusedEl = document.getElementById('unused-list');
  unusedEl.innerHTML = unused.length
    ? unused.map(m => `<div class="pattern-card">
        <div class="pattern-query">${escHtml(m.content)}</div>
        <div class="pattern-stats">
          <div class="pattern-stat">
            <div class="pattern-stat-num" style="font-size:12px;color:var(--text-3)">${fmtDate(m.created_at)}</div>
            <div class="pattern-stat-label">Created</div>
          </div>
        </div>
        <button class="action-btn delete" onclick="deleteMemory('${esc(m.id)}')" title="Delete">⌫</button>
      </div>`).join('')
    : `<div class="empty"><div class="empty-icon">✓</div><div class="empty-title">No unused memories</div><div class="empty-sub">Every memory has been accessed at least once</div></div>`;
}

// ── Conflicts ─────────────────────────────────────────────────────────────

async function loadConflicts() {
  const data = await fetchJSON('/api/contradictions');
  if (!data) return;
  const el = document.getElementById('conflicts-list');
  if (!data.length) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">✓</div><div class="empty-title">No open conflicts</div><div class="empty-sub">All contradictions have been resolved</div></div>`;
    return;
  }
  el.innerHTML = data.map(c => `<div class="conflict-card">
    <div class="conflict-header">
      <div>
        <span class="badge badge-stale">⚡ Conflict</span>
        <span class="meta-text" style="margin-left:8px">similarity ${c.similarity.toFixed(2)}</span>
      </div>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:12px" onclick="resolveConflict('${esc(c.id)}')">Mark Resolved</button>
    </div>
    <div class="conflict-pair">
      <div>
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin-bottom:6px">Existing memory</div>
        <div class="conflict-side conflict-old">${escHtml(c.existing_content)}</div>
      </div>
      <div>
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-3);margin-bottom:6px">New memory (just stored)</div>
        <div class="conflict-side conflict-new">${escHtml(c.new_content)}</div>
      </div>
    </div>
    <div class="conflict-meta">Detected ${fmtDate(c.detected_at)}</div>
  </div>`).join('');
}

async function resolveConflict(id) {
  await fetch(`/api/contradictions/${encodeURIComponent(id)}/resolve`, { method: 'POST' });
  toast('Conflict resolved', 'success');
  loadConflicts();
  loadStats();
}

// ── Audit ─────────────────────────────────────────────────────────────────

async function loadAudit(filter = 'all', btn = null) {
  if (btn) {
    document.querySelectorAll('.sub-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  const data = await fetchJSON(`/api/audit?namespace=${encodeURIComponent(ns)}&limit=100&action=${filter}`);
  if (!data) return;
  const el = document.getElementById('audit-list');
  if (!data.length) {
    el.innerHTML = `<div class="empty"><div class="empty-icon">◎</div><div class="empty-title">No audit entries</div></div>`;
    return;
  }
  el.innerHTML = data.map(e => {
    const isRead = e.action === 'recall';
    return `<div class="audit-entry">
      <div class="audit-icon ${isRead ? 'read' : 'write'}">${isRead ? '⌕' : '✎'}</div>
      <div class="audit-body">
        <div class="audit-top">
          <span class="audit-agent">${escHtml(e.agent_id || 'unknown')}</span>
          <span class="audit-time">${fmtDate(e.timestamp)}</span>
        </div>
        ${isRead
          ? `<div class="audit-text">Asked: "${escHtml(e.query || '')}"</div>
             ${e.top_result ? `<div class="audit-sub">→ ${escHtml(e.top_result.slice(0,100))}${e.top_result.length > 100 ? '…' : ''}</div>` : ''}`
          : `<div class="audit-text">Stored: "${escHtml((e.content || '').slice(0,120))}${(e.content||'').length > 120 ? '…' : ''}"</div>`
        }
      </div>
    </div>`;
  }).join('');
}

// ── Namespaces ────────────────────────────────────────────────────────────

async function loadNamespaces() {
  const data = await fetchJSON('/api/namespaces');
  if (!data) return;
  const el = document.getElementById('namespaces-list');
  if (!data.length) {
    el.innerHTML = `<tr><td colspan="5" style="text-align:center;padding:32px;color:var(--text-3)">No namespaces found</td></tr>`;
    return;
  }
  el.innerHTML = data.map(n => `<tr>
    <td><span class="ns-name">${escHtml(n.namespace)}</span></td>
    <td style="color:var(--text-2)">${n.memory_count}</td>
    <td style="color:var(--text-2)">${n.local_only_count}</td>
    <td class="meta-text">${fmtDate(n.last_updated)}</td>
    <td>
      <div class="ns-actions">
        <button class="btn btn-ghost" style="padding:4px 10px;font-size:11px" onclick="downloadExport('${escHtml(n.namespace)}')">Export</button>
        <button class="btn btn-ghost" style="padding:4px 10px;font-size:11px;border-color:rgba(248,113,113,0.3);color:var(--red)" onclick="deleteNs('${escHtml(n.namespace)}')">Delete</button>
      </div>
    </td>
  </tr>`).join('');
}

async function deleteNs(namespace) {
  if (!confirm(`Delete namespace '${namespace}' and ALL its memories? This cannot be undone.`)) return;
  await fetch(`/api/namespaces/${encodeURIComponent(namespace)}`, { method: 'DELETE' });
  toast(`Namespace '${namespace}' deleted`, 'success');
  loadNamespaces();
  loadStats();
}

async function downloadExport(namespace) {
  const data = await fetchJSON(`/api/export/${encodeURIComponent(namespace)}?include_embeddings=false`);
  if (!data) return;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `mesh_${namespace}_export.json`;
  a.click();
  URL.revokeObjectURL(url);
  toast(`Exported ${data.memory_count} memories`, 'success');
}

// ── Digest ────────────────────────────────────────────────────────────────

async function loadDigest() {
  const data = await fetchJSON(`/api/digest?namespace=${encodeURIComponent(ns)}&hours=24`);
  if (!data) return;

  document.getElementById('digest-stats').innerHTML = `
    <div class="digest-stat">
      <div class="digest-stat-num" style="color:var(--mint)">${data.new_count}</div>
      <div class="digest-stat-label">New Memories</div>
    </div>
    <div class="digest-stat">
      <div class="digest-stat-num" style="color:var(--red)">${data.contradiction_count}</div>
      <div class="digest-stat-label">Contradictions</div>
    </div>
    <div class="digest-stat">
      <div class="digest-stat-num" style="color:var(--amber)">${data.stale_count}</div>
      <div class="digest-stat-label">Going Stale</div>
    </div>
  `;

  let html = '';

  if (data.new_count > 0) {
    html += `<div class="pattern-section">
      <div class="section-header"><div class="section-title">New Memories</div><div class="section-badge">${data.new_count}</div></div>
      ${data.new_memories.slice(0, 10).map(m => `<div class="gap-card" style="border-color:var(--border)">
        <div class="gap-icon">${m.local_only ? '🔒' : '+'}</div>
        <div class="gap-body">
          <div class="gap-query">${escHtml(m.content)}</div>
          <div class="gap-meta">[${escHtml(m.memory_type)}] from ${escHtml(m.source_agent)}</div>
        </div>
      </div>`).join('')}
    </div>`;
  }

  if (data.contradiction_count > 0) {
    html += `<div class="pattern-section">
      <div class="section-header"><div class="section-title">Contradictions</div><div class="section-badge">${data.contradiction_count}</div></div>
      ${data.contradictions.map(c => `<div class="gap-card" style="border-color:rgba(248,113,113,0.2)">
        <div class="gap-icon">⚡</div>
        <div class="gap-body">
          <div class="gap-query">${escHtml(c.content.slice(0,120))}</div>
          <div class="gap-meta">flagged by ${escHtml(c.agent_id)}</div>
        </div>
      </div>`).join('')}
    </div>`;
  }

  if (data.stale_count > 0) {
    html += `<div class="pattern-section">
      <div class="section-header"><div class="section-title">Going Stale</div><div class="section-badge">${data.stale_count}</div></div>
      ${data.stale_memories.slice(0, 5).map(m => `<div class="gap-card" style="border-color:rgba(251,191,36,0.2)">
        <div class="gap-icon">⚠</div>
        <div class="gap-body">
          <div class="gap-query">${escHtml(m.content.slice(0,120))}</div>
          <div class="gap-meta">TTL: ${m.ttl_days} days, stored ${fmtDate(m.created_at)}</div>
        </div>
      </div>`).join('')}
    </div>`;
  }

  if (!html) {
    html = `<div class="empty"><div class="empty-icon">✓</div><div class="empty-title">All clear — no activity in last 24h</div></div>`;
  }

  document.getElementById('digest-content').innerHTML = html;
}

// ── Tab navigation ────────────────────────────────────────────────────────

function showTab(name, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  btn.classList.add('active');

  if (name === 'patterns')   loadPatterns();
  if (name === 'conflicts')  loadConflicts();
  if (name === 'namespaces') loadNamespaces();
  if (name === 'audit')      loadAudit();
  if (name === 'digest')     loadDigest();
}

// ── Utilities ─────────────────────────────────────────────────────────────

async function fetchJSON(url) {
  try {
    const res = await fetch(url);
    return res.ok ? res.json() : null;
  } catch { return null; }
}

function parseTags(raw) {
  if (!raw) return [];
  try { return JSON.parse(raw); } catch { return []; }
}

function escHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function esc(s) {
  return String(s || '').replace(/'/g, "\\'");
}

function fmtDate(s) {
  if (!s) return '';
  try {
    const d = new Date(s);
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
    return d.toLocaleDateString();
  } catch { return s.slice(0, 10); }
}

function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${type === 'success' ? '✓' : '✕'}</span> ${escHtml(msg)}`;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// Keyboard shortcut: N = new memory
document.addEventListener('keydown', e => {
  if (e.key === 'n' && !e.ctrlKey && !e.metaKey &&
      document.activeElement.tagName !== 'INPUT' &&
      document.activeElement.tagName !== 'TEXTAREA') {
    openModal();
  }
  if (e.key === 'Escape') closeModal();
});
</script>
</body>
</html>"""


@api.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


# ── CLI entry point ─────────────────────────────────────────────────────────

@click.command()
@click.option("--namespace", default="shared", envvar="MESH_NAMESPACE",
              help="Mesh namespace to inspect (default: shared)")
@click.option("--port", default=7433, help="Port to run on (default: 7433)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser")
def run(namespace: str, port: int, no_browser: bool):
    """Start the Mesh dashboard at http://localhost:<port>"""
    global _mesh
    _mesh = Mesh(namespace=namespace, agent_id="dashboard")

    url = f"http://localhost:{port}"
    print(f"\n  ◈  Mesh Dashboard")
    print(f"     {url}")
    print(f"     namespace: {namespace}")
    print(f"     Press Ctrl+C to stop.\n")

    if not no_browser:
        def open_browser():
            time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(api, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    run()