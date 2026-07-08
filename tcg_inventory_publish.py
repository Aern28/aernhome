"""Publish the live TCGplayer inventory to the nexus visual-inventory page.

Run from any fleet machine that has the newest TCGplayer__MyPricing_*.csv export
(defaults to the current user's Downloads). Builds data/live_inventory.json,
localizes card images from the TCGplayer CDN (only ones not already staged),
and scp's both to the aernhome data dir on Ashaman.

Usage:
    py tcg_inventory_publish.py                      # newest export in ~/Downloads
    py tcg_inventory_publish.py path\\to\\export.csv
    py tcg_inventory_publish.py --no-push            # stage only, skip scp

The export is the source of truth; /nexus/inventory is its mirror. Re-run after
any confirmed listing change (delist upload, staged->live push, restock).
"""
import csv
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ASHAMAN_DATA = "ashaman:C:/projects/aernhome/data"
STAGE = os.path.join(os.environ.get("TEMP", "/tmp"), "tcg_inventory_publish")
CDN = "https://product-images.tcgplayer.com/fit-in/437x437/{pid}.jpg"
TCGCSV = "https://tcgcsv.com/tcgplayer"

# The pricing export's "TCGplayer Id" is a SKU id; the image CDN keys on
# PRODUCT id. tcgcsv (same TCGplayer catalog) maps (set, name) -> productId.
# Export "Product Line" -> tcgcsv categoryId; unlisted lines resolve by name.
CATEGORY_HINTS = {"Magic": 1, "Pokemon": 3}


UA = {"User-Agent": "aernhome-inventory-publish/1.0"}


def _urlopen(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)


def _get_json(url, cache_name):
    os.makedirs(os.path.join(STAGE, "tcgcsv_cache"), exist_ok=True)
    cache = os.path.join(STAGE, "tcgcsv_cache", cache_name)
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
        with io.open(cache, encoding="utf-8") as f:
            return json.load(f)
    with _urlopen(url) as r:
        data = json.load(r)
    with io.open(cache, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def build_product_map(items):
    """(line, set, name-lower) -> productId via tcgcsv. Misses are skipped —
    the page hides images it can't serve."""
    cats = {c["name"].lower(): c["categoryId"]
            for c in _get_json(f"{TCGCSV}/categories", "categories.json")["results"]}

    def cat_id(line):
        if line in CATEGORY_HINTS:
            return CATEGORY_HINTS[line]
        ll = line.lower()
        for name, cid in cats.items():
            if name == ll or ll in name or name in ll:
                return cid
        return None

    pmap = {}
    for (line, set_name) in sorted({(i["line"], i["set"]) for i in items}):
        cid = cat_id(line)
        if cid is None:
            continue
        groups = _get_json(f"{TCGCSV}/{cid}/groups", f"groups_{cid}.json")["results"]
        gid = next((g["groupId"] for g in groups if g["name"] == set_name), None)
        if gid is None:
            continue
        prods = _get_json(f"{TCGCSV}/{cid}/{gid}/products", f"products_{cid}_{gid}.json")["results"]
        for p in prods:
            pmap[(line, set_name, p["name"].strip().lower())] = p["productId"]
    return pmap


def newest_export():
    home = os.path.expanduser("~")
    paths = glob.glob(os.path.join(home, "Downloads", "TCGplayer__MyPricing_*.csv"))
    if not paths:
        sys.exit("no TCGplayer__MyPricing_*.csv in Downloads - pass a path")
    return max(paths, key=os.path.getmtime)


def money(s):
    s = (s or "").replace("$", "").replace(",", "").strip()
    return float(s) if s else 0.0


def numkey(number):
    m = re.search(r"(\d+)", number or "")
    return (int(m.group(1)) if m else 0, number or "")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    push = "--no-push" not in sys.argv
    export = args[0] if args else newest_export()

    items = []
    with io.open(export, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            qty = int(r["Total Quantity"] or 0)
            if qty <= 0:
                continue
            items.append({
                "id": int(r["TCGplayer Id"]),
                "line": r["Product Line"].strip(),
                "set": r["Set Name"].strip(),
                "name": r["Product Name"].strip(),
                "number": r["Number"].strip(),
                "condition": r["Condition"].strip(),
                "qty": qty,
                "price": money(r["TCG Marketplace Price"]),
            })
    items.sort(key=lambda i: (i["line"], i["set"], numkey(i["number"])))

    snapshot = {
        "source": os.path.basename(export),
        "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(export))),
        "totals": {
            "skus": len(items),
            "cards": sum(i["qty"] for i in items),
            "value": round(sum(i["price"] * i["qty"] for i in items), 2),
        },
        "items": items,
    }

    img_dir = os.path.join(STAGE, "card_images")
    os.makedirs(img_dir, exist_ok=True)
    json_path = os.path.join(STAGE, "live_inventory.json")
    with io.open(json_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f)

    pmap = build_product_map(items)
    fetched = failed = unmapped = 0
    for it in items:
        dest = os.path.join(img_dir, f"{it['id']}.jpg")
        if os.path.exists(dest):
            continue
        pid = pmap.get((it["line"], it["set"], it["name"].lower()))
        if pid is None:
            unmapped += 1
            print(f"  no catalog match: {it['set']}: {it['name']}")
            continue
        try:
            with _urlopen(CDN.format(pid=pid)) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            fetched += 1
            time.sleep(0.2)  # be polite to the CDN
        except Exception as e:
            failed += 1
            print(f"  image {it['id']} ({it['name']}): {type(e).__name__}")

    print(f"snapshot: {snapshot['totals']['skus']} SKUs, {snapshot['totals']['cards']} cards, "
          f"${snapshot['totals']['value']:.2f} | images: {fetched} fetched, {failed} failed, "
          f"{unmapped} unmapped (missing images just don't render)")

    if not push:
        print(f"staged only: {STAGE}")
        return
    subprocess.run(["scp", "-o", "ConnectTimeout=30", json_path, ASHAMAN_DATA + "/"], check=True)
    subprocess.run(["scp", "-o", "ConnectTimeout=30", "-r", img_dir, ASHAMAN_DATA + "/"], check=True)
    print("pushed to Ashaman - /nexus/inventory is current")


if __name__ == "__main__":
    main()
