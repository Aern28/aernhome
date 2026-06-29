"""
seed_pto_doc.py — seed the "PTO Cap Watch 2026" reference doc into the Nexus Docs
section (nexus.db). Idempotent: skips if the slug already exists, so it's safe to
re-run and won't clobber later edits made in the UI.

Run wherever DATA_DIR points at the live nexus.db:
    # locally (NenTera dev copy)
    py seed_pto_doc.py
    # on Ashaman, inside the container after a rebuild
    docker exec aernhome-dashboard python seed_pto_doc.py
"""
import nexus_writes as w

SLUG = "pto-cap-watch-2026"

BODY = r"""
**Snapshot:** balance **250 / cap ~280 h** · accrual **+28 h/mo** · **8 h** per PTO day.
Source: QGenda → Google Calendar (events titled "PTO"). Re-derive when balance/schedule changes.

## Bottom line

> ⚠️ **Caps out in August** and forfeits **~90 h (≈11 working days)** of earned PTO by year-end if nothing more is booked. Only ~30 h of headroom against a 28 h/mo accrual.

To *stop the bleed* you need ~**3.5 days off / month**; currently ~1.

## 6-month projection (accrue +28, burn scheduled PTO, cap at 280)

| Month | Start | +Accrue | −PTO | Raw | Capped | Forfeited |
|-------|------:|--------:|-----:|----:|-------:|----------:|
| July | 250 | +28 | 0 | 278 | 278 | 0 |
| August | 278 | +28 | −16 (2d) | 290 | **280** | **10** |
| September | 280 | +28 | −8 (1d) | 300 | **280** | **20** |
| October | 280 | +28 | −8 (1d) | 300 | **280** | **20** |
| November | 280 | +28 | 0 | 308 | **280** | **28** |
| December | 280 | +28 | −16 (2d) | 292 | **280** | **12** |

Existing PTO on the books: **Aug 6–7 · Sep 21 · Dec 29–30** (6 days total).

## Candidate days to add

Shift difficulty (lightest → hardest to drop): **Education** (no panel, nothing to arrange) → **Clinic** (needs closure rules / lead time) → **Gyn BU** (needs coverage arranged) → **L&D / Post-Call** (leave alone).

### Tier A — real multi-day breaks (need closure *or* coverage)

| Day | Shift | Buys | Must arrange |
|-----|-------|------|--------------|
| **Fri Sep 18** | PL Clinic | 4-day w/ existing Sep 21 PTO | Clinic closure |
| **Fri Aug 28** | PL Clinic | 4-day (Aug 29–31 already clear) | Clinic closure |
| **Mon Jul 27** | Gyn BU | ~5-day off CME + No-Call (Jul 23–27) | Gyn coverage |
| **Fri Jul 17** | PL Clinic | 3-day (Fri–Sun) | Clinic closure |
| **Fri Jul 10** | PL Clinic | 3-day (Fri–Sun) | Clinic closure |
| **Mon Jul 13** | Gyn BU | 3-day (Sat–Mon) | Gyn coverage |

### Tier B — zero-friction hour-burners (pure Education days)

Nothing to close, nobody to cover — just miss didactics. Cheapest way to drain hours:

**Thu Jul 16 · Thu Jul 30 · Thu Aug 20 · Thu Sep 3 · Thu Sep 10 · Thu Sep 17 · Thu Sep 24**

## Next actions

1. Lock 2 Education Thursdays now (e.g. **Jul 16 + Jul 30**) — no arranging, immediate cap relief.
2. Start closure paperwork on **Sep 18** and **Aug 28** (the two 4-day weekends) — they need the most lead time.
3. **Jul 27** as a stretch goal — biggest single block (~5 days), gated on finding gyn backup.

*Booking 6 of these (~48 h) cuts forfeiture from ~90 h to ~20–30 h, and every July day delays the cap.*
""".strip()


def main():
    if w.get_doc(SLUG):
        print(f"doc '{SLUG}' already exists — skipping (edit in the UI instead)")
        return
    res = w.add_doc("PTO Cap Watch 2026", BODY, area="work")
    w.set_doc_pinned(res["id"], True)
    print(f"seeded doc id={res['id']} slug={res['slug']} (pinned)")


if __name__ == "__main__":
    main()
