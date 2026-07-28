# Adding a trip

1. Copy this `_template/` folder to `travel/<slug>/` (slug = `kc-aug-2026` style).
2. Fill in `trip.json` — `start` (ISO date) drives sort order on /nexus/travel; `dates` is the display string; `artifact` (optional) links the claude.ai itinerary page.
3. Build `index.html` — a fully self-contained page (inline CSS, QR codes as data: URIs so they render with no network dependencies). The KC trip (`kc-aug-2026/`) is the reference shape: travel best-practices up top, key locations with Google Maps links, then day-by-day itinerary with embedded docs.
4. Drop any extra assets (PDFs, images) in the folder — they serve at `/travel/<slug>/<filename>`.
5. Deploy: commit + push, then on Ashaman `git pull` + `docker restart aernhome-dashboard`.

House rule: the hub is never the ONLY copy of a scan-at-gate item. Nexus is
Tailscale-only — a dead zone at a turnstile means no ticket. Everything scannable
also goes in Apple Wallet or a screenshot before the trip.

Folders starting with `_` are ignored by the index.
