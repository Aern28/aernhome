"""Best-effort Todoist bridge for Nexus write-paths.

Crossing something off IN the Nexus should close its Todoist twin, and new
for-Aern queue items should appear in Todoist so they ride his normal GTD flow.
Every function here degrades to a no-op (returns None/False) on any failure —
Todoist being down must never break a Nexus write. Same API dialect as
maint_todoist_push.py (Todoist API v1; REST v2 died 2026-02-10).
"""
import requests

from nexus_sources import _get_todoist_token

API = "https://api.todoist.com/api/v1/tasks"
QUEUE_PREFIX = "Nexus: "
MAINT_PREFIX = "Home maintenance: "

# queue priority 1|2|3 (1=highest) -> Todoist priority 4|3|2 (4=urgent)
_PRIORITY_MAP = {1: 4, 2: 3, 3: 2}


def _headers():
    token = _get_todoist_token()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def create_task(content, priority=2, due_string=None):
    """Create an open Todoist task; returns its id or None."""
    H = _headers()
    if not H:
        return None
    payload = {"content": content, "priority": _PRIORITY_MAP.get(priority, 3)}
    if due_string:
        payload["due_string"] = due_string
    try:
        r = requests.post(API, headers=H, json=payload, timeout=15)
        if r.status_code < 300:
            return (r.json() or {}).get("id")
    except requests.RequestException:
        pass
    return None


def close_task(task_id):
    """Close a Todoist task by id; returns True on success."""
    H = _headers()
    if not (H and task_id):
        return False
    try:
        r = requests.post(f"{API}/{task_id}/close", headers=H, timeout=15)
        return r.status_code < 300
    except requests.RequestException:
        return False


def active_task_ids():
    """Set of ALL active task ids (as strings), following v1 pagination.
    Returns None — never an empty set — on ANY failure, so the reverse-sync
    caller can never mistake a Todoist outage for 'every task was completed'.
    Hard-capped at 20 pages (4000 tasks) against a broken cursor loop."""
    H = _headers()
    if not H:
        return None
    ids = set()
    params = {"limit": 200}
    try:
        for _ in range(20):
            r = requests.get(API, headers=H, params=params, timeout=15)
            if r.status_code >= 300:
                return None
            data = r.json()
            tasks = data.get("results", data) if isinstance(data, dict) else data
            if not isinstance(tasks, list):
                return None
            ids.update(str(t["id"]) for t in tasks if isinstance(t, dict) and t.get("id") is not None)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor:
                return ids
            params["cursor"] = cursor
        return ids
    except (requests.RequestException, ValueError):
        return None


def close_by_content(content):
    """Close the open Todoist task whose content matches exactly (first match).
    Used when we never stored the task id (e.g. maintenance tasks created by
    the daily push job). Returns True if something was closed."""
    H = _headers()
    if not H:
        return False
    try:
        r = requests.get(API, headers=H, timeout=15)
        if r.status_code >= 300:
            return False
        results = r.json()
        tasks = results.get("results", results) if isinstance(results, dict) else results
        for t in tasks or []:
            if t.get("content") == content:
                return close_task(t.get("id"))
    except (requests.RequestException, ValueError):
        pass
    return False
