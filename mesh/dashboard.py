"""
Mesh Dashboard — Minimal, high-speed web UI for managing shared agent memory.

Start: mesh-dashboard
       mesh-dashboard --namespace my-project --port 7433
"""

from __future__ import annotations

import threading
import time
import webbrowser
from typing import Any, List, Optional

import click
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .audit import get_audit_log, log_forget, log_learn, log_recall, log_update
from .digest import generate_digest
from .io import export_namespace
from .memory import VALID_MEMORY_TYPES, Mesh, get_engine_status, warmup_model
from .namespaces import delete_namespace, list_namespaces, namespace_stats
from .version import VERSION

api = FastAPI(title="Mesh Dashboard", version=VERSION)

_MESH_LOCK = threading.RLock()
_MESH_CACHE: dict[str, Mesh] = {}
_default_namespace: str = "shared"
_mesh: Optional[Mesh] = None


def get_mesh(namespace: Optional[str] = None) -> Mesh:
    """Return a cached Mesh instance for the requested (or default) namespace."""
    global _mesh
    ns = (namespace or (_mesh.namespace if _mesh is not None else _default_namespace)).strip() or "shared"
    with _MESH_LOCK:
        if _mesh is not None and _mesh.namespace == ns:
            return _mesh
        inst = _MESH_CACHE.get(ns)
        if inst is None:
            inst = Mesh(namespace=ns, agent_id="dashboard")
            _MESH_CACHE[ns] = inst
        if _mesh is None:
            _mesh = inst
        return inst


# --- Request models ---


class CreateMemoryRequest(BaseModel):
    content: str
    memory_type: str = "fact"
    confidence: float = 1.0
    tags: List[str] = Field(default_factory=list)
    local_only: bool = False
    ttl_days: Optional[float] = None
    namespace: Optional[str] = None


class UpdateMemoryRequest(BaseModel):
    content: Optional[str] = None
    memory_type: Optional[str] = None
    confidence: Optional[float] = None
    tags: Optional[List[str]] = None
    local_only: Optional[bool] = None
    ttl_days: Optional[float] = None
    namespace: Optional[str] = None


# --- REST API endpoints ---


@api.get("/api/stats")
def get_stats(namespace: Optional[str] = None) -> dict[str, Any]:
    mesh = get_mesh(namespace)
    patterns = mesh.patterns()
    contradictions = mesh.contradictions()
    return {
        "namespace": mesh.namespace,
        "total_memories": mesh.count(),
        "open_contradictions": len(contradictions),
        "memory_gaps": len(patterns["memory_gaps"]),
        "stale_memories": len(patterns["stale_memories"]),
        "unused_memories": len(patterns["unused_memories"]),
        "engine": get_engine_status(),
    }


@api.get("/api/memories")
def get_memories(limit: int = 200, namespace: Optional[str] = None) -> list[dict[str, Any]]:
    return get_mesh(namespace).inspect(limit=limit)


