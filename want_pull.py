# created-by: fable
# created: 2026-09-06
# purpose: read Aern's Google Keep list "TCG List" (the phone-side want inbox) and dump its unchecked items to /data/want_inbox.json for the host-side ingest - READ-ONLY on Keep; runs inside the aernhome container (the only place with gkeepapi + the token)
# lifespan: infrastructure
# project: pc-collection (C:/Projects/tcg-inventory-tool/pc-collection/project.md)
import json, os, re, sys, datetime

LIST_ID = os.environ.get("WANT_LIST_ID", "1a079a4581b.942665969e66e94b")   # "TCG List", made 2026-09-06
OUT = "/data/want_inbox.json"
TOKEN = "/data/keep_token.txt"

sys.path.insert(0, "/app")
import gkeepapi
src = open("/app/keep_sync.py", encoding="utf-8").read()
m = re.search(r'^EMAIL\s*=\s*(.+)$', src, re.M)
try:
    EMAIL = eval(m.group(1), {"os": os}) if m else None
except Exception:
    EMAIL = None
EMAIL = EMAIL or os.environ.get("KEEP_EMAIL") or "mcarroll203@gmail.com"

keep = gkeepapi.Keep()
keep.authenticate(EMAIL, open(TOKEN, encoding="utf-8").read().strip())
node = keep.get(LIST_ID)
if node is None or not isinstance(node, gkeepapi.node.List):
    print(f"want_pull: list {LIST_ID} not found"); sys.exit(2)

items = [{"keep_item_id": it.id, "text": it.text.strip(), "checked": bool(it.checked)}
         for it in node.items if it.text and it.text.strip()]
payload = {"pulled_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "list_id": LIST_ID, "list_title": node.title, "items": items}
tmp = OUT + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=1, ensure_ascii=False)
os.replace(tmp, OUT)
print(f"want_pull: {len(items)} item(s) ({sum(1 for i in items if not i['checked'])} open) -> {OUT}")
