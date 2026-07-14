"""Fleet Facts - the machine-verifiable truth store (2026-07-14).

The third leg of the memory-keeper stack: thread-note = where Matt is,
commitments = what can't slip, fleet facts = what's true about the machines.

Every fact carries value/source/verified_at, and optionally a `check` -- a
mechanical verification recipe (see fleet_facts_verify.py) so facts get
re-verified on a schedule instead of trusted until they bite. Facts whose
checks fail get `verify_ok: false` and surface on the Fleet board.

Owned by Ashaman (always-on services box). Phoenix keeps master-gist prose
and doctrine; this store holds the verifiable substrate it can cite.

API (nexus-gated like the seat):
  GET  /api/facts            -> full doc
  GET  /api/facts?k=<prefix> -> facts whose id starts with prefix
  POST /api/facts            -> upsert {"fact": {...}} or {"facts": [...]}
                                (id required; updated_by recorded)
"""

import json
import os
import threading

from flask import Blueprint, jsonify, request, abort

from second_brain import _is_nexus_allowed, _now_iso, DATA_DIR

facts_bp = Blueprint("facts", __name__)

FACTS_PATH = os.path.join(DATA_DIR, "fleet_facts.json")
_LOCK = threading.Lock()

FACT_FIELDS = ("id", "value", "source", "verified_at", "verify_ok", "check", "note", "updated_by")


def load_facts():
    try:
        with open(FACTS_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("facts"), list):
            return doc
    except (OSError, ValueError):
        pass
    return {"updated_at": None, "updated_by": None, "facts": []}


def save_facts_atomic(doc):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FACTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FACTS_PATH)


def _clean_fact(raw, existing=None):
    existing = existing or {}
    fact = {}
    for k in FACT_FIELDS:
        v = raw.get(k, existing.get(k))
        if v is not None:
            fact[k] = v
    return fact if fact.get("id") and ("value" in fact) else None


@facts_bp.route("/api/facts")
def api_facts_get():
    if not _is_nexus_allowed():
        abort(404)
    doc = load_facts()
    prefix = request.args.get("k")
    if prefix:
        doc = dict(doc, facts=[f for f in doc["facts"] if str(f.get("id", "")).startswith(prefix)])
    return jsonify(doc)


@facts_bp.route("/api/facts", methods=["POST"])
def api_facts_post():
    if not _is_nexus_allowed():
        abort(404)
    body = request.get_json(silent=True) or {}
    incoming = body.get("facts") if isinstance(body.get("facts"), list) else None
    if incoming is None and isinstance(body.get("fact"), dict):
        incoming = [body["fact"]]
    if not incoming:
        return jsonify({"ok": False, "error": "body must include 'fact' or 'facts'"}), 400

    with _LOCK:
        doc = load_facts()
        by_id = {f.get("id"): i for i, f in enumerate(doc["facts"])}
        applied = 0
        for raw in incoming:
            if not isinstance(raw, dict):
                continue
            idx = by_id.get(raw.get("id"))
            existing = doc["facts"][idx] if idx is not None else None
            cleaned = _clean_fact(raw, existing)
            if cleaned is None:
                continue
            cleaned["updated_by"] = body.get("updated_by") or raw.get("updated_by") or "unknown"
            if idx is not None:
                doc["facts"][idx] = cleaned
            else:
                doc["facts"].append(cleaned)
            applied += 1
        doc["updated_at"] = _now_iso()
        doc["updated_by"] = body.get("updated_by") or "unknown"
        save_facts_atomic(doc)
    return jsonify({"ok": True, "applied": applied, "total": len(doc["facts"])})
