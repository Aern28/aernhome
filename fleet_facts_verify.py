"""fleet_facts_verify.py - mechanically re-verify fleet facts (2026-07-14).

Runs on the Ashaman HOST (py fleet_facts_verify.py). For every fact carrying
a `check`, executes a WHITELISTED check type and stamps verified_at +
verify_ok in data/fleet_facts.json. Facts with no check are left untouched
(they age visibly via their source date instead).

SECURITY: check types are a closed whitelist with validated parameters and
arg-array subprocess calls (never shell=True). No free-form command type
exists ON PURPOSE - the facts store is POST-able by fleet members including
the sandboxed relay container, and a cmd-bearing fact would otherwise be a
container-to-host escalation path. Do not add a generic exec type.

Checks marked "soft": failure records verify_ok=false but is expected
sometimes (e.g. Trainer ollama during game-mode) - reporters should not alarm.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FACTS = os.path.join(HERE, "data", "fleet_facts.json")

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_PATH = re.compile(r"^[A-Za-z]:\\[^|&;<>*?\"]*$|^[A-Za-z]:\\$")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_http(p):
    url = p.get("url", "")
    if not url.startswith(("http://127.0.0.1", "http://100.", "http://localhost")):
        return None, "url outside allowlist"
    expect = p.get("expect", [200])
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status in expect, "http %s" % r.status
    except urllib.error.HTTPError as e:
        return e.code in expect, "http %s" % e.code
    except Exception as e:
        return False, str(e)[:80]


def check_file(p):
    path = p.get("path", "")
    if not SAFE_PATH.match(path):
        return None, "path failed validation"
    return os.path.exists(path), "exists" if os.path.exists(path) else "missing"


def check_docker_volume(p):
    name = p.get("name", "")
    if not SAFE_NAME.match(name):
        return None, "name failed validation"
    r = subprocess.run(["docker", "volume", "inspect", name],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0, "present" if r.returncode == 0 else "absent"


def check_cmdkey_target(p):
    target = p.get("target", "")
    if not SAFE_NAME.match(target.replace(".", "")) and not re.match(r"^[0-9.]+$", target):
        return None, "target failed validation"
    r = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True, timeout=30)
    ok = target in (r.stdout or "")
    return ok, "stored" if ok else "no credential for target"


CHECKS = {
    "http": check_http,
    "file": check_file,
    "docker_volume": check_docker_volume,
    "cmdkey_target": check_cmdkey_target,
}


def main():
    with open(FACTS, "r", encoding="utf-8") as f:
        doc = json.load(f)
    ran = passed = failed = soft_failed = 0
    for fact in doc.get("facts", []):
        chk = fact.get("check")
        if not isinstance(chk, dict):
            continue
        fn = CHECKS.get(chk.get("type"))
        if fn is None:
            print("SKIP %-38s unknown check type %r" % (fact.get("id"), chk.get("type")))
            continue
        ok, detail = fn(chk)
        if ok is None:
            print("SKIP %-38s %s" % (fact.get("id"), detail))
            continue
        ran += 1
        fact["verified_at"] = _now()
        fact["verify_ok"] = bool(ok)
        if ok:
            passed += 1
            print("OK   %-38s %s" % (fact.get("id"), detail))
        elif chk.get("soft"):
            soft_failed += 1
            print("soft %-38s %s (expected sometimes)" % (fact.get("id"), detail))
        else:
            failed += 1
            print("FAIL %-38s %s" % (fact.get("id"), detail))
    doc["updated_at"] = _now()
    doc["updated_by"] = "fleet_facts_verify"
    tmp = FACTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FACTS)
    print("--- %d checks: %d ok, %d FAILED, %d soft-failed ---" % (ran, passed, failed, soft_failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
