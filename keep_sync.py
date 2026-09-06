# created-by: fable
# created: 2026-09-02
# purpose: two-way join between Google Keep "Grocery List" (voice inbox) and the Nexus restock table (the store)
# lifespan: infrastructure
# project: household-restock
"""keep_sync.py — Google Keep "Grocery List" <-> Nexus restock table.

Aern, 2026-09-02: the restock list has ONE store (the Nexus restock table, feeds
/nexus/house + the TRMNL + Aernbot's add/got verb). Google Keep's "Grocery List"
is the *voice inbox* — it's what Google Assistant writes to when he says "add
milk to my shopping list" — so this joins the two: whatever lands in either
place shows up in both, and checking/clearing on one side clears the other.
The Mealie "Household Restock" list was retired the same day ("kill it").

Scope (2026-09-05): only SYNC_CATEGORIES (grocery, house) mirror to Keep. Rowan/
Jace/Business rows are Nexus/TRMNL-only; their Keep lines get DELETED (never
ticked) and a Keep add that routes out of scope lands in Nexus then leaves Keep.

Rules (per item, decided by which SIDE changed since the last run — no clocks):
    Keep unchecked, not yet linked  -> match an open Nexus row by name, else add
                                       (category from KEYWORDS / trailing "to <cat>")
    Nexus open, not yet linked      -> add to Keep (so the phone shows the whole list)
    linked, Nexus side changed      -> push to Keep  (row cleared -> tick; reopened -> untick)
    linked, Keep side changed       -> push to Nexus (ticked -> done; unticked -> reopen)
    linked, Keep item deleted       -> Nexus done ("removed in Keep"), link dropped
Links + last-seen flags live in /data/keep_sync_state.json; a dead-man stamp goes
to /data/keep_sync_last.json for fleet.check_keep_sync(). ONLY the pinned list id
is ever read or written — Keep's other lists are off limits by design.

Auth: gkeepapi master token in /data/keep_token.txt (Bitwarden secure note
"Google Keep Master Token"; minted via the EmbeddedSetup/gpsoauth flow — there is
no consumer Keep API and app passwords are dead). The token is never printed.

Usage (inside aernhome-dashboard; host task "Keep Sync" runs it q15m):
    python keep_sync.py             # sync once, print a one-line summary
    python keep_sync.py --dry-run   # show what would change, write nothing
"""
import datetime
import json
import os
import re
import sys
import tempfile
from urllib.request import Request, urlopen

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BASE = os.environ.get("NEXUS_BASE", "http://127.0.0.1:5555")
TOKEN_PATH = os.path.join(DATA_DIR, "keep_token.txt")
STATE_PATH = os.path.join(DATA_DIR, "keep_sync_state.json")
STAMP_PATH = os.path.join(DATA_DIR, "keep_sync_last.json")
EMAIL = os.environ.get("KEEP_EMAIL", "mcarroll203@gmail.com")
LIST_ID = os.environ.get("KEEP_LIST_ID", "1567028679377.1771595269")  # "Grocery List" — the Assistant shopping list
ADDED_BY = "keep"
CATEGORIES = ("house", "grocery", "rowan", "jace", "business")
# Only these categories mirror to the phone (Aern 2026-09-05: "keep = grocery + house").
# Rowan/Jace/Business rows live on Nexus + the TRMNL only; a Keep line that routes to an
# out-of-scope category is deleted from Keep after landing in Nexus (deleted, NOT ticked —
# a tick means "got it" and would clear the Nexus row).
SYNC_CATEGORIES = ("grocery", "house")
# Word-boundary keyword -> category. Deliberately narrow; extend from real misses,
# not guesses. Since the 2026-09-05 grocery split, an ambiguous KEEP item defaults
# to GROCERY (this list is the grocery voice inbox — "add cheese bread" is food
# until proven otherwise); the 'house' keywords catch the durable staples.
KEYWORDS = {
    "house": ("tp", "toilet paper", "paper towel", "paper towels", "toothpaste", "toothbrush",
              "shaver", "razor", "devacurl", "shampoo", "conditioner", "deodorant", "soap",
              "detergent", "laundry", "dish soap", "dishwasher", "trash bags", "garbage bags",
              "batteries", "light bulb", "lightbulb", "cleaner", "sponges", "paper plates",
              "napkins", "foil", "ziploc"),
    "rowan": ("litter", "kitten", "cat food", "cat treats", "nexgard"),
    "jace": ("dog food", "dog treats", "ollie", "jerky", "puppy", "heartgard", "simparica"),
    "business": ("sleeves", "toploader", "toploaders", "shields", "packing slip", "packing slips",
                 "bubble mailer", "bubble mailers", "mailers", "shipping labels", "team bags"),
}


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def categorize(text):
    """('item text', category). Honors the API's trailing 'to <cat>' / 'for <cat>'
    form first (a voice add can say it), then the keyword map, else GROCERY
    (Keep is the grocery voice inbox; 2026-09-05 split)."""
    t = (text or "").strip()
    for c in CATEGORIES:
        for tail in (" to " + c, " for " + c, " (" + c + ")"):
            if t.lower().endswith(tail):
                return t[: -len(tail)].strip(), c
    low = " " + _norm(t) + " "
    for cat, words in KEYWORDS.items():
        if any((" " + w + " ") in low or low.startswith(" " + w) for w in words):
            return t, cat
    return t, "grocery"


