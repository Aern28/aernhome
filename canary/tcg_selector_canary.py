"""
tcg_selector_canary.py — nightly "selector canary" for the tcgsales-automation
pipeline (TCGplayer + LetterTrackPro + PirateShip + TCGCSV). Probes the sites
that pipeline depends on and flags markup/selector drift BEFORE the
unattended run hits it mid-sale.

RUNTIME ENVIRONMENT
--------------------
Run with the SAME Python the tcgsales-automation scheduled task uses. That
project has no dedicated venv (confirmed: no venv/.venv dir in the repo, and
run_unattended.ps1:47-48 just does `Set-Location $PSScriptRoot; py main.py
--unattended`) — it relies on the Windows `py` launcher's default
interpreter, which already has playwright + requests installed (that's the
whole reason this canary can reuse it instead of provisioning its own env).
Invoke this script the same way: `py tcg_selector_canary.py`.

SELECTOR PROVENANCE (mined from tcgsales-automation in review/, 2026-07-05)
----------------------------------------------------------------------------
TCGplayer seller portal (tcgplayer_automator.py):
  - line 30/34: `playwright.chromium.connect_over_cdp('http://127.0.0.1:9222')`
    then `browser.contexts[0]` — the CDP connection pattern, copied exactly
    into _connect_cdp() below.
  - line 43: `'sellerportal.tcgplayer.com/orders/' in page.url` — the order
    URL contract _get_or_create_page() relies on.
  - line 64: order URL fallback `https://store.tcgplayer.com/admin/orders/
    manageorder/{order_id}`, which the CLAUDE.md "Discovered Info" section
    notes redirects to `sellerportal.tcgplayer.com/orders/{ORDER_ID}`.
  - line 71: `'login' in self.page.url.lower() or 'oauth' in self.page.url
    .lower()` — the auth-redirect detection this canary reuses verbatim.
  - line 80: `page.wait_for_selector('text=Shipping Address', ...)`
  - line 114: `page.locator('text=Shipping Address').first.locator('xpath=../..')`
  - line 175: `page.locator('button:has-text("Packing Slip")').first`
  - line 181: `page.locator('button:has-text("Print Default")').first`
    NOTE on coverage gap: lines 80/114/175/181 are all per-ORDER-DETAIL-page
    selectors. main.py never scrapes a standalone orders list/search page —
    it jumps straight from a Gmail-derived order id to the detail URL. This
    canary intentionally does NOT open a specific order's detail page: doing
    so requires a real order id (the canary has no safe/credential-free way
    to obtain one) and would mean navigating into live customer data, which
    conflicts with "never touch a live order" read-only intent. Instead it
    opens the orders LIST page (`/orders`) — the closest safe, id-less
    equivalent — and asserts the auth/URL contract (lines 43/64/71 above)
    plus generic portal-shell anchors. If the per-order field selectors
    (Shipping Address / Packing Slip / Print Default) ever drift, this
    canary will NOT catch it directly; it would need a designated always-safe
    test order id (env-provided) to close that gap — noted as a follow-up.

LetterTrackPro (lettertrack_automator.py):
  - line 19: `LETTERTRACK_URL = "https://www.lettertrackpro.com/"`
  - line 71: `input[name="Username"]`
  - line 72: `input[name="Password"]`
  - line 73: `input[value="Login"]`
  Confirmed live 2026-07-05 via plain `requests.get`: it's a classic .asp-era
  server-rendered page (no auth needed to see the login form), so a raw HTML
  regex match on these three attributes is a faithful, credential-free proxy
  for "the login form still renders the fields main.py's login() fills."

PirateShip (pirateship_automator.py):
  - line 39: `PIRATESHIP_URL = "https://ship.pirateship.com/ship/single"`
  - line 86: `'login' in self.page.url.lower() or 'sign' in self.page.url
    .lower()` — not-logged-in detection, reused verbatim.
  - line 165: `input[name="shipToAddress.fullName"]` — the first field
    _fill_address() fills; a stable, load-bearing anchor for "the address
    form still renders with the field names main.py depends on."
  Confirmed live 2026-07-05 via plain `requests.get` on both
  https://ship.pirateship.com/login (404) and /ship/single (200, but the
  raw HTML is a pure React shell: one `id="root"` div, zero occurrences of
  "email"/"password"/"login"/"<form"). Per the task's documented fallback
  for JS-heavy SPAs, this probe is routed through the CDP-connected,
  already-authenticated Chrome as a second read-only tab instead of a plain
  HTTP GET — it actually gets to assert a real load-bearing selector this
  way, which the plain-HTTP LetterTrackPro-style approach could not.

TCGCSV (tcg-inventory-tool/fetch_tcgcsv.py):
  - line 27: `BASE_URL = "https://tcgcsv.com/tcgplayer"`
  - line 58: `f"{BASE_URL}/categories"`
  - lines 48-52: browser-like User-Agent/Accept/Referer headers required.
  Confirmed live 2026-07-05: 200 JSON, `{"success": true, "results": [...],
  "totalItems": 90, ...}`.

READ-ONLY GUARANTEES
---------------------
- Every probe is wrapped so it can never raise out of main() (a crash inside
  one probe degrades ONLY that probe to "unknown", per fleet.py's
  run_all_checks / fleet_host_stats.ps1's Invoke-CheckSafely convention).
- The CDP-based probes open exactly one new tab each, navigate exactly once
  (with one retry inside a 60s wall-clock budget), read-only locator
  queries only (.count()/.url/.title() — never .click()/.fill()/.check()),
  and close the tab in a `finally` block.
- If CDP isn't reachable, both CDP-based checks report status "unknown",
  detail "chrome session down" — the fleet's separate chrome_cdp check
  (host_collector/fleet_host_stats.ps1) already owns alerting on that
  condition, so this canary deliberately doesn't duplicate it as its own
  down/warn.
- Never logs page content, HTML, or credentials. Only which selector
  hit/missed and short structural details (counts, URLs, HTTP status).
- No credentials are read, stored, or required anywhere in this script.
"""

