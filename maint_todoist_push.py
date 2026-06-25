"""Push overdue nexus maintenance items to Todoist. Runs inside the aernhome
container (TODOIST_TOKEN in env, nexus.db at /data). Stateless dedup: skips items
that already have an open Todoist task with our prefix, so it's safe to run daily.
Replaces the retired n8n 'Maintenance Log to Inbox' flow.

Deployed: scheduled task "Nexus Maint Todoist" on Ashaman (daily 8 AM) runs
run-maint-push.cmd -> docker exec aernhome-dashboard python /data/maint_todoist_push.py.
(Runtime copy lives in the /data volume so it needs no image rebuild; this repo copy
is the tracked source — move the wrapper to /app on a future rebuild if desired.)"""
import os, sys, sqlite3, datetime, requests
sys.path.insert(0, "/app")
from nexus_sources import _get_todoist_token

PREFIX = "Home maintenance: "
API = "https://api.todoist.com/api/v1/tasks"


def main():
    token = _get_todoist_token()
    if not token:
        print("no Todoist token in env"); return
    H = {"Authorization": "Bearer " + token}

    db = os.path.join(os.environ.get("DATA_DIR", "/data"), "nexus.db")
    c = sqlite3.connect(db)
    today = datetime.date.today().isoformat()
    overdue = c.execute(
        "SELECT task, due_date FROM maintenance "
        "WHERE completed = 0 AND due_date IS NOT NULL AND date(due_date) <= date(?) "
        "ORDER BY date(due_date)", (today,)).fetchall()

    # existing open Todoist tasks carrying our prefix -> dedup set
    r = requests.get(API, headers=H, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("results") if isinstance(data, dict) else data
    have = set()
    for t in (items or []):
        ct = t.get("content", "")
        if ct.startswith(PREFIX):
            have.add(ct[len(PREFIX):])

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


main()
