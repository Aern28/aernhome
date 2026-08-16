"""One-shot: seed the restock list with Aern's 2026-08-14 house-walk sample.
Idempotent (add_restock de-dupes open items). Run inside aernhome-dashboard:
    docker exec aernhome-dashboard python seed_restock.py
"""
import nexus_writes as ns

SEED = {
    "rowan": ["litter", "wet food", "dry food", "NexGard+"],
    "jace": ["yogurt", "Ollie", "dry food", "preventatives"],
    "house": ["paper towels", "Celsius", "toothpaste", "toothbrush", "shaver cleaner",
              "milk", "DevaCurl", "TP"],
    "business": ["sleeves", "toploaders", "shipping shields", "packing slip paper"],
}

if __name__ == "__main__":
    n = 0
    for cat, items in SEED.items():
        for it in items:
            ns.add_restock(it, cat, "seed-2026-08-14")
            n += 1
    print(f"seeded {n} items; open now: {len(ns.list_restock())}")
