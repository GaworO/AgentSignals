"""Read-only trading-terminal UI for the DOL Reversal Manager shadow challenger.

The visual layout intentionally mirrors Downside Manager Shadow.  This module is
presentation/read-only only; it never places, modifies, cancels, or retries an
order.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from flask import abort, jsonify, Response, render_template_string

import dol_reversal_manager_shadow_v1 as shadow

HERE = Path(__file__).resolve().parent
NY = dt.timezone(dt.timedelta(hours=-4))  # display helper only; epoch is authoritative


def _time_et(row):
    ms = row.get("fill_ms") or row.get("entry_anchor_ms") or row.get("bos_ms")
    if ms:
        # Use zoneinfo when available so DST is correct.
        try:
            from zoneinfo import ZoneInfo
            z = ZoneInfo("America/New_York")
        except Exception:
            z = NY
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).astimezone(z).strftime("%Y-%m-%d %H:%M")
    return str(row.get("created_at") or "—")[:16]


def _public(row):
    ctx = row.get("strategy_context") or {}
    sig = row.get("signal") or {}
    done = row.get("control_final_r") is not None and row.get("manager_final_r") is not None
    return {
        "id": row.get("candidate_id"),
        "time_et": _time_et(row),
        "strategy": "DOL Delivery Reversal",
        "session": sig.get("session") or "—",
        "side": "LONG" if row.get("direction") == 1 else "SHORT",
        "entry": row.get("entry"),
        "listed_entry": row.get("entry"),
        "control_r": row.get("control_final_r"),
        "manager_r": row.get("manager_final_r"),
        "delta_r": row.get("delta_r"),
        "last_decision": row.get("recommendation") or "—",
        "status": "REPLAYED" if done else str(row.get("status") or "PENDING"),
        "reason": None if done else str(row.get("status") or "PENDING"),
        "catalyst": ctx.get("catalyst"),
        "dol_tier": ctx.get("dol_tier_class"),
    }


def _rows():
    return shadow.rows() if shadow.ENABLED else []


def _catalog():
    rs = _rows()
    done = [r for r in rs if r.get("control_final_r") is not None and r.get("manager_final_r") is not None]
    return {
        "summary": {
            "sample_label": f"FORWARD SHADOW · {len(done)} COMPLETED MANAGED TRADES",
            "shadow_only": True,
            "broker_execution": False,
        },
        "trades": [_public(r) for r in rs],
    }


def _live_rows():
    out = []
    for r in reversed(_rows()[-300:]):
        decisions = r.get("decisions") or []
        last = decisions[-1] if decisions else {}
        out.append({
            "id": r.get("candidate_id"),
            "source_key": r.get("candidate_id"),
            "strategy_id": "DOL_DELIVERY_REVERSAL",
            "status": r.get("status"),
            "direction": r.get("direction"),
            "entry": r.get("entry"),
            "initial_sl": r.get("initial_sl"),
            "fixed_tp": r.get("fixed_tp"),
            "quantity": r.get("quantity"),
            "fill_ms": r.get("fill_ms"),
            "current_r": last.get("current_r"),
            "mfe_r": last.get("mfe_r"),
            "mae_r": last.get("mae_r"),
            "manager_probability": last.get("probability"),
            "recommendation": r.get("recommendation"),
            "control_final_r": r.get("control_final_r"),
            "manager_final_r": r.get("manager_final_r"),
            "delta_r": r.get("delta_r"),
            "state_quality": r.get("state_quality"),
        })
    return out


def _detail(identifier):
    value = shadow.detail(identifier)
    if value is None:
        abort(404)
    value = dict(value)
    row = next((r for r in _rows() if str(r.get("candidate_id")) == str(identifier)), None)
    value["time_et"] = _time_et(row or value)
    value["actual_broker_usd"] = None
    value["listed_outcome"] = "Forward shadow only"
    value["control_usd"] = None
    value["manager_usd"] = None
    return value


def register(app):
    app.add_url_rule(
        "/dol-reversal-manager/api/trades",
        "dolrev_mgr_replay_list",
        lambda: jsonify(_catalog()),
    )
    app.add_url_rule(
        "/dol-reversal-manager/api/trade/<path:identifier>",
        "dolrev_mgr_replay_one",
        lambda identifier: jsonify(_detail(identifier)),
    )
    app.add_url_rule(
        "/dol-reversal-manager/api/live",
        "dolrev_mgr_live_list",
        lambda: jsonify({"status": shadow.status(), "trades": _live_rows()}),
    )
    app.add_url_rule(
        "/dol-reversal-manager/api/live/<path:identifier>",
        "dolrev_mgr_live_one",
        lambda identifier: jsonify(_detail(identifier)),
    )
    for path, endpoint in (
        ("/dol-reversal-manager/trades", "dolrev_mgr_history"),
        ("/dol-reversal-manager/real-replays", "dolrev_mgr_real_replays"),
        ("/dol-reversal-manager/metrics", "dolrev_mgr_metrics"),
        ("/dol-reversal-manager/trade/<path:identifier>", "dolrev_mgr_trade_page"),
    ):
        app.add_url_rule(path, endpoint, lambda **_: Response(page(), mimetype="text/html"))


def page():
    return render_template_string(
        (HERE / "templates" / "dol_reversal_manager_shadow.html").read_text()
    )