import datetime as dt
import json
import os
import re
import tempfile
import time

import requests

# ── Config ─────────────────────────────────────────────────────────────────
OUT_PATH = os.environ.get(
    "TCG_CANARY_OUT", r"C:\projects\aernhome\data\canary.json"
)
CDP_URL = "http://127.0.0.1:9222"  # same endpoint tcgsales-automation connects to
HTTP_TIMEOUT_S = 15
CDP_HARD_TIMEOUT_S = 60  # wall-clock budget per CDP-based probe, incl. its 1 retry

TCGPLAYER_ORDERS_URL = "https://sellerportal.tcgplayer.com/orders"
PIRATESHIP_SHIP_URL = "https://ship.pirateship.com/ship/single"  # pirateship_automator.py:39
LETTERTRACK_URL = "https://www.lettertrackpro.com/"  # lettertrack_automator.py:19
TCGCSV_URL = "https://tcgcsv.com/tcgplayer/categories"  # fetch_tcgcsv.py:27,58
TCGCSV_HEADERS = {  # fetch_tcgcsv.py:48-52 — TCGCSV requires browser-like headers
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://tcgcsv.com/",
}


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _run_check(check_id, label, fn):
    """Run one probe, guaranteeing a well-formed result dict even if fn
    raises something its own try/except didn't anticipate. Mirrors
    fleet.py's run_all_checks / fleet_host_stats.ps1's Invoke-CheckSafely."""
    try:
        status, detail = fn()
    except Exception as e:
        status, detail = "unknown", f"probe crashed: {e}"[:300]
    return {"id": check_id, "label": label, "status": status, "detail": detail}


