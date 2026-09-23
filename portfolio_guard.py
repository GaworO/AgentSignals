"""Read-only portfolio decision audit for the existing per-account Guard.

This module records what the production Guard actually decided.  It does not
arbitrate strategies, submit orders, or promote shadow signals to live trades.
"""

import datetime as dt
import hashlib
import html
import json
import os
import time


def _path(data_dir=None):
    # Resolve at call time so each isolated account service uses its own DATA_DIR.
    return os.path.join(data_dir or os.environ.get("DATA_DIR", "."), "guard_decisions.jsonl")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _explanation(decision, reason, strategy):
    """Decision-time text only; the UI never invokes this function."""
    if decision == "sent":
        return (strategy + " passed the existing account Guard; the execution relay "
                "accepted the submitted order. Broker fill is not implied.")
    if decision == "manual":
        return strategy + " was retained for manual review. No broker order was submitted."
    if reason == "duplicate":
        return strategy + " was blocked because this physical setup was already recorded by the account Guard."
    if reason == "group_pending":
        return strategy + " was blocked because this account already had a pending setup-group reservation."
    if reason == "position_open":
        return strategy + " was blocked because this account already had an open position."
    if reason.startswith("session:"):
        return strategy + " was blocked by the account session rule (" + reason + ")."
    return strategy + " was not submitted. The account Guard recorded reason code: " + (reason or "unknown") + "."