@api.post("/api/memories")
def create_memory(req: CreateMemoryRequest) -> dict[str, Any]:
    """Add a new memory from the dashboard."""
    try:
        mesh = get_mesh(req.namespace)
        result = mesh.learn(
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            local_only=req.local_only,
            ttl_days=req.ttl_days if req.ttl_days and req.ttl_days > 0 else None,
        )
        log_learn(
            content=req.content,
            namespace=mesh.namespace,
            agent_id="dashboard",
            result=result,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.put("/api/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateMemoryRequest) -> dict[str, Any]:
    """Update a memory in-place, preserving its ID, creation time, and access count."""
    try:
        mesh = get_mesh(req.namespace)
        result = mesh.update(
            memory_id=memory_id,
            content=req.content,
            memory_type=req.memory_type,
            confidence=req.confidence,
            tags=req.tags,
            local_only=req.local_only,
            ttl_days=req.ttl_days if req.ttl_days and req.ttl_days > 0 else None,
            reset_ttl=True,
        )
        log_update(
            memory_id=memory_id,
            content=req.content,
            namespace=mesh.namespace,
            agent_id="dashboard",
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str, namespace: Optional[str] = None) -> dict[str, str]:
    mesh = get_mesh(namespace)
    deleted = mesh.forget(memory_id)
    if deleted:
        log_forget(memory_id=memory_id, namespace=mesh.namespace, agent_id="dashboard")
    return {"status": "deleted" if deleted else "not_found", "memory_id": memory_id}


@api.get("/api/patterns")
def get_patterns(namespace: Optional[str] = None) -> dict[str, Any]:
    return get_mesh(namespace).patterns()


@api.get("/api/contradictions")
def get_contradictions(namespace: Optional[str] = None) -> list[dict[str, Any]]:
    return get_mesh(namespace).contradictions()


@api.post("/api/contradictions/{contradiction_id}/resolve")
def resolve_contradiction(contradiction_id: str, namespace: Optional[str] = None) -> dict[str, str]:
    get_mesh(namespace).resolve_contradiction(contradiction_id)
    return {"status": "resolved"}


@api.get("/api/search")
def search(q: str, n: int = 25, namespace: Optional[str] = None) -> list[dict[str, Any]]:
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    mesh = get_mesh(namespace)
    results = mesh.recall(q, n=n)
    log_recall(query=q, namespace=mesh.namespace, agent_id="dashboard", results=results)
    return results


@api.get("/api/namespaces")
def get_namespaces() -> list[dict[str, Any]]:
    return list_namespaces()


@api.get("/api/namespaces/{namespace}/stats")
def get_namespace_stats(namespace: str) -> dict[str, Any]:
    try:
        return namespace_stats(namespace)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.delete("/api/namespaces/{namespace}")
def api_delete_namespace(namespace: str) -> dict[str, Any]:
    global _mesh
    with _MESH_LOCK:
        _MESH_CACHE.pop(namespace, None)
        if _mesh is not None and _mesh.namespace == namespace:
            _mesh = None
    count = delete_namespace(namespace)
    return {"deleted": True, "memories_removed": count}


@api.get("/api/export/{namespace}")
def api_export_namespace(namespace: str, include_embeddings: bool = False) -> dict[str, Any]:
    return export_namespace(namespace=namespace, include_embeddings=include_embeddings)


@api.get("/api/audit")
async def get_audit(
    namespace: Optional[str] = None,
    limit: int = 100,
    action: str = "all",
) -> list[dict[str, Any]]:
    mesh = get_mesh(namespace)
    return get_audit_log(namespace=mesh.namespace, limit=limit, action_filter=action)


@api.get("/api/digest")
async def get_digest(namespace: Optional[str] = None, hours: int = 24) -> dict[str, Any]:
    mesh = get_mesh(namespace)
    return generate_digest(namespace=mesh.namespace, hours=hours)


# --- Minimal, Responsive Dashboard HTML (RAW STRING to preserve JS regex backslashes) ---

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mesh</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:       #090a0f;
  --surface:  #10121a;
  --surface2: #161924;
  --border:   rgba(255, 255, 255, 0.07);
  --border2:  rgba(255, 255, 255, 0.13);
  --text:     #eceff4;
  --muted:    #8b95a7;
  --subtle:   #525b6d;
  --accent:   #10b981;
  --accent-bg:rgba(16, 185, 129, 0.12);
  --blue:     #3b82f6;
  --purple:   #8b5cf6;
  --amber:    #f59e0b;
  --red:      #ef4444;
  --font:     -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  --mono:     "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 13px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}

::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--surface2); border-radius: 3px; }

.app {
  max-width: 1120px;
  margin: 0 auto;
  padding: 0 16px 40px;
}

/* Top Bar */
header {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 50px;
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: rgba(9, 10, 15, 0.88);
  backdrop-filter: blur(10px);
  z-index: 50;
  flex-wrap: wrap;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-weight: 600;
  font-size: 14px;
  letter-spacing: -0.02em;
  color: var(--text);
}

.brand-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}

.ns-select {
  background: var(--surface);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 12px;
  font-family: var(--mono);
  outline: none;
  cursor: pointer;
}
.ns-select:hover, .ns-select:focus { border-color: var(--border2); color: var(--text); }

.search-wrap {
  flex: 1;
  min-width: 180px;
  max-width: 360px;
  position: relative;
}

.search-wrap input {
  width: 100%;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 26px 5px 10px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.12s;
}
.search-wrap input:focus { border-color: var(--accent); }
.search-wrap input::placeholder { color: var(--subtle); }

.search-kbd {
  position: absolute;
  right: 7px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 10px;
  font-family: var(--mono);
  color: var(--subtle);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 0 4px;
  pointer-events: none;
}

.metrics-inline {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
  font-size: 12px;
  color: var(--muted);
}

.metric-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.metric-val {
  font-family: var(--mono);
  font-weight: 600;
  color: var(--text);
}
.metric-val.warn { color: var(--amber); }
.metric-val.danger { color: var(--red); }

.btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  font-family: var(--font);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  transition: all 0.12s;
  white-space: nowrap;
}
.btn:hover { background: var(--surface2); border-color: var(--border2); }

.btn-primary {
  background: var(--accent);
  color: #04130d;
  border-color: transparent;
  font-weight: 600;
}
.btn-primary:hover { background: #0ea572; border-color: transparent; }

.btn-danger { color: var(--red); border-color: rgba(239, 68, 68, 0.25); }
.btn-danger:hover { background: rgba(239, 68, 68, 0.1); }

/* Sub-navigation & Toolbar */
.subnav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.tabs {
  display: flex;
  gap: 2px;
  overflow-x: auto;
}

.tab {
  padding: 4px 10px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  background: transparent;
  border: none;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: all 0.12s;
}
.tab:hover { color: var(--text); background: var(--surface); }
.tab.active { color: var(--text); background: var(--surface2); }

.tab-count {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--subtle);
}
.tab.active .tab-count { color: var(--accent); }

.toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.chip {
  padding: 3px 8px;
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--muted);
  background: transparent;
  border: 1px solid transparent;
  cursor: pointer;
}
.chip:hover { color: var(--text); background: var(--surface); }
.chip.active {
  color: var(--accent);
  background: var(--accent-bg);
  border-color: rgba(16, 185, 129, 0.25);
}

.sort-select {
  margin-left: auto;
  background: var(--surface);
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 3px 7px;
  font-size: 11.5px;
  outline: none;
}

/* Panels & Compact Rows */
.panel { display: none; }
.panel.active { display: block; }

.list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  overflow: hidden;
}

.row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  transition: background 0.1s;
}
.row:last-child { border-bottom: none; }
.row:hover { background: rgba(255, 255, 255, 0.018); }

.row-main { flex: 1; min-width: 0; }

.row-top {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}

.row-content {
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--text);
  word-break: break-word;
  line-height: 1.5;
}

.row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--subtle);
}

.type-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  background: var(--surface2);
  color: var(--muted);
  flex-shrink: 0;
}
.type-pill.fact        { color: #60a5fa; background: rgba(59,130,246,0.12); }
.type-pill.preference  { color: #a78bfa; background: rgba(139,92,246,0.12); }
.type-pill.context     { color: #34d399; background: rgba(16,185,129,0.12); }
.type-pill.result      { color: #4ade80; background: rgba(74,222,128,0.12); }
.type-pill.instruction { color: #fbbf24; background: rgba(245,158,11,0.12); }
.type-pill.process     { color: #38bdf8; background: rgba(56,189,248,0.12); }
.type-pill.decision    { color: #f472b6; background: rgba(244,114,182,0.12); }
.type-pill.error       { color: #f87171; background: rgba(248,113,113,0.12); }
.type-pill.general     { color: #94a3b8; background: rgba(148,163,184,0.12); }

.tag {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--muted);
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 0 5px;
  border-radius: 3px;
}

.flag {
  font-size: 10px;
  padding: 0 5px;
  border-radius: 3px;
  font-weight: 500;
}
.flag.stale { color: var(--amber); background: rgba(245,158,11,0.12); }
.flag.private { color: var(--red); background: rgba(239,68,68,0.12); }
.flag.sim { color: var(--accent); background: var(--accent-bg); font-family: var(--mono); }

.row-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
  opacity: 0.35;
  transition: opacity 0.12s;
}
.row:hover .row-actions { opacity: 1; }

.icon-btn {
  background: transparent;
  border: 1px solid transparent;
  color: var(--muted);
  width: 24px;
  height: 24px;
  border-radius: 4px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}
.icon-btn:hover { background: var(--surface2); border-color: var(--border2); color: var(--text); }
.icon-btn.del:hover { color: var(--red); border-color: rgba(239,68,68,0.3); }

/* Sections & Grids */
.section-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  margin: 14px 0 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.conflict-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 6px;
}

.conflict-box {
  padding: 8px 10px;
  border-radius: 6px;
  font-family: var(--mono);
  font-size: 11.5px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.conflict-box.old { border-left: 2px solid var(--red); }
.conflict-box.new { border-left: 2px solid var(--accent); }

.empty {
  padding: 36px 16px;
  text-align: center;
  color: var(--subtle);
  font-size: 12.5px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
}

/* Minimal Modal */
.backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 16px;
}
.backdrop.open { display: flex; }

.modal {
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: 10px;
  width: 100%;
  max-width: 480px;
  padding: 16px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
}

.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  font-weight: 600;
  font-size: 13.5px;
}

.field { margin-bottom: 10px; display: flex; flex-direction: column; gap: 4px; }
.field label { font-size: 11px; color: var(--muted); font-weight: 500; }
.field textarea, .field input[type="text"], .field input[type="number"], .field select {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 9px;
  color: var(--text);
  font-family: var(--font);
  font-size: 12.5px;
  outline: none;
}
.field textarea {
  font-family: var(--mono);
  min-height: 76px;
  resize: vertical;
}
.field textarea:focus, .field input:focus, .field select:focus { border-color: var(--accent); }

.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }

.modal-foot { display: flex; justify-content: flex-end; gap: 6px; margin-top: 12px; }

#toast {
  position: fixed;
  bottom: 16px;
  right: 16px;
  background: var(--surface2);
  border: 1px solid var(--border2);
  color: var(--text);
  padding: 7px 12px;
  border-radius: 6px;
  font-size: 12px;
  display: none;
  z-index: 200;
}

@media (max-width: 720px) {
  header { height: auto; padding: 8px 0; }
  .search-wrap { order: 3; max-width: 100%; flex-basis: 100%; }
  .metrics-inline { display: none; }
  .conflict-grid, .field-row { grid-template-columns: 1fr; }
  .row-actions { opacity: 1; }
}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <span class="brand-dot"></span>
      <span>Mesh</span>
    </div>
    <select id="ns-select" class="ns-select" onchange="switchNamespace(this.value)" title="Switch Namespace"></select>
    <div class="search-wrap">
      <input type="text" id="search-input" placeholder="Search memories (semantic + keyword)..." oninput="onSearch(this.value)">
      <span class="search-kbd">/</span>
    </div>
    <div class="metrics-inline">
      <span class="metric-pill">Memories <span class="metric-val" id="m-total">0</span></span>
      <span class="metric-pill">Stale <span class="metric-val warn" id="m-stale">0</span></span>
      <span class="metric-pill">Conflicts <span class="metric-val danger" id="m-conflicts">0</span></span>
      <span class="metric-pill">Gaps <span class="metric-val" id="m-gaps">0</span></span>
    </div>
    <button class="btn" onclick="refreshAll()" title="Refresh">&#x21bb;</button>
    <button class="btn btn-primary" onclick="openModal()">+ New</button>
  </header>

  <div class="subnav">
    <div class="tabs">
      <button class="tab active" onclick="showTab('memories', this)">Memories <span class="tab-count" id="tc-memories">0</span></button>
      <button class="tab" onclick="showTab('conflicts', this)">Conflicts <span class="tab-count" id="tc-conflicts">0</span></button>
      <button class="tab" onclick="showTab('patterns', this)">Patterns</button>
      <button class="tab" onclick="showTab('audit', this)">Audit</button>
      <button class="tab" onclick="showTab('namespaces', this)">Namespaces</button>
      <button class="tab" onclick="showTab('digest', this)">Digest</button>
    </div>
    <span id="shown-count" style="font-size:11.5px;color:var(--subtle);font-family:var(--mono)"></span>
  </div>

  <!-- Memories Panel -->
  <div class="panel active" id="panel-memories">
    <div class="toolbar">
      <button class="chip active" onclick="setType('all', this)">All</button>
      <button class="chip" onclick="setType('fact', this)">Fact</button>
      <button class="chip" onclick="setType('decision', this)">Decision</button>
      <button class="chip" onclick="setType('process', this)">Process</button>
      <button class="chip" onclick="setType('instruction', this)">Instruction</button>
      <button class="chip" onclick="setType('preference', this)">Preference</button>
      <button class="chip" onclick="setType('context', this)">Context</button>
      <button class="chip" onclick="setType('result', this)">Result</button>
      <button class="chip" onclick="setType('error', this)">Error</button>
      <button class="chip" onclick="setType('general', this)">General</button>
      <select class="sort-select" onchange="setSort(this.value)">
        <option value="accessed">Sort: Recent access</option>
        <option value="created">Sort: Created</option>
        <option value="confidence">Sort: Confidence</option>
      </select>
    </div>
    <div id="memory-list"></div>
  </div>

  <!-- Conflicts Panel -->
  <div class="panel" id="panel-conflicts">
    <div id="conflicts-list"></div>
  </div>

  <!-- Patterns Panel -->
  <div class="panel" id="panel-patterns">
    <div class="section-label">Top Queries</div>
    <div id="patterns-topics"></div>
    <div class="section-label">Memory Gaps (Low Similarity)</div>
    <div id="patterns-gaps"></div>
    <div class="section-label">Unused Memories (7+ Days)</div>
    <div id="patterns-unused"></div>
  </div>

  <!-- Audit Panel -->
  <div class="panel" id="panel-audit">
    <div class="toolbar">
      <button class="chip active" onclick="loadAudit('all', this)">All</button>
      <button class="chip" onclick="loadAudit('read', this)">Reads</button>
      <button class="chip" onclick="loadAudit('write', this)">Writes</button>
      <button class="chip" onclick="loadAudit('update', this)">Updates</button>
      <button class="chip" onclick="loadAudit('forget', this)">Deletes</button>
    </div>
    <div id="audit-list"></div>
  </div>

  <!-- Namespaces Panel -->
  <div class="panel" id="panel-namespaces">
    <div id="namespaces-list"></div>
  </div>

  <!-- Digest Panel -->
  <div class="panel" id="panel-digest">
    <div id="digest-list"></div>
  </div>
</div>

<!-- Add / Edit Modal -->
<div class="backdrop" id="backdrop" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-head">
      <span id="modal-title">New Memory</span>
      <button class="icon-btn" onclick="closeModal()" title="Close">&#x2715;</button>
    </div>
    <div class="field">
      <label>Content</label>
      <textarea id="f-content" placeholder="Specific fact, decision, or rule..."></textarea>
    </div>
    <div class="field-row">
      <div class="field">
        <label>Type</label>
        <select id="f-type">
          <option value="fact">fact</option>
          <option value="decision">decision</option>
          <option value="process">process</option>
          <option value="instruction">instruction</option>
          <option value="preference">preference</option>
          <option value="context">context</option>
          <option value="result">result</option>
          <option value="error">error</option>
          <option value="general">general</option>
        </select>
      </div>
      <div class="field">
        <label>Confidence (0.0 - 1.0)</label>
        <input type="number" id="f-conf" min="0" max="1" step="0.05" value="1.0">
      </div>
    </div>
    <div class="field-row">
      <div class="field">
        <label>Tags (comma-separated)</label>
        <input type="text" id="f-tags" placeholder="api, db, auth">
      </div>
      <div class="field">
        <label>TTL Days (optional)</label>
        <input type="number" id="f-ttl" min="1" step="1" placeholder="Permanent">
      </div>
    </div>
    <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;margin-top:2px">
      <input type="checkbox" id="f-private"> Private (local_only - exclude from exports)
    </label>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" id="f-submit" onclick="saveMemory()">Save</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let ns = new URLSearchParams(location.search).get('ns') || '';
let memories = [];
let searchResults = null;
let activeType = 'all';
let activeTab = 'memories';
let activeAuditFilter = 'all';
let sortBy = 'accessed';
let editId = null;
let debounceTimer = null;

window.addEventListener('DOMContentLoaded', async () => {
  await refreshAll();
});

async function apiFetch(path, opts = {}) {
  try {
    const res = await fetch(path, opts);
    return res.ok ? await res.json() : null;
  } catch { return null; }
}

async function refreshAll() {
  const stats = await apiFetch(`/api/stats${ns ? '?namespace=' + encodeURIComponent(ns) : ''}`);
  if (stats) {
    ns = stats.namespace;
    document.getElementById('m-total').textContent = stats.total_memories;
    document.getElementById('m-stale').textContent = stats.stale_memories;
    document.getElementById('m-conflicts').textContent = stats.open_contradictions;
    document.getElementById('m-gaps').textContent = stats.memory_gaps;
    document.getElementById('tc-memories').textContent = stats.total_memories;
    document.getElementById('tc-conflicts').textContent = stats.open_contradictions;
  }
  await populateNamespaceDropdown();
  await loadMemories();
  if (activeTab === 'conflicts') await loadConflicts();
  else if (activeTab === 'patterns') await loadPatterns();
  else if (activeTab === 'audit') await loadAudit(activeAuditFilter);
  else if (activeTab === 'namespaces') await loadNamespaces();
  else if (activeTab === 'digest') await loadDigest();
}

async function populateNamespaceDropdown() {
  const list = await apiFetch('/api/namespaces') || [];
  const names = new Set(list.map(x => x.namespace));
  if (ns) names.add(ns);
  const sel = document.getElementById('ns-select');
  sel.innerHTML = Array.from(names).sort().map(n =>
    `<option value="${esc(n)}" ${n === ns ? 'selected' : ''}>${esc(n)}</option>`
  ).join('');
}

function switchNamespace(newNs) {
  ns = newNs;
  searchResults = null;
  document.getElementById('search-input').value = '';
  refreshAll();
}

async function loadMemories() {
  const data = await apiFetch(`/api/memories?limit=200&namespace=${encodeURIComponent(ns)}`);
  memories = Array.isArray(data) ? data : [];
  renderMemories();
}

function parseTags(m) {
  if (Array.isArray(m.tags_list)) return m.tags_list;
  if (Array.isArray(m.tags)) return m.tags;
  if (typeof m.tags === 'string' && m.tags.trim()) {
    try { const p = JSON.parse(m.tags); return Array.isArray(p) ? p : []; } catch { return []; }
  }
  return [];
}

function renderMemories() {
  let list = searchResults !== null ? [...searchResults] : [...memories];
  if (activeType !== 'all') {
    list = list.filter(m => (m.memory_type || 'fact') === activeType);
  }
  if (searchResults === null) {
    if (sortBy === 'accessed') {
      list.sort((a, b) => (b.accessed_at || b.created_at || '').localeCompare(a.accessed_at || a.created_at || ''));
    } else if (sortBy === 'created') {
      list.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    } else if (sortBy === 'confidence') {
      list.sort((a, b) => (b.effective_confidence ?? b.confidence ?? 1) - (a.effective_confidence ?? a.confidence ?? 1));
    }
  }
  document.getElementById('shown-count').textContent =
    searchResults !== null ? `${list.length} matches` : `${list.length} items`;

  const root = document.getElementById('memory-list');
  if (!list.length) {
    root.innerHTML = `<div class="empty">No memories match the current view.</div>`;
    return;
  }
  root.innerHTML = `<div class="list">` + list.map(m => {
    const t = m.memory_type || 'fact';
    const tags = parseTags(m);
    const conf = (m.effective_confidence ?? m.confidence ?? 1.0).toFixed(2);
    const sim = m.similarity !== undefined ? `<span class="flag sim">${Math.round(m.similarity * 100)}% match</span>` : '';
    const stale = m.is_stale ? `<span class="flag stale">stale</span>` : '';
    const priv = m.local_only ? `<span class="flag private">private</span>` : '';
    return `<div class="row">
      <div class="row-main">
        <div class="row-top">
          <span class="type-pill ${esc(t)}">${esc(t)}</span>
          <span class="row-content">${esc(m.content)}</span>
        </div>
        <div class="row-meta">
          ${sim}${stale}${priv}
          <span>conf ${conf}</span>
          <span>by ${esc(m.source_agent || 'agent')}</span>
          ${m.access_count ? `<span>${m.access_count}x read</span>` : ''}
          ${m.created_at ? `<span>${relTime(m.created_at)}</span>` : ''}
          ${tags.map(tag => `<span class="tag">#${esc(tag)}</span>`).join('')}
        </div>
      </div>
      <div class="row-actions">
        <button class="icon-btn" title="Edit" onclick="openEdit('${escAttr(m.id)}')">&#x270e;</button>
        <button class="icon-btn del" title="Delete" onclick="delMemory('${escAttr(m.id)}')">&#x2715;</button>
      </div>
    </div>`;
  }).join('') + `</div>`;
}

function setType(type, btn) {
  activeType = type;
  document.querySelectorAll('#panel-memories .chip').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderMemories();
}

function setSort(val) {
  sortBy = val;
  renderMemories();
}

function onSearch(val) {
  clearTimeout(debounceTimer);
  const q = val.trim();
  if (!q) {
    searchResults = null;
    renderMemories();
    return;
  }
  const ql = q.toLowerCase();
  searchResults = memories.filter(m =>
    (m.content || '').toLowerCase().includes(ql) ||
    parseTags(m).some(t => t.toLowerCase().includes(ql))
  );
  renderMemories();
  if (q.length >= 2) {
    debounceTimer = setTimeout(async () => {
      const res = await apiFetch(`/api/search?q=${encodeURIComponent(q)}&n=25&namespace=${encodeURIComponent(ns)}`);
      if (Array.isArray(res)) {
        searchResults = res;
        renderMemories();
      }
    }, 220);
  }
}

function openModal() {
  editId = null;
  document.getElementById('modal-title').textContent = 'New Memory';
  document.getElementById('f-content').value = '';
  document.getElementById('f-type').value = 'fact';
  document.getElementById('f-conf').value = '1.0';
  document.getElementById('f-tags').value = '';
  document.getElementById('f-ttl').value = '';
  document.getElementById('f-private').checked = false;
  document.getElementById('backdrop').classList.add('open');
  setTimeout(() => document.getElementById('f-content').focus(), 40);
}

function openEdit(id) {
  const pool = searchResults !== null ? [...searchResults, ...memories] : memories;
  const m = pool.find(x => x.id === id);
  if (!m) return;
  editId = id;
  document.getElementById('modal-title').textContent = 'Edit Memory';
  document.getElementById('f-content').value = m.content || '';
  document.getElementById('f-type').value = m.memory_type || 'fact';
  document.getElementById('f-conf').value = m.confidence ?? 1.0;
  document.getElementById('f-tags').value = parseTags(m).join(', ');
  document.getElementById('f-ttl').value = m.ttl_days || '';
  document.getElementById('f-private').checked = Boolean(m.local_only);
  document.getElementById('backdrop').classList.add('open');
}

function closeModal() {
  document.getElementById('backdrop').classList.remove('open');
  editId = null;
}

async function saveMemory() {
  const content = document.getElementById('f-content').value.trim();
  if (!content) return notify('Content is required');
  const tags = document.getElementById('f-tags').value.split(',').map(s => s.trim()).filter(Boolean);
  const ttlRaw = document.getElementById('f-ttl').value;
  const payload = {
    content,
    memory_type: document.getElementById('f-type').value,
    confidence: parseFloat(document.getElementById('f-conf').value) || 1.0,
    tags,
    ttl_days: ttlRaw ? parseFloat(ttlRaw) : null,
    local_only: document.getElementById('f-private').checked,
    namespace: ns
  };
  const url = editId ? `/api/memories/${encodeURIComponent(editId)}` : '/api/memories';
  const method = editId ? 'PUT' : 'POST';
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (res.ok) {
    const wasEdit = Boolean(editId);
    closeModal();
    searchResults = null;
    document.getElementById('search-input').value = '';
    notify(wasEdit ? 'Memory updated' : 'Memory saved');
    await refreshAll();
  } else {
    notify('Failed to save memory');
  }
}

async function delMemory(id) {
  if (!confirm('Delete this memory?')) return;
  const res = await fetch(`/api/memories/${encodeURIComponent(id)}?namespace=${encodeURIComponent(ns)}`, { method: 'DELETE' });
  if (res.ok) {
    memories = memories.filter(m => m.id !== id);
    if (searchResults) searchResults = searchResults.filter(m => m.id !== id);
    renderMemories();
    notify('Memory deleted');
    refreshAll();
  }
}

async function loadConflicts() {
  const list = await apiFetch(`/api/contradictions?namespace=${encodeURIComponent(ns)}`) || [];
  const el = document.getElementById('conflicts-list');
  if (!list.length) {
    el.innerHTML = `<div class="empty">No unresolved conflicts in '${esc(ns)}'.</div>`;
    return;
  }
  el.innerHTML = `<div class="list">` + list.map(c => `<div class="row" style="flex-direction:column">
    <div style="display:flex;justify-content:space-between;width:100%;align-items:center">
      <span class="row-meta"><span class="flag stale">conflict</span> similarity ${(c.similarity || 0).toFixed(2)} &middot; ${relTime(c.detected_at)}</span>
      <button class="btn" onclick="resolveConflict('${escAttr(c.id)}')">Mark Resolved</button>
    </div>
    <div class="conflict-grid" style="width:100%">
      <div class="conflict-box old"><div style="font-size:10px;color:var(--subtle);margin-bottom:3px">EXISTING</div>${esc(c.existing_content)}</div>
      <div class="conflict-box new"><div style="font-size:10px;color:var(--subtle);margin-bottom:3px">NEW</div>${esc(c.new_content)}</div>
    </div>
  </div>`).join('') + `</div>`;
}

async function resolveConflict(id) {
  await fetch(`/api/contradictions/${encodeURIComponent(id)}/resolve?namespace=${encodeURIComponent(ns)}`, { method: 'POST' });
  notify('Conflict resolved');
  loadConflicts();
  refreshAll();
}

async function loadPatterns() {
  const data = await apiFetch(`/api/patterns?namespace=${encodeURIComponent(ns)}`);
  if (!data) return;
  const tEl = document.getElementById('patterns-topics');
  tEl.innerHTML = (data.top_topics || []).length
    ? `<div class="list">` + data.top_topics.map(t => `<div class="row">
        <span class="row-content">${esc(t.query_text)}</span>
        <span class="row-meta">${t.query_count} queries &middot; avg sim ${(t.avg_similarity || 0).toFixed(2)}</span>
      </div>`).join('') + `</div>`
    : `<div class="empty">No queries logged yet.</div>`;

  const gEl = document.getElementById('patterns-gaps');
  gEl.innerHTML = (data.memory_gaps || []).length
    ? `<div class="list">` + data.memory_gaps.map(g => `<div class="row">
        <span class="row-content">${esc(g.query_text)}</span>
        <span class="row-meta"><span class="flag stale">gap</span> asked ${g.query_count}x &middot; avg sim ${(g.avg_similarity || 0).toFixed(2)}</span>
      </div>`).join('') + `</div>`
    : `<div class="empty">No memory gaps detected.</div>`;

  const uEl = document.getElementById('patterns-unused');
  uEl.innerHTML = (data.unused_memories || []).length
    ? `<div class="list">` + data.unused_memories.map(m => `<div class="row">
        <span class="row-content">${esc(m.content)}</span>
        <button class="icon-btn del" title="Delete" onclick="delMemory('${escAttr(m.id)}')">&#x2715;</button>
      </div>`).join('') + `</div>`
    : `<div class="empty">No unused memories.</div>`;
}

async function loadAudit(filter = 'all', btn = null) {
  activeAuditFilter = filter;
  if (btn) {
    document.querySelectorAll('#panel-audit .chip').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
  const list = await apiFetch(`/api/audit?namespace=${encodeURIComponent(ns)}&limit=100&action=${encodeURIComponent(filter)}`) || [];
  const el = document.getElementById('audit-list');
  if (!list.length) {
    el.innerHTML = `<div class="empty">No audit activity recorded yet.</div>`;
    return;
  }
  el.innerHTML = `<div class="list">` + list.map(e => `<div class="row">
    <div class="row-main">
      <div class="row-top">
        <span class="type-pill">${esc(e.action)}</span>
        <span class="row-content">${esc(e.action === 'recall' ? e.query : e.content)}</span>
      </div>
      <div class="row-meta">
        <span>by ${esc(e.agent_id || 'agent')}</span>
        <span>${relTime(e.timestamp)}</span>
        ${e.top_result ? `<span>&rarr; "${esc(e.top_result.slice(0, 70))}"</span>` : ''}
      </div>
    </div>
  </div>`).join('') + `</div>`;
}

async function loadNamespaces() {
  const list = await apiFetch('/api/namespaces') || [];
  const el = document.getElementById('namespaces-list');
  if (!list.length) {
    el.innerHTML = `<div class="empty">No namespaces found.</div>`;
    return;
  }
  el.innerHTML = `<div class="list">` + list.map(n => `<div class="row" style="align-items:center">
    <div class="row-main">
      <div class="row-top">
        <span class="row-content" style="font-weight:600;cursor:pointer" onclick="switchNamespace('${escAttr(n.namespace)}')">${esc(n.namespace)}</span>
      </div>
      <div class="row-meta">
        <span>${n.memory_count} memories</span>
        <span>${n.local_only_count} private</span>
        <span>updated ${relTime(n.last_updated)}</span>
      </div>
    </div>
    <div style="display:flex;gap:6px">
      <button class="btn" onclick="exportNs('${escAttr(n.namespace)}')">Export</button>
      <button class="btn btn-danger" onclick="deleteNs('${escAttr(n.namespace)}')">Delete</button>
    </div>
  </div>`).join('') + `</div>`;
}

async function exportNs(targetNs) {
  const data = await apiFetch(`/api/export/${encodeURIComponent(targetNs)}?include_embeddings=false`);
  if (!data) return;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `mesh_${targetNs}.json`;
  a.click();
  notify(`Exported ${data.memory_count} memories`);
}

async function deleteNs(targetNs) {
  if (!confirm(`Delete namespace '${targetNs}' and all its memories?`)) return;
  await fetch(`/api/namespaces/${encodeURIComponent(targetNs)}`, { method: 'DELETE' });
  if (targetNs === ns) {
    ns = '';
    searchResults = null;
  }
  notify(`Deleted ${targetNs}`);
  await loadNamespaces();
  await refreshAll();
}

async function loadDigest() {
  const d = await apiFetch(`/api/digest?namespace=${encodeURIComponent(ns)}&hours=24`);
  const el = document.getElementById('digest-list');
  if (!d) return;
  if (!d.new_count && !d.contradiction_count && !d.stale_count) {
    el.innerHTML = `<div class="empty">All clear &mdash; no new activity or stale memories in the last 24h.</div>`;
    return;
  }
  const items = (d.new_memories || []).map(m => `<div class="row">
    <div class="row-main">
      <div class="row-top"><span class="type-pill ${esc(m.memory_type)}">${esc(m.memory_type)}</span><span class="row-content">${esc(m.content)}</span></div>
      <div class="row-meta"><span>by ${esc(m.source_agent)}</span><span>${relTime(m.created_at)}</span></div>
    </div>
  </div>`).join('');
  el.innerHTML = `<div class="section-label">Last 24h: ${d.new_count} new &middot; ${d.total_recalls} recalls &middot; ${d.contradiction_count} conflicts</div>` +
    (items ? `<div class="list">${items}</div>` : '');
}

function showTab(name, btn) {
  activeTab = name;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'conflicts') loadConflicts();
  if (name === 'patterns') loadPatterns();
  if (name === 'audit') loadAudit(activeAuditFilter);
  if (name === 'namespaces') loadNamespaces();
  if (name === 'digest') loadDigest();
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escAttr(s) {
  return String(s ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function relTime(s) {
  if (!s || s === 'never') return 'never';
  const d = new Date(s);
  if (isNaN(d)) return String(s).slice(0, 10);
  const sec = (Date.now() - d.getTime()) / 1000;
  if (sec < 60) return 'just now';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
  if (sec < 604800) return Math.floor(sec / 86400) + 'd ago';
  return d.toLocaleDateString();
}

function notify(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.display = 'none'; }, 2400);
}

document.addEventListener('keydown', e => {
  const activeTag = document.activeElement ? document.activeElement.tagName : '';
  if (e.key === '/' && activeTag !== 'INPUT' && activeTag !== 'TEXTAREA' && activeTag !== 'SELECT') {
    e.preventDefault();
    document.getElementById('search-input').focus();
  } else if (e.key === 'n' && !e.metaKey && !e.ctrlKey && activeTag !== 'INPUT' && activeTag !== 'TEXTAREA' && activeTag !== 'SELECT') {
    e.preventDefault();
    openModal();
  } else if (e.key === 'Escape') {
    closeModal();
  }
});
</script>
</body>
</html>"""


@api.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


# --- CLI entry point ---


@click.command()
@click.option(
    "--namespace",
    default="shared",
    envvar="MESH_NAMESPACE",
    help="Mesh namespace to inspect (default: shared)",
)
@click.option("--port", default=7433, help="Port to run on (default: 7433)")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser")
def run(namespace: str, port: int, no_browser: bool) -> None:
    """Start the Mesh dashboard at http://localhost:<port>"""
    global _mesh, _default_namespace
    _default_namespace = namespace
    _mesh = Mesh(namespace=namespace, agent_id="dashboard")
    threading.Thread(target=warmup_model, args=(namespace,), daemon=True).start()

    url = f"http://localhost:{port}"
    print("\n  Mesh Dashboard")
    print(f"  {url}")
    print(f"  namespace: {namespace}")
    print("  Press Ctrl+C to stop.\n")

    if not no_browser:
        def open_browser() -> None:
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(api, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    run()