LABELS = {"house": "House", "grocery": "Grocery", "rowan": "Rowan", "jace": "Jace", "business": "Business"}


def keep_text_for(row, n_open):
    """What a Nexus row is called in Keep. Pet rows always carry '(Rowan)' / '(Jace)'
    (Aern 2026-09-02: 'separate Jace and Rowan' — 'dry food' exists under both), and so
    does any name that collides across categories. categorize() reads the same tag on
    the way back, so the tag doubles as the routing."""
    name, cat = row["item"], row["category"]
    collides = any(_norm(r["item"]) == _norm(name) and r["category"] != cat for r in n_open.values())
    if cat in ("rowan", "jace") or collides:
        return f"{name} ({LABELS.get(cat, cat)})"
    return name


# --- Nexus side -------------------------------------------------------------
class Nexus:
    """Thin client over /api/restock (the same gate + verbs Aernbot's restock.py uses)."""

    def __init__(self, base=BASE, dry_run=False):
        self.base, self.dry_run = base, dry_run

    def _call(self, body=None):
        req = Request(self.base + "/api/restock", headers={"Content-Type": "application/json",
                                                           "User-Agent": "aernhome keep_sync"},
                      data=json.dumps(body).encode("utf-8") if body is not None else None,
                      method="POST" if body is not None else "GET")
        with urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def open_items(self):
        """{id: {"id","item","category"}} for every OPEN row."""
        out = {}
        for c in self._call().get("categories", []):
            for i in c.get("items", []):
                out[int(i["id"])] = {"id": int(i["id"]), "item": i["item"], "category": c["key"]}
        return out

    def add(self, item, category):
        if self.dry_run:
            return None
        r = self._call({"item": item, "category": category, "added_by": ADDED_BY})
        return int(r["id"]) if r.get("ok") else None

    def done(self, rid):
        if self.dry_run:
            return 0
        return int(self._call({"done": rid}).get("cleared", 0))


# --- Keep side --------------------------------------------------------------
class KeepList:
    """The ONE pinned Keep list. Everything else in the account is invisible here."""

    def __init__(self, token, dry_run=False):
        import gkeepapi  # imported late so --help / tests don't need it
        self.dry_run = dry_run
        self.keep = gkeepapi.Keep()
        self.keep.authenticate(EMAIL, token)
        self.node = self.keep.get(LIST_ID)
        if self.node is None or not isinstance(self.node, gkeepapi.node.List):
            raise SystemExit(f"Keep list {LIST_ID} not found or not a list")
        self._dirty = False

    def items(self):
        """{keep_item_id: {"id","text","checked"}} for live (non-deleted) items."""
        return {it.id: {"id": it.id, "text": it.text, "checked": bool(it.checked)}
                for it in self.node.items if not getattr(it, "deleted", False)}

    def add(self, text):
        if self.dry_run:
            return None
        import gkeepapi
        it = self.node.add(text, False, gkeepapi.node.NewListItemPlacementValue.Top)
        self._dirty = True
        return it.id

    def set_checked(self, keep_id, checked):
        if self.dry_run:
            return
        for it in self.node.items:
            if it.id == keep_id:
                it.checked = bool(checked)
                self._dirty = True
                return

    def set_text(self, keep_id, text):
        if self.dry_run:
            return
        for it in self.node.items:
            if it.id == keep_id:
                it.text = text
                self._dirty = True
                return

    def delete(self, keep_id):
        """Remove a line from the Keep list (scope cleanup — never a 'got it')."""
        if self.dry_run:
            return
        for it in self.node.items:
            if it.id == keep_id:
                it.delete()
                self._dirty = True
                return

    def commit(self):
        if self._dirty and not self.dry_run:
            self.keep.sync()
            self._dirty = False


