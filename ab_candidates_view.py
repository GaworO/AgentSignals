"""Combined A/B and A/B-shallow candidate funnel.

The detector owns the A/B stages.  A/B-shallow is not a second detector: it is
derived from a confirmed A/B setup, so this view deliberately shows it waiting
behind the parent setup until the normal leg is ready.
"""
from __future__ import annotations

from collections import Counter
import html
import os
from typing import Any, Iterable, Mapping

import ab_shallow


def _candidate_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("trig_ms") or 0),
        str(row.get("model") or ""),
        str(row.get("cat") or ""),
        str(row.get("dir") or ""),
    )


def _terminal_stage(stages: Iterable[str]) -> tuple[str, int, str]:
    """Return (state, current step, human-readable reason) for the A/B leg."""
    items = list(stages)
    if "POTWIERDZONY" in items:
        return "confirmed", 4, "A/B order levels are ready"
    cap = next((x for x in reversed(items) if x.startswith("odciety cap")), None)
    if cap:
        return "failed", 3, "Stop was wider than the configured risk cap"
    if "brak wejscia" in items:
        return "failed", 3, "No valid entry level could be calculated"
    if "setup OK (BOS)" in items:
        return "waiting", 3, "BOS passed; waiting for entry and risk validation"
    if "brak setupu (odbicie/BOS)" in items:
        return "failed", 2, "The 50% FVG hold/rejection or BOS did not pass"
    return "waiting", 2, "Displacement found; waiting for the 50% hold and BOS"


def _ab_steps(state: str, current: int) -> list[dict[str, str]]:
    labels = [
        "Displacement + FVG found",
        "50% FVG hold + BOS",
        "Entry + stop/risk checks",
        "A/B confirmed",
    ]
    out = []
    for number, label in enumerate(labels, 1):
        if number < current:
            status = "done"
        elif number == current:
            status = "done" if state == "confirmed" else state
        else:
            status = "blocked" if state == "failed" else "pending"
        out.append({"number": str(number), "label": label, "status": status})
    return out