def record_note(candidate, decision, reason, account, mode, candidate_id=None, data_dir=None):
    """Append one immutable audit event after the legacy Guard note is persisted.

    Unknown data stays null; this is not a retrospective competition decision.
    """
    now_ms = int(time.time() * 1000)
    strategy = str(candidate.get("_strat") or "A/B")
    entry, sl, tp = candidate.get("entry"), candidate.get("SL"), candidate.get("_exec_tp", candidate.get("TP"))
    try:
        risk_points = abs(float(entry) - float(sl))
    except (TypeError, ValueError):
        risk_points = None
    qty = candidate.get("_sent_qty", candidate.get("_exec_qty_override"))
    try:
        risk_usd = risk_points * 2.0 * int(qty) if risk_points is not None and qty is not None else None
    except (TypeError, ValueError):
        risk_usd = None
    status_by_reason = {
        "duplicate": "BLOCKED_DUPLICATE", "group_pending": "BLOCKED_PENDING_ORDER",
        "position_open": "BLOCKED_POSITION_OPEN", "day_loss_n": "BLOCKED_DAILY_LOSS",
        "day_loss_usd": "BLOCKED_DAILY_LOSS", "loss_streak_n": "BLOCKED_COOLDOWN",
        "stale_data": "BLOCKED_STALE_STATE", "equity_stale": "BLOCKED_STALE_STATE",
        "news_window": "BLOCKED_NEWS", "news_cal_stale": "BLOCKED_NEWS",
        "dd_proximity": "BLOCKED_RISK_LIMIT", "projected_dd_risk": "BLOCKED_RISK_LIMIT",
        "sl_too_tight": "BLOCKED_RISK_LIMIT", "late_day": "BLOCKED_SESSION",
    }
    status = ("SELECTED" if decision == "sent" else
              "BLOCKED_SESSION" if str(reason).startswith("session:") else
              status_by_reason.get(reason, "BLOCKED"))
    event = {
        "schema_version": 1,
        "decision_timestamp": dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).isoformat(),
        "account": account,
        "mode": mode.upper(),
        "broker_mode": "LIVE_RELAY" if decision == "sent" else "NO_BROKER_SEND",
        "candidate_ids": [str(candidate_id or candidate.get("candidate_id") or candidate.get("_setup_group_id") or "unavailable")],
        "strategy": strategy,
        "direction": candidate.get("dir"),
        "entry": entry, "sl": sl, "tp": tp,
        "risk_points": risk_points, "risk_usd": risk_usd,
        "requested_quantity": candidate.get("_group_qty_cap", candidate.get("_exec_qty_override")),
        "submitted_quantity": candidate.get("_sent_qty"),
        "session": candidate.get("sess"),
        "dol_eligibility": None, "manager_eligibility": None,
        "pending_or_open_before": None,
        "guard_checks": [{"name": "legacy_guard_final_result", "passed": decision == "sent",
                          "reason_code": reason or "ok"}],
        "guard_checks_complete": False,
        "priority": None,
        "status": status,
        "selected_candidate_id": (str(candidate_id or candidate.get("candidate_id") or candidate.get("_setup_group_id"))
                                  if decision == "sent" else None),
        "rejected_candidate_ids": ([str(candidate_id or candidate.get("candidate_id") or candidate.get("_setup_group_id"))]
                                   if decision == "blocked" else []),
        "reason_code": reason or ("relay_accepted" if decision == "sent" else "unknown"),
        "explanation": _explanation(decision, str(reason or ""), strategy),
        "portfolio_state_before": None, "portfolio_state_after": None,
        "reservation_status": "not_recorded",
        "order_submission_status": "RELAY_ACCEPTED" if decision == "sent" else "NOT_SUBMITTED",
        "guard_decision_latency_ms": None,
        "competition_captured": False,
        "evidence_limit": "Legacy Guard evaluated this account's A/B setup; Continuation and DOL Reversal shadow were not competing broker candidates.",
    }
    event["decision_hash"] = hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()
    path = _path(data_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # O_APPEND gives each event its own append operation. The Guard's existing
    # execution path does not wait for dashboard rendering or network fetches.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_canonical(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def read_records(data_dir=None):
    try:
        with open(_path(data_dir), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []
    return rows


def _filtered(rows, args):
    fields = ("strategy", "status", "reason_code", "mode", "account", "session", "direction")
    for field in fields:
        wanted = (args.get(field) or "").strip()
        if wanted:
            rows = [r for r in rows if str(r.get(field) or "") == wanted]
    date = (args.get("date") or "").strip()
    if date:
        rows = [r for r in rows if str(r.get("decision_timestamp") or "").startswith(date)]
    return rows


def _page(rows):
    esc = lambda value: html.escape(str(value if value is not None else "not recorded"))
    trs = []
    for row in reversed(rows[-200:]):
        details = {
            "entry": row.get("entry"), "sl": row.get("sl"), "tp": row.get("tp"),
            "risk_points": row.get("risk_points"), "risk_usd": row.get("risk_usd"),
            "requested_quantity": row.get("requested_quantity"),
            "submitted_quantity": row.get("submitted_quantity"),
            "session": row.get("session"), "dol_eligibility": row.get("dol_eligibility"),
            "manager_eligibility": row.get("manager_eligibility"),
            "pending_or_open_before": row.get("pending_or_open_before"),
            "guard_checks": row.get("guard_checks"),
            "guard_checks_complete": row.get("guard_checks_complete"),
            "selected_candidate_id": row.get("selected_candidate_id"),
            "rejected_candidate_ids": row.get("rejected_candidate_ids"),
            "reservation_status": row.get("reservation_status"),
            "order_submission_status": row.get("order_submission_status"),
            "guard_decision_latency_ms": row.get("guard_decision_latency_ms"),
            "decision_hash": row.get("decision_hash"),
        }
        trs.append("<tr><td>" + esc(row.get("decision_timestamp")) + "</td><td>" +
                   esc(row.get("account")) + "</td><td>" + esc(row.get("candidate_ids")) +
                   "</td><td>" + esc(row.get("strategy")) + "</td><td>" +
                   esc(row.get("direction")) + "</td><td>" + esc(row.get("status")) +
                   "</td><td>" + esc(row.get("reason_code")) + "</td><td>" +
                   esc(row.get("explanation")) + "</td><td><details><summary>Evidence</summary><pre>" +
                   esc(json.dumps(details, ensure_ascii=False, indent=2)) + "</pre></details></td></tr>")
    return ("""<!doctype html><html lang="en"><meta charset="utf-8"><title>Portfolio Guard</title>
<style>body{background:#0b0e14;color:#e6e9ef;font:14px system-ui;margin:24px}h1{margin:0 0 8px}
p{color:#9aa3b5;line-height:1.5}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:9px;border:1px solid #283347;vertical-align:top}th{background:#152033}
input{background:#152033;color:#fff;border:1px solid #33415a;padding:8px;margin:3px;width:135px}
button{background:#143d25;color:#8de6ad;border:1px solid #28844a;padding:8px;cursor:pointer}</style>
<h1>Portfolio Guard</h1><p>Only decisions actually persisted by this account's existing A/B Guard are shown.
Historical guard_log rows have no decision-time explanation and are not backfilled.
Continuation and DOL Reversal currently run as post-decision shadows, so no cross-strategy priority comparison is claimed.</p>
<form><input type="date" name="date" aria-label="Date"><input name="strategy" placeholder="Strategy">
<input name="status" placeholder="Status"><input name="reason_code" placeholder="Reason code">
<input name="mode" placeholder="SHADOW/LIVE mode"><input name="account" placeholder="Account">
<input name="session" placeholder="Session"><input name="direction" placeholder="Direction">
<button>Filter</button></form><p>Guard checks, portfolio before/after, collision timeline and latency are not
available from the existing legacy Guard. They are intentionally not reconstructed.</p>
<table><thead><tr><th>Decision time UTC</th><th>Account</th><th>Candidate IDs</th><th>Strategy</th>
<th>Direction</th><th>Guard status</th><th>Reason code</th><th>Persisted explanation</th><th>Evidence</th></tr></thead><tbody>""" +
        "".join(trs) + "</tbody></table></html>")


def register(app, data_dir=None):
    from flask import Response, jsonify, request

    def page():
        return Response(_page(_filtered(read_records(data_dir), request.args)), mimetype="text/html")

    def data():
        return jsonify(records=_filtered(read_records(data_dir), request.args))

    app.add_url_rule("/portfolio-guard", "portfolio_guard_page", page)
    app.add_url_rule("/portfolio-guard/data", "portfolio_guard_data", data)
    return app
