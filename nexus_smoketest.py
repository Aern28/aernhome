"""
nexus_smoketest.py — fast, READ-ONLY health check for the personal nexus.

Calls each connector and renders every /nexus page through Flask's test client,
asserting 200 internally and 404 via a simulated Cloudflare request. Writes
nothing — safe to run against the live install any time (e.g. after a deploy).

Usage:  py nexus_smoketest.py
Exit 0 = all good, 1 = something failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    import nexus_sources as ns
    import app as A

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    print("== connectors (read-only) ==")
    tcg = ns.tcg_alerts()
    check("tcg_alerts", isinstance(tcg, dict) and "inventory_value" in tcg,
          f"${tcg.get('inventory_value', 0):.0f} · {tcg.get('sales_today', 0)} sales")
    check("todoist_today", isinstance(ns.todoist_today(), list))
    check("currently_reading", isinstance(ns.currently_reading(), list))
    check("infra_summary", isinstance(ns.infra_summary(), dict))
    check("goals_summary", isinstance(ns.goals_summary(), list))
    check("maintenance_due", isinstance(ns.maintenance_due(), list))

    print("== pages (internal -> 200) ==")
    c = A.app.test_client()
    for p in ["/nexus", "/nexus/goals", "/nexus/house", "/nexus/tcg", "/nexus/books", "/nexus/infra"]:
        r = c.get(p)
        check(p, r.status_code == 200, f"HTTP {r.status_code}")

    print("== security (via Cloudflare -> 404) ==")
    for p in ["/nexus", "/nexus/goals"]:
        r = c.get(p, headers={"CF-Connecting-IP": "203.0.113.9"})
        check(f"{p} blocked", r.status_code == 404, f"HTTP {r.status_code}")
    r = c.post("/api/nexus/capture", json={"text": "x"}, headers={"CF-Connecting-IP": "203.0.113.9"})
    check("/api/nexus/capture blocked", r.status_code == 404, f"HTTP {r.status_code}")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