# --- state ------------------------------------------------------------------
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_atomic(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _read_token():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        raise SystemExit(f"no Keep token at {TOKEN_PATH} (install from Bitwarden 'Google Keep Master Token')")
    if not tok:
        raise SystemExit("Keep token file is empty")
    return tok


# --- reconcile --------------------------------------------------------------
def reconcile(nexus, keep, state, log):
    """Pure-ish: reads both sides, applies the rules through the adapters, mutates state.
    `state["links"]` = {keep_id: {"nexus_id", "nexus_open", "keep_checked", "text"}}.
    Returns counts."""
    links = state.setdefault("links", {})
    n_open = nexus.open_items()                       # id -> row
    k_items = keep.items()                            # keep_id -> item
    counts = {"keep_to_nexus": 0, "nexus_to_keep": 0, "cleared_nexus": 0, "ticked_keep": 0,
              "reopened": 0, "matched": 0, "renamed": 0, "descoped": 0}
    by_nexus = {v["nexus_id"]: k for k, v in links.items() if v.get("nexus_id") is not None}
    n_open_by_name = {}
    for row in n_open.values():
        n_open_by_name.setdefault(_norm(row["item"]), row)

    # 1) linked items: whichever side moved since last run wins
    for keep_id in list(links.keys()):
        link = links[keep_id]
        nid = link.get("nexus_id")
        k = k_items.get(keep_id)
        n_is_open = nid in n_open
        if k is None:
            # deleted in Keep. Ticked-then-deleted is his normal cleanup (already
            # reconciled); an unticked delete means "don't need it" -> clear Nexus.
            if n_is_open and not link.get("keep_checked"):
                log(f"cleared in Nexus (removed in Keep): {link.get('text')!r}")
                nexus.done(nid)
                counts["cleared_nexus"] += 1
            del links[keep_id]
            continue
        if n_is_open and n_open[nid]["category"] not in SYNC_CATEGORIES:
            # out-of-scope category (pets/business): remove the phone line, keep the
            # Nexus row open — even if he ticked it, a tick on a descoped line is not
            # trusted as "got it" (the sweep and the tick race; Nexus stays canonical)
            log(f"descoped from Keep (category {n_open[nid]['category']}): {k['text']!r}")
            keep.delete(keep_id)
            del links[keep_id]
            counts["descoped"] += 1
            continue
        nexus_changed = n_is_open != bool(link.get("nexus_open"))
        keep_changed = k["checked"] != bool(link.get("keep_checked"))
        if nexus_changed:
            # Nexus side moved (Aernbot "got X" / a Nexus reopen) -> mirror to Keep
            if k["checked"] != (not n_is_open):
                log(f"{'tick' if not n_is_open else 'untick'} in Keep: {k['text']!r}")
                keep.set_checked(keep_id, not n_is_open)
                counts["ticked_keep" if not n_is_open else "reopened"] += 1
            link["keep_checked"] = not n_is_open
        elif keep_changed:
            if k["checked"] and n_is_open:
                log(f"cleared in Nexus (ticked in Keep): {k['text']!r}")
                nexus.done(nid)
                counts["cleared_nexus"] += 1
                n_is_open = False
            elif not k["checked"] and not n_is_open:
                text, cat = categorize(k["text"])
                log(f"reopened in Nexus (unticked in Keep): {text!r} -> {cat}")
                new_id = nexus.add(text, cat)   # add() reopens a cleared row
                if new_id is not None:
                    link["nexus_id"] = new_id
                    n_is_open = True
                counts["reopened"] += 1
            link["keep_checked"] = k["checked"]
        # keep the Keep line labelled the way Nexus knows it — but only if he hasn't
        # edited the text himself since we last saw it
        if (n_is_open and nid in n_open and not k["checked"]
                and _norm(k["text"]) == _norm(link.get("text") or "")):
            want = keep_text_for(n_open[nid], n_open)
            if _norm(want) != _norm(k["text"]):
                log(f"renamed in Keep: {k['text']!r} -> {want!r}")
                keep.set_text(keep_id, want)
                k["text"] = want
                link["text"] = want
                counts["renamed"] += 1
        # link["text"] is the last text WE wrote/linked, never his edit — that is what
        # makes "he changed it" detectable (and respected) on every later run
        link["nexus_open"] = n_is_open

    # 2) Keep items with no link: unchecked -> into Nexus (match by name first)
    for keep_id, k in k_items.items():
        if keep_id in links or k["checked"] or not _norm(k["text"]):
            continue
        text, cat = categorize(k["text"])
        row = n_open_by_name.get(_norm(text))
        if row and row["id"] not in by_nexus:
            log(f"linked by name: {text!r} = Nexus #{row['id']}")
            nid = row["id"]
            cat = row["category"]
            counts["matched"] += 1
        else:
            log(f"Keep -> Nexus: {text!r} -> {cat}")
            nid = nexus.add(text, cat)
            counts["keep_to_nexus"] += 1
        if cat not in SYNC_CATEGORIES:
            # routed to a phone-invisible category: it lives on Nexus now, so the
            # Keep line comes off (deleted, not ticked) and no link is kept
            log(f"descoped from Keep (category {cat}): {k['text']!r}")
            keep.delete(keep_id)
            counts["descoped"] += 1
            continue
        want = keep_text_for({"item": text, "category": cat}, n_open)
        if _norm(want) != _norm(k["text"]):
            log(f"renamed in Keep: {k['text']!r} -> {want!r}")
            keep.set_text(keep_id, want)
            k["text"] = want
            counts["renamed"] += 1
        links[keep_id] = {"nexus_id": nid, "nexus_open": True, "keep_checked": False, "text": k["text"]}
        if nid is not None:
            by_nexus[nid] = keep_id

    # 3) open Nexus rows nobody in Keep knows about (Aernbot / Nexus adds) -> into Keep
    #    (grocery/house only — pets and business never mirror to the phone)
    for nid, row in n_open.items():
        if nid in by_nexus or row["category"] not in SYNC_CATEGORIES:
            continue
        text = keep_text_for(row, n_open)
        log(f"Nexus -> Keep: {text!r}")
        keep_id = keep.add(text)
        counts["nexus_to_keep"] += 1
        if keep_id is not None:
            links[keep_id] = {"nexus_id": nid, "nexus_open": True, "keep_checked": False, "text": text}
            by_nexus[nid] = keep_id

    keep.commit()
    state["links"] = links
    return counts


def main(argv):
    dry_run = "--dry-run" in argv
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    lines = []
    log = lambda s: lines.append(s)
    state = _load(STATE_PATH, {"links": {}})
    stamp = {"ok": False, "at": now, "dry_run": dry_run, "error": None, "counts": {}, "open": None}
    try:
        keep = KeepList(_read_token(), dry_run=dry_run)
        nexus = Nexus(dry_run=dry_run)
        counts = reconcile(nexus, keep, state, log)
        stamp["counts"] = counts
        stamp["open"] = len(nexus.open_items())
        stamp["ok"] = True
        if not dry_run:
            _save_atomic(STATE_PATH, state)
    except SystemExit as e:
        stamp["error"] = str(e)
    except Exception as e:  # never let a Keep hiccup take the task down silently
        stamp["error"] = f"{type(e).__name__}: {e}"[:300]
    if not dry_run:
        try:
            _save_atomic(STAMP_PATH, stamp)
        except OSError:
            pass
    for l in lines:
        print("  " + l)
    if stamp["ok"]:
        c = stamp["counts"]
        changes = sum(c.values())
        print(f"keep_sync ok{' (dry-run)' if dry_run else ''}: {changes} change(s) "
              f"[keep->nexus {c['keep_to_nexus']}, nexus->keep {c['nexus_to_keep']}, cleared {c['cleared_nexus']}, "
              f"ticked {c['ticked_keep']}, reopened {c['reopened']}, matched {c['matched']}, renamed {c.get('renamed', 0)}, "
              f"descoped {c.get('descoped', 0)}] · {stamp['open']} open")
        return 0
    print(f"keep_sync FAILED: {stamp['error']}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
