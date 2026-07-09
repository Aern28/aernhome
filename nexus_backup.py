"""
nexus_backup.py — durable, rotating backup of nexus.db (the personal nexus's
canonical writable store) to the NAS.

Uses the SQLite online backup API, so it is safe to run against the live DB while
the Flask app is using it (no need to stop the container). Writes a dated snapshot
and keeps the most recent KEEP files. Run on the Ashaman host (where the /data
volume lives), e.g. as a daily scheduled task.

Usage:
    py nexus_backup.py                 # snapshot -> NEXUS_BACKUP_DIR, prune to KEEP
Env:
    DATA_DIR           source dir holding nexus.db (default C:/projects/aernhome/data)
    NEXUS_BACKUP_DIR   backup target (default H:/aernhome/backups)
    NEXUS_BACKUP_KEEP  how many snapshots to retain (default 14)
"""
import os
import sys
import glob
import sqlite3
from datetime import datetime

SRC = os.path.join(os.environ.get("DATA_DIR", "C:/projects/aernhome/data"), "nexus.db")
# UNC default: mapped drives (H:) only exist in interactive logon sessions, so the
# scheduled task silently failed whenever it ran elsewhere (found 2026-07-05 by the
# Fleet board). UNC + a cmdkey-stored credential works from any session.
DEST_DIR = os.environ.get("NEXUS_BACKUP_DIR", r"\\192.168.1.118\home\aernhome\backups")
KEEP = int(os.environ.get("NEXUS_BACKUP_KEEP", "14"))


def backup(stamp):
    if not os.path.exists(SRC):
        print(f"[skip] no nexus.db at {SRC}")
        return None
    os.makedirs(DEST_DIR, exist_ok=True)
    dest = os.path.join(DEST_DIR, f"nexus-{stamp}.db")
    src = sqlite3.connect(SRC)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)          # online backup — safe on a live DB
        finally:
            dst.close()
    finally:
        src.close()
    # integrity-gate the copy before trusting it
    chk = sqlite3.connect(dest)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        os.remove(dest)
        print(f"[fail] integrity_check={ok} — backup discarded")
        return None
    return dest


def prune():
    snaps = sorted(glob.glob(os.path.join(DEST_DIR, "nexus-*.db")))
    for old in snaps[:-KEEP] if len(snaps) > KEEP else []:
        try:
            os.remove(old)
        except OSError:
            pass


def backup_ssh(stamp):
    """Fallback transport: snapshot locally, scp via the 'synology' ssh alias.
    UNC + cmdkey can't work from non-interactive sessions (Credential Manager
    is unreachable there — proven 2026-07-09); key-based scp works from ANY
    session, and the AernHome NAS Stats task proves the alias works scheduled.
    Remote dir is the same physical folder the UNC path pointed at."""
    import subprocess
    staging = os.path.join(os.environ.get("TEMP", "."), f"nexus-{stamp}.db")
    src = sqlite3.connect(SRC)
    try:
        dst = sqlite3.connect(staging)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    chk = sqlite3.connect(staging)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        os.remove(staging)
        print(f"[fail] integrity_check={ok} — ssh backup discarded")
        return None
    remote = "synology:aernhome/backups/"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                        "synology", "mkdir -p aernhome/backups"],
                       capture_output=True, text=True, timeout=30)
    r = subprocess.run(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                        staging, remote], capture_output=True, text=True, timeout=120)
    os.remove(staging)
    if r.returncode != 0:
        print(f"[fail] scp: {r.stderr.strip()}")
        return None
    # prune remote to KEEP via the same alias (script-owned rotation)
    subprocess.run(["ssh", "-o", "BatchMode=yes", "synology",
                    f"cd aernhome/backups && ls -1t nexus-*.db | tail -n +{KEEP + 1} | xargs -r rm --"],
                   capture_output=True, text=True, timeout=30)
    return remote + f"nexus-{stamp}.db"


def main():
    # Date.now() is fine here — this is a plain script, not a workflow.
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    try:
        dest = backup(stamp)
    except OSError as e:
        print(f"[warn] UNC transport failed ({e}); trying ssh fallback")
        dest = None
    if dest:
        prune()
        size_kb = os.path.getsize(dest) // 1024
        print(f"[ok] nexus.db -> {dest} ({size_kb} KB); keeping last {KEEP}")
        return 0
    dest = backup_ssh(stamp)
    if dest:
        print(f"[ok] nexus.db -> {dest} (ssh transport); keeping last {KEEP}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