# ── LetterTrackPro (plain HTTP, no auth) ────────────────────────────────────
def probe_lettertrack():
    try:
        resp = requests.get(LETTERTRACK_URL, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return ("down", f"request failed: {e}"[:200])

    if resp.status_code != 200:
        return ("down", f"HTTP {resp.status_code}")

    html = resp.text
    selectors = {
        'input[name="Username"]': r'name=["\']Username["\']',
        'input[name="Password"]': r'name=["\']Password["\']',
        'input[value="Login"]': r'value=["\']Login["\']',
    }
    missing = [sel for sel, pat in selectors.items() if not re.search(pat, html, re.I)]
    if missing:
        return ("down", "login form selector(s) missing: " + ", ".join(missing))
    return ("up", "login form fields present (Username/Password/Login)")


# ── TCGCSV (plain HTTP, no auth) ────────────────────────────────────────────
def probe_tcgcsv():
    try:
        resp = requests.get(TCGCSV_URL, headers=TCGCSV_HEADERS, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return ("down", f"request failed: {e}"[:200])

    if resp.status_code != 200:
        return ("down", f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        return ("down", "response was not valid JSON")

    if not isinstance(data, dict) or not data.get("success"):
        return ("warn", f"unexpected response shape: {str(data)[:150]}")

    results = data.get("results")
    if not isinstance(results, list) or not results:
        return ("warn", "'results' missing or empty in TCGCSV response")

    return ("up", f"{len(results)} categories returned")


# ── TCGplayer seller portal + PirateShip (via CDP, one shared session) ─────
def _connect_cdp(playwright):
    """Exact pattern copied from tcgplayer_automator.connect_to_chrome /
    lettertrack_automator / pirateship_automator / main.check_tracking()."""
    browser = playwright.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    return browser, context


def _goto_with_retry(page, url, hard_timeout_s=CDP_HARD_TIMEOUT_S):
    """Navigate with exactly one retry inside a hard wall-clock budget.
    domcontentloaded is required to succeed; a subsequent networkidle wait
    is best-effort (some SPAs never go fully idle, e.g. polling/analytics)."""
    deadline = time.monotonic() + hard_timeout_s
    last_err = None
    for _attempt in (1, 2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_err or TimeoutError("hard timeout exceeded before navigation attempt")
        try:
            page.goto(url, timeout=max(1000, min(30000, remaining * 1000)), wait_until="domcontentloaded")
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    page.wait_for_load_state("networkidle", timeout=max(1000, remaining * 1000))
                except Exception:
                    pass  # best-effort only
            return
        except Exception as e:
            last_err = e
    raise last_err


def _probe_tcgplayer_orders_page(context):
    page = None
    try:
        page = context.new_page()
        _goto_with_retry(page, TCGPLAYER_ORDERS_URL)

        url = page.url.lower()
        if "login" in url or "oauth" in url:  # tcgplayer_automator.py:71
            return ("down", "redirected to login — Chrome session not authenticated to TCGplayer")
        if "sellerportal.tcgplayer.com" not in url:  # tcgplayer_automator.py:43
            return ("warn", f"unexpected URL after load: {page.url}"[:200])

        title_ok = False
        try:
            title_ok = "tcgplayer" in (page.title() or "").lower()
        except Exception:
            pass
        if not title_ok:
            return ("warn", "page loaded but title doesn't mention TCGplayer — portal shell may have changed")

        # Read-only count of order-row links matching the /orders/{id} URL
        # contract (tcgplayer_automator.py:43/64) — does NOT navigate to any.
        try:
            order_links = page.locator('a[href*="/orders/"]').count()
        except Exception:
            order_links = -1

        detail = "portal loaded, authenticated, title OK"
        if order_links >= 0:
            detail += f"; {order_links} order-row link(s) matching /orders/ pattern"
        return ("up", detail)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def _probe_pirateship(context):
    page = None
    try:
        page = context.new_page()
        _goto_with_retry(page, PIRATESHIP_SHIP_URL)

        url = page.url.lower()
        if "login" in url or "sign" in url:  # pirateship_automator.py:86
            return ("down", "redirected to login/sign-in — Chrome session not authenticated to PirateShip")

        try:
            field_present = page.locator('input[name="shipToAddress.fullName"]').count() > 0
        except Exception:
            field_present = False

        if field_present:
            return ("up", "ship/single form rendered, shipToAddress.fullName field present")
        return ("warn", "logged in but shipToAddress.fullName field not found — form markup may have changed")
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def probe_tcgplayer_and_pirateship():
    """Both CDP-based probes share one Playwright/browser session so a dead
    Chrome session degrades both to the same unknown/"chrome session down"
    result instead of two independent (and differently-worded) failures.
    Returns (tcgplayer_result, pirateship_result), each an (status, detail)
    tuple. Never raises."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        unknown = ("unknown", f"playwright not available: {e}"[:200])
        return unknown, unknown

    try:
        with sync_playwright() as p:
            # 9/05: the 06:30 run failed as "chrome session down" while the same
            # connect succeeded by hand at 19:45 and the host /json/version answered
            # all day. The 08:10 'TCG Delivery Check' saw the same shape (websocket
            # connected, then a 180 s protocol timeout). Record the real exception
            # and retry once after a pause so the morning failure explains itself.
            last_exc = None
            for attempt in (1, 2):
                try:
                    browser, context = _connect_cdp(p)
                    last_exc = None
                    break
                except Exception as e:
                    last_exc = e
                    if attempt == 1:
                        time.sleep(20)
            if last_exc is not None:
                reason = f"{type(last_exc).__name__}: {str(last_exc).splitlines()[0]}"[:160]
                unknown = ("unknown", f"chrome session down after 2 attempts ({reason})")
                return unknown, unknown

            try:
                tcg_result = _probe_tcgplayer_orders_page(context)
            except Exception as e:
                tcg_result = ("unknown", f"probe crashed: {e}"[:300])

            try:
                ps_result = _probe_pirateship(context)
            except Exception as e:
                ps_result = ("unknown", f"probe crashed: {e}"[:300])

            return tcg_result, ps_result
    except Exception as e:
        unknown = ("unknown", f"playwright session crashed: {e}"[:300])
        return unknown, unknown


# ── Output (atomic write) ───────────────────────────────────────────────────
def atomic_write_json(path, payload):
    """temp file + os.replace, same convention as fleet.py's
    save_state_atomic and fleet_host_stats.ps1's temp+Move-Item — a reader
    (fleet.py's canary check) never observes a partially-written file."""
    out_dir = os.path.dirname(path) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".canary_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def main():
    checks = [
        _run_check("lettertrack_login", "LetterTrackPro Login Page", probe_lettertrack),
        _run_check("tcgcsv_categories", "TCGCSV Categories Endpoint", probe_tcgcsv),
    ]

    try:
        tcg_result, ps_result = probe_tcgplayer_and_pirateship()
    except Exception as e:  # belt-and-suspenders: probe_tcgplayer_and_pirateship already never raises
        tcg_result = ps_result = ("unknown", f"probe crashed: {e}"[:300])

    checks.append({
        "id": "tcgplayer_orders_portal",
        "label": "TCGplayer Seller Portal (Orders)",
        "status": tcg_result[0],
        "detail": tcg_result[1],
    })
    checks.append({
        "id": "pirateship_ship_form",
        "label": "PirateShip Ship Form",
        "status": ps_result[0],
        "detail": ps_result[1],
    })

    payload = {"generated_at": _now_iso(), "checks": checks}

    try:
        atomic_write_json(OUT_PATH, payload)
    except Exception as e:
        print(f"[canary] FAILED to write {OUT_PATH}: {e}")

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
