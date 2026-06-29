"""
nexus_notion_import.py — pull a Notion database into Nexus Docs.

Two modes:
    py nexus_notion_import.py --inspect <db_id>     # schema + row count + titles, NO writes
    py nexus_notion_import.py --import  <db_id>     # each Notion page -> a Nexus Doc

Auth: NOTION_TOKEN env var, else Bitwarden item "Notion API Token" (needs BW_SESSION;
get it via `. C:\\tools\\bw-session.ps1` first). The integration must have the target
database shared with it, or Notion returns 404.
"""
import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

NOTION_VERSION = "2022-06-28"
BW = r"C:\tools\bw.exe"


def _token():
    t = os.environ.get("NOTION_TOKEN")
    if t:
        return t.strip()
    session = os.environ.get("BW_SESSION", "")
    if session:
        try:
            r = subprocess.run([BW, "get", "notes", "Notion API Token", "--session", session],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return r.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    sys.exit("No Notion token (set NOTION_TOKEN or BW_SESSION).")


def _hdrs(token):
    return {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}


def _api(url, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers=_hdrs(token), method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        sys.exit(f"Notion API {e.code} on {url}\n{e.read().decode(errors='replace')[:500]}")
    except (URLError, TimeoutError) as e:
        sys.exit(f"Notion API unreachable: {e}")


def _norm_id(s):
    s = s.strip().split("?")[0].rstrip("/").split("/")[-1].replace("-", "")
    if len(s) != 32:
        sys.exit(f"Bad Notion id: {s!r}")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _plain(rich):
    return "".join(r.get("plain_text", "") for r in (rich or []))


def _title_of(props):
    for name, val in props.items():
        if val.get("type") == "title":
            return _plain(val.get("title")), name
    return "(untitled)", None


def _query_all(db_id, token):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = _api(f"https://api.notion.com/v1/databases/{db_id}/query", token, "POST", body)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def inspect(db_id, token):
    db = _api(f"https://api.notion.com/v1/databases/{db_id}", token)
    title = _plain(db.get("title")) or "(untitled db)"
    props = db.get("properties", {})
    rows = _query_all(db_id, token)
    print(f"DB: {title}")
    print(f"   id: {db_id}")
    print(f"   properties ({len(props)}): " +
          ", ".join(f"{n} [{p.get('type')}]" for n, p in props.items()))
    print(f"   rows: {len(rows)}")
    print("   --- titles ---")
    for r in rows[:60]:
        t, _ = _title_of(r.get("properties", {}))
        print(f"     • {t}")
    if len(rows) > 60:
        print(f"     … +{len(rows) - 60} more")


def _prop(props, ptype):
    """First property value of a given type: url string or list of multi_select names."""
    for val in props.values():
        if val.get("type") == ptype:
            if ptype == "url":
                return val.get("url")
            if ptype == "multi_select":
                return [o.get("name") for o in val.get("multi_select", [])]
    return None


def _blocks_md(page_id, token):
    """Fetch a page's child blocks and convert the common ones to markdown."""
    data = _api(f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100", token)
    lines = []
    for b in data.get("results", []):
        bt = b.get("type")
        node = b.get(bt, {})
        txt = _plain(node.get("rich_text")) if isinstance(node, dict) else ""
        if bt == "paragraph":
            lines.append(txt)
        elif bt == "heading_1":
            lines.append(f"# {txt}")
        elif bt == "heading_2":
            lines.append(f"## {txt}")
        elif bt == "heading_3":
            lines.append(f"### {txt}")
        elif bt == "bulleted_list_item":
            lines.append(f"- {txt}")
        elif bt == "numbered_list_item":
            lines.append(f"1. {txt}")
        elif bt == "to_do":
            done = "x" if node.get("checked") else " "
            lines.append(f"- [{done}] {txt}")
        elif bt == "quote":
            lines.append(f"> {txt}")
        elif bt == "code":
            lines.append(f"```\n{txt}\n```")
        elif bt == "divider":
            lines.append("---")
        elif txt:
            lines.append(txt)
        if bt in ("paragraph", "heading_1", "heading_2", "heading_3", "quote", "code", "divider"):
            lines.append("")  # blank line after block-level elements
    return "\n".join(lines).strip()


def do_import(db_id, token, coll_tag=None):
    import app
    app.init_nexus_db()  # ensure the docs table (incl. tags) exists
    import nexus_writes as w

    db = _api(f"https://api.notion.com/v1/databases/{db_id}", token)
    db_title = _plain(db.get("title")) or "notion"
    coll = (coll_tag or db_title).strip().lower().rstrip("s") or "imported"
    rows = _query_all(db_id, token)
    created = skipped = 0
    for r in rows:
        props = r.get("properties", {})
        title, _ = _title_of(props)
        if title == "(untitled)" or w.doc_title_exists(title):
            skipped += 1
            print(f"   skip: {title}")
            continue
        link = _prop(props, "url")
        ntags = _prop(props, "multi_select") or []
        body = _blocks_md(r["id"], token)
        parts = []
        if link:
            parts.append(f"🔗 [{link}]({link})")
        if body:
            parts.append(body)
        if not parts:
            parts.append(f"_Imported from Notion “{db_title}”._")
        tags = [coll] + ntags
        res = w.add_doc(title, "\n\n".join(parts), tags=tags)
        created += 1
        print(f"   + {title}  [{','.join(t.lower() for t in tags)}]  -> {res['slug']}")
    print(f"done: {created} created, {skipped} skipped (collection tag: '{coll}')")


def peek(db_id, token):
    """Dump the first row's body block types — to gauge how much content to migrate."""
    rows = _query_all(db_id, token)
    if not rows:
        print("no rows"); return
    pid = rows[0]["id"]
    t, _ = _title_of(rows[0].get("properties", {}))
    print(f"first page: {t}  ({pid})")
    data = _api(f"https://api.notion.com/v1/blocks/{pid}/children?page_size=50", token)
    blocks = data.get("results", [])
    print(f"   body blocks: {len(blocks)}")
    for b in blocks[:25]:
        bt = b.get("type")
        txt = _plain(b.get(bt, {}).get("rich_text")) if isinstance(b.get(bt), dict) else ""
        print(f"     [{bt}] {txt[:80]}")


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("--inspect", "--peek", "--import"):
        sys.exit(__doc__)
    mode, db_id = args[0], _norm_id(args[1])
    token = _token()
    if mode == "--inspect":
        inspect(db_id, token)
    elif mode == "--peek":
        peek(db_id, token)
    else:
        coll = args[2] if len(args) > 2 else None  # optional collection-tag override
        do_import(db_id, token, coll)


if __name__ == "__main__":
    main()