def _shallow_preview(candidate: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    parent_confirmed = candidate.get("ab_state") == "confirmed"
    if not parent_confirmed:
        failed = candidate.get("ab_state") == "failed"
        state = "blocked" if failed else "waiting"
        reason = "A/B did not confirm, so no Shallow sibling is created" if failed else "Waiting for A/B confirmation"
        current = 1
        child = None
    elif not ab_shallow.enabled(env):
        state, current, reason, child = "disabled", 2, "AB_SHALLOW_ENABLED is off", None
    else:
        required = ("entry", "SL", "signal_close")
        missing = [key for key in required if candidate.get(key) in (None, "")]
        if missing:
            state, current, child = "unavailable", 2, None
            reason = "Detector trace is missing: " + ", ".join(missing)
        else:
            signal = dict(candidate)
            signal["_strat"] = "A/B"
            signal["_signal_close"] = float(candidate["signal_close"])
            try:
                child = ab_shallow.build_shallow_signal(signal, env)
                state, current, reason = "ready", 4, "Shallow limit and shared-risk checks passed"
            except Exception as exc:
                child = None
                state, current = "failed", 3
                reason = str(exc)

    labels = [
        "Wait for A/B confirmation",
        "Calculate Shallow limit",
        "Validate stop + shared budget",
        "Shallow sibling ready",
    ]
    steps = []
    for number, label in enumerate(labels, 1):
        if number < current:
            status = "done"
        elif number == current:
            status = "done" if state == "ready" else state
        else:
            status = "blocked" if state in ("blocked", "failed", "disabled", "unavailable") else "pending"
        steps.append({"number": str(number), "label": label, "status": status})

    return {
        "state": state,
        "reason": reason,
        "steps": steps,
        "entry": child.get("entry") if child else None,
        "SL": child.get("SL") if child else None,
        "TP": child.get("TP") if child else None,
        "risk": child.get("risk") if child else None,
        "budget": child.get("_risk_budget_usd") if child else None,
    }


def build_candidates(trace: Iterable[Mapping[str, Any]], env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Collapse repeated detector trace rows into one two-leg candidate record."""
    effective_env = os.environ if env is None else env
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in trace:
        row = dict(raw)
        key = _candidate_key(row)
        item = grouped.setdefault(key, {"stages": []})
        stage = str(row.get("stage") or "")
        if stage and stage not in item["stages"]:
            item["stages"].append(stage)
        item.update({k: v for k, v in row.items() if k != "stage"})

    result = []
    for item in grouped.values():
        state, current, reason = _terminal_stage(item["stages"])
        item["ab_state"] = state
        item["ab_reason"] = reason
        item["ab_steps"] = _ab_steps(state, current)
        item["shallow"] = _shallow_preview(item, effective_env)
        result.append(item)
    result.sort(key=lambda x: int(x.get("trig_ms") or 0), reverse=True)
    return result


def payload(trace: Iterable[Mapping[str, Any]], hours: float, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    candidates = build_candidates(trace, env)
    return {
        "hours": hours,
        "count": len(candidates),
        "summary": {
            "ab": dict(Counter(x["ab_state"] for x in candidates)),
            "shallow": dict(Counter(x["shallow"]["state"] for x in candidates)),
        },
        "candidates": candidates,
    }


def _esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def _levels(data: Mapping[str, Any]) -> str:
    values = [("Entry", data.get("entry")), ("SL", data.get("SL")), ("TP", data.get("TP"))]
    if data.get("budget") is not None:
        values.append(("Risk budget", f"${float(data['budget']):.2f}"))
    return "".join(f"<span><b>{label}</b> {_esc(value)}</span>" for label, value in values)


def _pipeline(steps: Iterable[Mapping[str, str]]) -> str:
    return "<div class='pipeline'>" + "".join(
        "<div class='step %s'><i>%s</i><span>%s</span></div>" %
        (_esc(step["status"]), _esc(step["number"]), _esc(step["label"]))
        for step in steps
    ) + "</div>"


def render_page(
    trace: Iterable[Mapping[str, Any]],
    hours: float,
    env: Mapping[str, str] | None = None,
    live_status: Mapping[str, Any] | None = None,
) -> str:
    data = payload(trace, hours, env)
    candidates = data["candidates"]
    summary = data["summary"]
    live = dict(live_status or {})
    try:
        refresh_seconds = max(5, min(60, int(live.get("refresh_seconds") or 15)))
    except Exception:
        refresh_seconds = 15
    age = live.get("age_sec")
    is_stale = age is None or float(age) > 120
    live_label = "WAITING FOR LIVE TRACE" if age is None else ("STALE" if is_stale else "LIVE")
    live_class = "stale" if is_stale else "ok"
    cards = []
    for item in candidates:
        shallow = item["shallow"]
        direction = str(item.get("dir") or "").upper()
        cards.append(
            "<article class='candidate'>"
            "<div class='candidate-head'><div><span class='dir %s'>%s</span> <b>%s</b> · %s</div>"
            "<time>%s</time></div>" %
            (_esc(direction.lower()), _esc(direction), _esc(item.get("cat")), _esc(item.get("model")), _esc(item.get("trig"))) +
            "<section><div class='leg-head'><h2>A/B Strategy</h2><span class='state %s'>%s</span></div>" %
            (_esc(item["ab_state"]), _esc(item["ab_state"])) +
            _pipeline(item["ab_steps"]) +
            "<div class='why'>%s</div><div class='levels'>%s</div></section>" %
            (_esc(item["ab_reason"]), _levels(item)) +
            "<section><div class='leg-head'><h2>A/B Shallow</h2><span class='state %s'>%s</span></div>" %
            (_esc(shallow["state"]), _esc(shallow["state"])) +
            _pipeline(shallow["steps"]) +
            "<div class='why'>%s</div><div class='levels'>%s</div></section></article>" %
            (_esc(shallow["reason"]), _levels(shallow))
        )

    empty = "<div class='empty'>No A/B candidates in this time window.</div>"
    ab_confirmed = summary["ab"].get("confirmed", 0)
    shallow_ready = summary["shallow"].get("ready", 0)
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="%d">
<title>A/B + Shallow candidates</title><style>
*{box-sizing:border-box}body{margin:0;background:#0b0e14;color:#e6e9ef;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;padding:20px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}h1{font-size:22px;margin:0 0 4px}.sub,.why{color:#8d98aa}.window a{color:#8ab4f8;text-decoration:none;margin-left:8px;padding:4px 8px;border:1px solid #26334a;border-radius:6px}.livebar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0;padding:9px 12px;background:#101621;border:1px solid #202a3b;border-radius:10px;color:#9ca7b8;font-size:12px}.livebar b{color:#e6e9ef}.liveflag{display:inline-flex;align-items:center;gap:6px;font-weight:800}.liveflag:before{content:'';width:8px;height:8px;border-radius:999px;background:#f59e0b}.liveflag.ok{color:#5ee492}.liveflag.ok:before{background:#42d982;box-shadow:0 0 0 4px #42d98222}.liveflag.stale{color:#f0bb55}.manual{margin-left:auto;color:#8ab4f8;text-decoration:none}.stats{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.stat,.legend,.candidate{background:#101621;border:1px solid #202a3b;border-radius:12px}.stat{padding:10px 14px;min-width:145px}.stat b{display:block;font-size:20px}.stat span{color:#8d98aa;font-size:12px}.legend{padding:14px 16px;margin-bottom:14px}.legend h2{margin:0 0 8px;font-size:15px}.legend-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 18px}.legend-item{color:#aab3c2;font-size:12.5px}.legend-item b{color:#fff}.note{margin-top:9px;color:#f1c66d;font-size:12px}.candidate{margin:12px 0;overflow:hidden}.candidate-head{display:flex;justify-content:space-between;gap:12px;padding:12px 15px;background:#121a28;border-bottom:1px solid #202a3b}.candidate-head time{color:#8d98aa;font-size:12px}.dir{font-weight:800}.dir.long{color:#4ade80}.dir.short{color:#f87171}section{padding:13px 15px;border-bottom:1px solid #202a3b}section:last-child{border:0}.leg-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.leg-head h2{font-size:15px;margin:0}.state{font-size:11px;text-transform:uppercase;font-weight:800;border-radius:999px;padding:3px 8px;background:#202a3b}.state.confirmed,.state.ready{background:#153c28;color:#66e39a}.state.failed,.state.blocked{background:#431e25;color:#ff8d99}.state.waiting{background:#453813;color:#f7cc62}.state.disabled,.state.unavailable{background:#283140;color:#abb5c5}.pipeline{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:8px}.step{display:flex;align-items:center;gap:7px;padding:8px;border:1px solid #283247;border-radius:8px;color:#7f8a9d;font-size:12px}.step i{display:grid;place-items:center;width:21px;height:21px;border-radius:999px;background:#273044;color:#c0c8d5;font-style:normal;font-weight:800;flex:0 0 auto}.step.done{border-color:#1d6c42;color:#b9e8cc}.step.done i{background:#1d6c42;color:#fff}.step.waiting{border-color:#8b6a19;color:#f2cf7a}.step.failed,.step.blocked{border-color:#65303a;color:#d99aa1}.step.failed i,.step.blocked i{background:#5a2630}.step.disabled,.step.unavailable{opacity:.7}.why{font-size:12px;margin-top:9px}.levels{display:flex;gap:14px;flex-wrap:wrap;margin-top:7px;font-variant-numeric:tabular-nums;font-size:12px}.levels span{color:#aab3c2}.levels b{color:#e6e9ef}.empty{padding:30px;text-align:center;color:#8d98aa;border:1px dashed #334057;border-radius:12px}@media(max-width:760px){body{padding:12px}.pipeline{grid-template-columns:1fr 1fr}.candidate-head{display:block}.candidate-head time{display:block;margin-top:4px}.manual{margin-left:0}}
</style></head><body>
<div class="top"><div><h1>A/B + A/B Shallow candidates</h1><div class="sub">One shared setup, two possible entries · %s</div></div>
<div class="window">Window:<a href="?hours=6">6h</a><a href="?hours=12">12h</a><a href="?hours=24">24h</a><a href="?hours=48">48h</a></div></div>
<div class="livebar"><span class="liveflag %s">%s</span><span>Detector: <b>%s</b></span><span>Last market bar: <b>%s</b></span><span>Page checks every <b>%ds</b></span><a class="manual" href="?hours=%s&amp;refresh=1">Run detector now</a></div>
<div class="stats"><div class="stat"><b>%d</b><span>candidate setups</span></div><div class="stat"><b>%d</b><span>A/B confirmed</span></div><div class="stat"><b>%d</b><span>Shallow ready</span></div></div>
<div class="legend"><h2>What each step means</h2><div class="legend-grid">
<div class="legend-item"><b>1 · Displacement + FVG</b><br>A strong impulse created a fair-value gap at a watched catalyst.</div>
<div class="legend-item"><b>2 · 50%% hold + BOS</b><br>Price respected the FVG midpoint and then broke structure in the trade direction.</div>
<div class="legend-item"><b>3 · Entry + risk</b><br>The resting entry, structural stop and target were calculated and passed the risk cap.</div>
<div class="legend-item"><b>4 · Confirmed / ready</b><br>A/B is a valid trade. Shallow can now derive its separate limit while sharing the same setup budget.</div>
</div><div class="note">Shallow is not an independent signal. It can only advance after its parent A/B setup confirms. “Ready” is a candidate preview; the Auto-Executor guard still makes the final send/block decision.</div></div>
%s<script>var q=new URLSearchParams(location.search);if(q.has('refresh')){q.delete('refresh');history.replaceState(null,'',location.pathname+(q.toString()?'?'+q.toString():''));}</script></body></html>""" % (
        refresh_seconds,
        _esc(live.get("version") or "live candidate view"),
        live_class, _esc(live_label), _esc(live.get("updated_at")), _esc(live.get("last_bar")),
        refresh_seconds, _esc(hours),
        len(candidates), ab_confirmed, shallow_ready, "".join(cards) if cards else empty,
    )
