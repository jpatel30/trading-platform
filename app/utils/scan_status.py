"""
Lightweight in-memory scan status tracker for the UI progress bar.
Single-process assumption (matches current uvicorn --reload deployment).
Stale entries auto-expire after 3 min in case a scan crashes mid-way.
"""
import time
import threading

_lock   = threading.Lock()
_status: dict[str, dict] = {}

STAGES = {
    "queued":       (0,   "Starting scan..."),
    "prices":       (10,  "Fetching live prices..."),
    "flow":         (20,  "Fetching options flow & dark pool..."),
    "regime":       (30,  "Checking market regime..."),
    "enriching":    (40,  "Analyzing candidates..."),
    "enriched":     (65,  "Candidates analyzed"),
    "llm_thinking": (70,  "Consulting AI model..."),
    "llm_done":     (90,  "Recommendations ready"),
    "storing":      (95,  "Saving results..."),
    "complete":     (100, "Done"),
    "error":        (100, "Scan failed"),
}

def set_scan_status(user_id: str, stage: str, detail: str = "") -> None:
    pct, label = STAGES.get(stage, (0, stage))
    with _lock:
        _status[user_id] = {
            "stage": stage, "pct": pct, "label": label,
            "detail": detail, "updated_at": time.time(),
        }

def get_scan_status(user_id: str) -> dict:
    with _lock:
        s = _status.get(user_id)
    if not s or time.time() - s["updated_at"] > 180:
        return {"stage": "idle", "pct": 0, "label": "", "detail": ""}
    return s

def clear_scan_status(user_id: str) -> None:
    with _lock:
        _status.pop(user_id, None)
