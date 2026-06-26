"""Two-way sync between nexus maintenance and Todoist. Runs inside the aernhome
container (TODOIST_TOKEN in env, nexus.db at /data).

  1. SYNC BACK  — maintenance tasks you completed in Todoist get marked done in the
     nexus log (recurring items roll their due_date forward, one-offs close). Without
     this the Todoist task vanished but the Nexus log still showed it due — and the
     next push re-created it. Idempotent: only touches rows that are still due+open,
     so a rolled-forward item is never double-completed or re-pushed.
  2. PUSH       — still-overdue maintenance items get an open Todoist task. Stateless
     dedup by our content prefix, so it's safe to run daily.

Replaces the retired n8n 'Maintenance Log to Inbox' flow. Deployed: scheduled task
"Nexus Maint Todoist" on Ashaman (daily 8 AM) runs run-maint-push.cmd ->
docker exec aernhome-dashboard python /data/maint_todoist_push.py. (Runtime copy lives
in the /data volume so it needs no image rebuild; this repo copy is the tracked source.)"""
import os, sys, sqlite3, datetime, requests
sys.path.insert(0, "/app")
from nexus_sources import _get_todoist_token
from nexus_writes import complete_maintenance

PREFIX = "Home maintenance: "
API = "https://api.todoist.com/api/v1/tasks"
COMPLETED_API = "https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
LOOKBACK_DAYS = 14  # window for "completed in Todoist" — covers missed daily runs


def _db():
    return os.path.join(os.environ.get("DATA_DIR", "/data"), "nexus.db")


def sync_back(H):
    """Mark nexus maintenance rows done for tasks completed in Todoist.

    Matches by task name (same key the push uses). Only rows that are still open and
    due/overdue are eligible, so re-running can't double-roll a recurring item or
    close something twice."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    until = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        r = requests.get(COMPLETED_API, headers=H, params={"since": since, "until": until}, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print("  sync-back: could not fetch completed tasks:", e)
        return 0
    completed_names = {
        c["content"][len(PREFIX):]
        for c in items
        if isinstance(c, dict) and (c.get("content") or "").startswith(PREFIX)
    }
    if not completed_names:
        print("  sync-back: no completed 'Home maintenance:' tasks in window")
        return 0

    today = datetime.date.today().isoformat()
    con = sqlite3.connect(_db())
    con.row_factory = sqlite3.Row
    eligible = con.execute(
        "SELECT id, task FROM maintenance "
        "WHERE completed = 0 AND due_date IS NOT NULL AND date(due_date) <= date(?)",
        (today,),
    ).fetchall()
    con.close()

    synced = 0
    for row in eligible:
        if row["task"] in completed_names:
            try:
                complete_maintenance(row["id"])  # rolls recurring forward / closes one-offs
                synced += 1
                print("  synced done:", row["task"])
            except Exception as e:
                print("  sync FAIL:", row["task"], e)
    return synced


def push(H):
    """Create Todoist tasks for still-overdue maintenance items (deduped by prefix)."""
    con = sqlite3.connect(_db())
    today = datetime.date.today().isoformat()
    overdue = con.execute(
        "SELECT task, due_date FROM maintenance "
        "WHERE completed = 0 AND due_date IS NOT NULL AND date(due_date) <= date(?) "
        "ORDER BY date(due_date)", (today,)).fetchall()
    con.close()

    r = requests.get(API, headers=H, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("results") if isinstance(data, dict) else data
    have = {t["content"][len(PREFIX):] for t in (items or [])
            if (t.get("content") or "").startswith(PREFIX)}

    created = 0
    for task, due in overdue:
        if task in have:
            continue
        payload = {"content": PREFIX + task, "due_string": due, "priority": 2}
        rr = requests.post(API, headers=H, json=payload, timeout=20)
        if rr.status_code in (200, 201, 204):
            created += 1
            print("  created:", task)
        else:
            print("  FAIL:", task, rr.status_code, rr.text[:150])
    print("overdue=%d created=%d already-present=%d"
          % (len(overdue), created, len(overdue) - created))


def main():
    token = _get_todoist_token()
    if not token:
        print("no Todoist token in env"); return
    H = {"Authorization": "Bearer " + token}
    print("sync-back: %d marked done" % sync_back(H))   # before push, so synced items don't re-push
    push(H)


main()
