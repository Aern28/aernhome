# created-by: opus
# created: 2026-09-03
# purpose: watch goobuegaming@gmail.com for distributor/bank/county replies and file them to the Nexus Aern queue
# lifespan: infrastructure
# project: tcg-distribution
"""Aernbot's eye on the BUSINESS mailbox.

goobuegaming@gmail.com is a separate Google account the Claude Gmail connector cannot
read. Until 2026-09-14 this filed header-only one-liners for a sender watch list; on
2026-09-14 Aern granted full read of the mailbox ("check QID") so it now files EVERY
inbound mail of the last 14 days (minus SKIP noise senders and his own sent mail), each
with a ~600-char body excerpt, to the Nexus `to_aern` queue - which already mirrors to
Todoist and shows on /nexus/aern and in the morning briefing. The task runs QID (every 6h).
The WATCH list is kept only to label known senders.

READ-ONLY by construction: opens the mailbox with readonly=True, so nothing is marked
seen, moved, or deleted, and it never sends. Seen message-ids are remembered in
biz_mail_seen.json so a reply is filed once, not every run.

Credential: a Google App Password for goobuegaming (NOT the account password) in
data/goobue_app_password.txt - same unattended-credential pattern as keep_token.txt.
Requires 2-Step Verification on that account to create one.
"""
import email
import imaplib
import json
import os
import re
import sys
import urllib.request
from email.header import decode_header, make_header

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PW_FILE = os.path.join(DATA, "goobue_app_password.txt")
STATE = os.path.join(DATA, "biz_mail_seen.json")
STAMP = os.path.join(DATA, "biz_mail_last.json")
ACCOUNT = os.getenv("BIZ_MAIL_ACCOUNT", "goobuegaming@gmail.com")
# Tailscale IP, NOT the LAN one: Nexus deliberately does not publish :5555 on the
# LAN (only :5556, for the TRMNL device). The LAN address here was refused on
# 2026-09-03 and cost this watcher its first filing.
NEXUS = os.getenv("NEXUS_URL", "http://100.110.245.37:5555")

# Senders worth waking Aern for. Everything else in the mailbox is ignored entirely.
WATCH = [
    ("acdd.com",            "ACD Distribution (wholesale application, submitted 9/03)"),
    ("unityb2b.com",        "Unity Trading (wholesale application, submitted 9/03)"),
    ("unitytradingllc.com", "Unity Trading"),
    ("bushiroad.com",       "Bushiroad"),
    ("southernhobby.com",   "Southern Hobby"),
    ("gtsdistribution.com", "GTS Distribution"),
    ("phdgames.com",        "PHD Games"),
    ("bankofamerica.com",   "Bank of America (reference letter for ACD)"),
    ("vanguard.com",        "Vanguard (bank reference)"),
    ("cclerk.hctx.net",     "Harris County Clerk (assumed name filing)"),
    ("hctx.net",            "Harris County"),
]


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _hdr(msg, name):
    raw = msg.get(name, "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


SKIP = [                     # noise senders - never filed
    "facebookmail.com",
    "mail.instagram.com",
]
EXCERPT_CHARS = 600


def _excerpt(msg):
    """First EXCERPT_CHARS of the message as plain text (html stripped, quoted replies cut)."""
    import html as _html
    parts = []
    for p in msg.walk():
        ct = p.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            body = p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/html":
            body = re.sub(r"<(script|style).*?</\1>", "", body, flags=re.S | re.I)
            body = re.sub(r"<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", "\n", body, flags=re.I)
            body = re.sub(r"<[^>]+>", "", body)
            body = _html.unescape(body)
        body = re.sub(r"[ \t\xa0|]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n", body).strip()
        # cut quoted history ("On ... wrote:") so a reply excerpt is the reply, not the thread
        body = re.split(r"\nOn .{5,80} wrote:", body, maxsplit=1)[0]
        parts.append(body)
    if not parts:
        return "(no text body)"
    t = max(parts, key=len)
    return t[:EXCERPT_CHARS] + (" ..." if len(t) > EXCERPT_CHARS else "")


def _queue(text, source):
    body = json.dumps({"dir": "to_aern", "text": text, "source": source,
                       "created_by": "aernbot-bizmail", "effort": "read",
                       "priority": 1}).encode()
    req = urllib.request.Request(NEXUS + "/api/queue", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("id")


def main():
    if not os.path.isfile(PW_FILE):
        # Not an error yet - Aern hasn't created the app password. Stamp it so the
        # fleet check can say "waiting on credential" instead of "broken".
        _save(STAMP, {"ok": False, "reason": "no app password file yet", "filed": 0})
        print("WAITING: no", PW_FILE)
        return 0

    with open(PW_FILE, encoding="utf-8") as f:
        pw = f.read().strip()

    seen = set(_load(STATE, {"ids": []}).get("ids", []))
    # Gmail server-side search: EVERY inbound mail of the last 14 days (Aern granted full read
    # of this mailbox on 2026-09-14: "check QID"), minus the SKIP senders and his own sent mail.
    skips = " ".join("-from:" + d for d in SKIP)
    query = f"newer_than:14d -from:{ACCOUNT} {skips}".strip()

    filed, matched = 0, 0
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(ACCOUNT, pw)
    except (imaplib.IMAP4.error, OSError) as e:
        # Stamp the REAL reason. Crashing here used to leave the previous stamp in
        # place, so the fleet reported "no app password yet" while the truth was an
        # app password Google refuses (revoked, or minted for a different account).
        _save(STAMP, {"ok": False, "reason": f"IMAP login failed: {e}", "filed": 0,
                      "account": ACCOUNT})
        print(f"LOGIN FAILED for {ACCOUNT}: {e}")
        return 1

    try:
        M.select('"[Gmail]/All Mail"', readonly=True)   # readonly: never marks seen
        typ, data = M.search(None, "X-GM-RAW", f'"{query}"')
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        for mid in ids:
            typ, raw = M.fetch(mid, "(BODY.PEEK[HEADER])")   # PEEK: no \Seen flag
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1])
            gid = _hdr(msg, "Message-ID") or mid.decode()
            matched += 1
            if gid in seen:
                continue
            sender = _hdr(msg, "From")
            subject = _hdr(msg, "Subject") or "(no subject)"
            date = _hdr(msg, "Date")
            who = next((label for d, label in WATCH if d in sender.lower()), "unlisted sender")
            # Body excerpt for NEW mail only (one extra PEEK fetch; still never marks read).
            excerpt = ""
            try:
                typ2, raw2 = M.fetch(mid, "(BODY.PEEK[])")
                if typ2 == "OK" and raw2 and isinstance(raw2[0], tuple):
                    excerpt = _excerpt(email.message_from_bytes(raw2[0][1]))
            except Exception as e:
                excerpt = f"(body fetch failed: {e})"
            text = (f"BUSINESS MAILBOX ({ACCOUNT}) - {who}. "
                    f"From: {sender} | Subject: {subject} | {date}.\n"
                    f"{excerpt}")
            try:
                _queue(text, f"biz_mail_watch.py / {ACCOUNT}")
                seen.add(gid)
                filed += 1
            except Exception as e:
                print(f"queue POST failed for {gid}: {e}")
    finally:
        try:
            M.logout()
        except Exception:
            pass

    _save(STATE, {"ids": sorted(seen)[-500:]})
    _save(STAMP, {"ok": True, "matched": matched, "filed": filed, "watching": len(WATCH)})
    print(f"OK: {matched} matching message(s), {filed} newly filed to the Aern queue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
