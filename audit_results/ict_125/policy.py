"""Offline causal policy; never imports agent/executor or sends an order.

Full-history level lifecycles may be precomputed, but decisions expose only state
known at their cutoff. See prefix-invariance tests and SPEC.md for limitations.
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RULES = json.loads((HERE / "rules.json").read_text())
TICK = RULES["tick_points"]
VARIANTS = {"baseline": (), "1": (1,), "2": (2,), "5": (5,),
            "1+2": (1, 2), "1+5": (1, 5), "2+5": (2, 5), "1+2+5": (1, 2, 5)}


def fingerprint(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def frozen_hash():
    return hashlib.sha256("".join(fingerprint(HERE / n) for n in
                                ("rules.json", "SPEC.md", "policy.py")).encode()).hexdigest()


def load_bars(paths):
    parts = [pd.read_csv(p, usecols=["ts_event", "open", "high", "low", "close"])
             for p in paths]
    f = pd.concat(parts, ignore_index=True)
    f["ts"] = pd.to_datetime(f.ts_event, utc=True, format="ISO8601")
    f = f.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
    if not (np.isfinite(f[["open", "high", "low", "close"]]).all().all()
            and (f.high >= f[["open", "close", "low"]].max(axis=1)).all()
            and (f.low <= f[["open", "close", "high"]].min(axis=1)).all()):
        raise ValueError("Invalid OHLC rows: refusing to repair silently")
    if not (f.ts.dt.second.eq(0) & f.ts.dt.microsecond.eq(0)).all():
        raise ValueError("Expected open-stamped full minute bars")
    return f


def load_signals(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        signals = [dict(x) for x in c.execute("SELECT * FROM signals ORDER BY logged_at,key")]
    for s in signals:
        s["emitted"] = pd.Timestamp(s["logged_at"], tz="UTC")
        s["bos_ts"] = pd.Timestamp(str(s["date"]) + " " + s["bos"], tz=RULES["clock"]).tz_convert("UTC")
        s["leg"] = "shallow" if "SHALLOW" in s["cat"] else "deep"
    return signals


@dataclass
class Level:
    id: str
    kind: str
    side: int  # +1 high, -1 low
    price: float
    source_start: int
    born: int
    expires: int
    touch: int


class Engine:
    def __init__(self, bars):
        self.f = bars.reset_index(drop=True)
        self.n = len(bars)
        self.ms = self.f.ts.astype("int64").to_numpy() // 1_000_000
        self.o, self.h, self.l, self.c = [self.f[k].to_numpy(dtype=float)
                                       for k in ("open", "high", "low", "close")]
        local = self.f.ts.dt.tz_convert(RULES["clock"])
        self.dates = local.dt.strftime("%Y-%m-%d").to_numpy()
        _, self.day_no = np.unique(self.dates, return_inverse=True)
        self.day_first = np.flatnonzero(np.r_[True, np.diff(self.day_no) != 0])
        self.levels = self._levels(local)
        self.sh = np.zeros(self.n, dtype=bool)
        self.sl = np.zeros(self.n, dtype=bool)
        k = RULES["swing_confirmation_bars"]
        for j in range(k, self.n-k):
            if self.ms[j+k] - self.ms[j-k] != 2*k*60_000:
                continue
            self.sh[j] = self.h[j] >= self.h[j-k:j+k+1].max()
            self.sl[j] = self.l[j] <= self.l[j-k:j+k+1].min()

    def _levels(self, local):
        f = self.f.copy()
        f["i"] = np.arange(self.n)
        minute = local.dt.hour * 60 + local.dt.minute
        sess = np.select([minute >= 1200, (minute >= 120) & (minute < 300),
                          (minute >= 570) & (minute < 720), (minute >= 720) & (minute < 780),
                          (minute >= 810) & (minute < 960), (minute >= 960) & (minute < 1200)],
                         ["ASIA", "LO", "NYAM", "NYL", "NYPM", "PM_AH"], default="PREM")
        session_ids = np.cumsum(np.r_[True, (sess[1:] != sess[:-1]) |
                                     (self.dates[1:] != self.dates[:-1])])
        wall = local.dt.tz_localize(None)
        keys = {"day": self.dates, "week": wall.dt.to_period("W-SUN").astype(str),
                "session": session_ids}
        out = []

        def add(kind, label, price, side, start, born):
            if born >= self.n:
                return
            exp_day = int(self.day_no[born]) + RULES["liquidity_lifetime_observed_dates"]
            exp = int(self.day_first[exp_day]) if exp_day < len(self.day_first) else self.n
            # Future first-touch is used ONLY behind cutoff comparisons in feature().
            crosses = np.flatnonzero(self.h[born:exp] >= price if side == 1
                                     else self.l[born:exp] <= price)
            touch = born + int(crosses[0]) if len(crosses) else self.n
            out.append(Level(f"{kind}:{label}:{side}", kind, side, float(price),
                             int(start), int(born), exp, touch))

        for kind, key in keys.items():
            grouped = f.groupby(key, sort=False).agg(start=("i", "min"), end=("i", "max"),
                                                       high=("high", "max"), low=("low", "min"))
            # A following aggregate must be observed; first may be truncated.
            for label, g in list(grouped.iterrows())[1:-1]:
                born = int(g.end) + 1
                # IDs use source timestamp so they survive a different buffer origin.
                label = int(self.ms[int(g.start)])
                add(kind, label, g.high, 1, g.start, born)
                add(kind, label, g.low, -1, g.start, born)

        h1 = f.groupby(f.ts.dt.floor("h")).agg(start=("i", "min"), end=("i", "max"),
                                                  high=("high", "max"), low=("low", "min"))
        # Partial hours cannot confirm a swing.
        h1 = h1[(self.ms[h1.end.to_numpy()] % 3_600_000 == 3_540_000) &
                (self.ms[h1.start.to_numpy()] % 3_600_000 == 0)]
        for side, field in ((1, "high"), (-1, "low")):
            swings = []
            v = h1[field].to_numpy()
            for k in range(2, len(h1)-2):
                if side*v[k] < max(side*v[k-2:k+3]):
                    continue
                birth_ms = int(self.ms[int(h1.iloc[k+2].end)]) + 60_000
                born = bisect.bisect_left(self.ms, birth_ms)
                for old_price, old_birth, old_start in swings:
                    if birth_ms-old_birth > 24*3_600_000:
                        continue
                    if abs(v[k]-old_price) <= RULES["equal_high_low_tolerance_points"]:
                        price = round((float(v[k])+float(old_price))/2, 1)
                        add("H1_equal", f"{old_birth}:{birth_ms}", price, side, old_start, born)
                swings = [(p, t, s) for p, t, s in swings if birth_ms-t <= 24*3_600_000]
                swings.append((v[k], birth_ms, int(h1.iloc[k-2].start)))
        return sorted(out, key=lambda x: (x.born, x.id))

    def cutoff(self, s):
        # A bar stamped 10:00 is unavailable until 10:01.
        return bisect.bisect_right(self.ms, int(s["emitted"].timestamp()*1000)-60_000)-1

    def level_view(self, p, i):
        return {"id": p.id, "kind": p.kind, "price": p.price,
                "available_at": int(self.ms[p.born]),
                "collected_at": int(self.ms[p.touch])+60_000 if p.touch <= i else None,
                "state": "expired" if p.expires <= i else ("collected" if p.touch <= i else "available")}

    def objective(self, s, i, history_bars=None):
        z = 1 if s["dir"] == "LONG" else -1
        boundary = max(z*float(s["entry"]), z*self.c[i])
        start = max(0, i+1-history_bars) if history_bars else 0
        choices = [p for p in self.levels if p.side == z and p.source_start >= start
                   and p.born <= i < p.expires and p.touch > i and z*p.price > boundary]
        return min(choices, key=lambda p: (z*p.price, p.id)) if choices else None

    def fvg_anchor(self, s, i):
        z = 1 if s["dir"] == "LONG" else -1
        try:
            a, b = float(s["fvg_lo"]), float(s["fvg_hi"])
        except (ValueError, TypeError):
            return None
        for j in range(i, max(1, i-RULES["event_window_bars"]), -1):
            if self.ms[j]-self.ms[j-2] != 120_000:
                continue
            lo, hi = (self.h[j-2], self.l[j]) if z == 1 else (self.h[j], self.l[j-2])
            if hi <= lo or abs(lo-a) > .11 or abs(hi-b) > .11:
                continue
            start = j-1
            while (start > max(0, i-120) and z*(self.c[start-1]-self.o[start-1]) > 0
                   and self.ms[start]-self.ms[start-1] == 60_000):
                start -= 1
            return {"formed": j, "impulse_start": start}
        return None

    def event(self, s, i, fvg):
        if fvg is None:
            return None
        z = 1 if s["dir"] == "LONG" else -1
        reverse = s["model"].lower().startswith("rev")
        found = []
        start = max(2, i-RULES["event_window_bars"]+1)
        for p in self.levels:
            if p.side != (-z if reverse else z) or p.born > i or p.expires <= start:
                continue
            # Events must arise from a level still available when the sequence starts.
            if not start <= p.touch <= min(i, p.expires-1):
                continue
            anchor = p.touch
            price = p.price
            if reverse:
                raids = [j for j in range(anchor, min(i, fvg["formed"])+1)
                         if (self.l[j] <= price-TICK if z == 1 else self.h[j] >= price+TICK)]
                if not raids:
                    continue
                a = raids[0]
                pivots = np.flatnonzero((self.sh if z == 1 else self.sl)[max(2, a-120):max(2, a-2)])
                if not len(pivots):
                    continue
                pivot = max(2, a-120)+int(pivots[-1])
                structure = self.h[pivot] if z == 1 else self.l[pivot]
                reclaimed = [j for j in range(a, i+1) if z*(self.c[j]-price) > 0]
                if not reclaimed:
                    continue
                q = reclaimed[0]
                breaks = [j for j in range(q+1, i+1) if z*(self.c[j]-structure) >= TICK]
                if not breaks:
                    continue
                end = breaks[0]
            else:
                accepts = [j for j in range(anchor, min(i, fvg["formed"])+1)
                           if z*(self.c[j]-price) >= TICK]
                if not accepts:
                    continue
                a = accepts[0]
                if any(z*(self.c[j]-price) < 0 for j in range(a, i+1)):
                    continue
                retests = [j for j in range(a+1, i) if
                           (self.l[j] <= price if z == 1 else self.h[j] >= price)
                           and z*(self.c[j]-price) >= 0]
                if not retests:
                    continue
                q = retests[0]
                structure = self.h[a:q+1].max() if z == 1 else self.l[a:q+1].min()
                breaks = [j for j in range(q+1, i+1) if z*(self.c[j]-structure) >= TICK]
                if not breaks:
                    continue
                end = breaks[0]
            if self.ms[i]-self.ms[a] != (i-a)*60_000:
                continue
            found.append({"anchor": a, "reclaim_or_retest": q, "structure_break": end,
                          "level_id": p.id, "level_price": price, "swing_price": float(structure),
                          "type": "raid_reclaim_shift" if reverse else "accept_retest_continue"})
        return max(found, key=lambda e: (e["anchor"], e["level_id"])) if found else None

    def feature(self, s):
        i = self.cutoff(s)
        lag = (s["emitted"]-s["bos_ts"]-pd.Timedelta(minutes=1)).total_seconds()
        issue = None
        if i < 0 or i >= self.n:
            issue = "missing_history"
        elif lag < 0 or lag > RULES["maximum_signal_lag_minutes"]*60:
            issue = "stale_or_early_signal"
        elif int(s["emitted"].timestamp()*1000)-(int(self.ms[i])+60_000) >= 60_000:
            issue = "stale_candle_feed"
        elif s["emitted"].tz_convert("America/New_York").strftime("%H:%M") >= RULES["flatten_ny"]:
            issue = "after_flatten"
        if issue:
            return {"issue": issue, "cutoff": i}
        fvg = self.fvg_anchor(s, i)
        objective = self.objective(s, i)
        event = self.event(s, i, fvg)
        return {"issue": None, "cutoff": i, "fvg": fvg, "event": event,
                "objective": self.level_view(objective, i) if objective else None}

    def decision(self, s, feature, bits):
        d = {"accepted": False, "reason": feature["issue"], "entry": float(s["entry"]),
             "stop": float(s["SL"]), "target": float(s["TP"]), "qty": 0}
        if feature["issue"]:
            return d
        z = 1 if s["dir"] == "LONG" else -1
        objective, fvg, event, i = [feature[k] for k in ("objective", "fvg", "event", "cutoff")]
        if 2 in bits and not event:
            d["reason"] = "event_or_fvg_link_missing"
            return d
        if 5 in bits:
            if not fvg or not objective:
                d["reason"] = "structural_anchor_or_objective_missing"
                return d
            start = min(fvg["impulse_start"], event["anchor"]) if 2 in bits else fvg["impulse_start"]
            adverse = self.l[start:i+1].min() if z == 1 else self.h[start:i+1].max()
            d["stop"] = math.floor(adverse/TICK)*TICK-TICK if z == 1 else math.ceil(adverse/TICK)*TICK+TICK
            d["target"] = (math.floor(objective["price"]/TICK)*TICK-TICK if z == 1
                           else math.ceil(objective["price"]/TICK)*TICK+TICK)
            d["structural_start_ms"] = int(self.ms[start])
        risk = z*(d["entry"]-d["stop"])
        reward = z*(d["target"]-d["entry"])
        if risk <= 0 or reward <= 0:
            d["reason"] = "invalid_bracket"
            return d
        if risk < RULES["minimum_stop_points"]:
            d["reason"] = "stop_below_existing_5_point_minimum"
            return d
        if 1 in bits:
            if not objective:
                d["reason"] = "no_uncollected_objective"
                return d
            objective_tp = (math.floor(objective["price"]/TICK)*TICK-TICK if z == 1
                            else math.ceil(objective["price"]/TICK)*TICK+TICK)
            if z*(objective_tp-d["entry"])/risk < RULES["minimum_objective_r"]:
                d["reason"] = "objective_less_than_1R"
                return d
        d["qty"] = min(RULES["max_contracts_per_leg"], math.floor(RULES["leg_risk_budget"] /
                    (risk*RULES["mnq_point_value"]+RULES["cost_per_contract_roundtrip"])))
        if d["qty"] < 1:
            d["reason"] = "risk_exceeds_one_contract_budget"
            return d
        d.update(accepted=True, reason="accepted", risk_points=risk, target_r=reward/risk)
        return d
