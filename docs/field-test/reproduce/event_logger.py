import json
import time
from pathlib import Path
from datetime import datetime, timezone

EVENTS_FILE = Path(__file__).resolve().parent.parent / "raw" / "events.jsonl"

def log_event(session_id: str, agent_id: str, action: str, tool: str, duration_ms: float, notes: str = "", extra: dict = None):
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "agent_id": agent_id,
        "action": action,
        "tool": tool,
        "duration_ms": round(duration_ms, 2),
        "notes": notes,
        "extra": extra or {}
    }
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
