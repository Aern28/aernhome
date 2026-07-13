"""Manually push the Family Week Agenda from Notion to the TRMNL device NOW,
replicating the n8n workflow's transform exactly (same merge_variables shape).
Normal cadence is n8n's 15-min schedule — this is for test/instant refreshes.

Usage (inside the aernhome-dashboard container, NOTION_TOKEN in env):
    python push_family_agenda_once.py
"""
import datetime
import json
import os
from zoneinfo import ZoneInfo

from sync_family_agenda import NOTION_FAMILY_AGENDA_DB, _api, _plain, _token

TRMNL_PLUGIN_UUID = os.environ.get("TRMNL_FAMILY_AGENDA_UUID",
                                   "7cc25d15-44e3-4a65-8ff0-37e588c23013")


def _sel(page, name):
    prop = page["properties"].get(name) or {}
    return (prop.get("select") or {}).get("name", "") if prop.get("select") is not None else ""


def _rich(page, name):
    return _plain((page["properties"].get(name) or {}).get("rich_text", []))


def main():
    token = _token()
    data = _api(f"https://api.notion.com/v1/databases/{NOTION_FAMILY_AGENDA_DB}/query",
                token, "POST",
                {"sorts": [{"property": "Sort Order", "direction": "ascending"}]})

    days = []
    for page in data.get("results", []):
        days.append({
            "day": _plain(page["properties"].get("Day", {}).get("title", [])),
            "gal_am": _sel(page, "Gal AM"),
            "gal_pm": _sel(page, "Gal PM"),
            "matt_am": _sel(page, "Matt AM"),
            "matt_pm": _sel(page, "Matt PM"),
            "jaina_drop": _sel(page, "Jaina Drop"),
            "jaina_pick": _sel(page, "Jaina Pick"),
            "meal": _rich(page, "Meal"),
            "meal_type": _sel(page, "Meal Type"),
            "notes": _rich(page, "Notes"),
        })

    tz = ZoneInfo("America/Chicago")
    now = datetime.datetime.now(tz)
    monday = now.date() - datetime.timedelta(days=now.weekday())
    sunday = monday + datetime.timedelta(days=6)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    week_label = f"{months[monday.month - 1]} {monday.day}–{sunday.day}"

    payload = {"merge_variables": {
        "days": days,
        "week_label": week_label,
        "today": now.strftime("%A"),
        "updated": now.strftime("%H:%M"),
    }}

    from urllib.request import Request, urlopen
    req = Request(f"https://trmnl.com/api/custom_plugins/{TRMNL_PLUGIN_UUID}",
                  data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json",
                           # bare Python-urllib UA gets Cloudflare-403'd; any browser-ish UA passes
                           "User-Agent": "Mozilla/5.0 (aernhome family-agenda push)"},
                  method="POST")
    with urlopen(req, timeout=30) as resp:
        print(f"TRMNL push: HTTP {resp.status} | {len(days)} days | "
              f"today={payload['merge_variables']['today']} "
              f"updated={payload['merge_variables']['updated']}")


if __name__ == "__main__":
    main()
