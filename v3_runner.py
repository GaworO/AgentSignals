"""Frozen Model 0 at first 2R touch; broker SL and 3R target stay unchanged.

Uses completed M1 bars strictly BEFORE the touch minute. No outcome inputs,
retraining, broker requests, or decisions from the unfinished touch candle.
"""
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
MODEL_FILE = 'v3_runner_model_2026.json'
MODEL_SHA256 = 'bc081baa5f53c1e63b4ed8be077361523266817539542fcc5ca8ff0f20059c70'
SESSIONS = ('ASIA', 'LO', 'PREM', 'NYAM', 'NYL', 'NYPM', 'PM_AH')
FEATURES = ('side', 'risk_points', 'minutes_fill_to_2r', 'minutes_to_1r_prebar',
            'minutes_to_1p5r_prebar', 'prebar_mfe_r', 'prebar_mae_r',
            'prebar_giveback_r', 'prebar_velocity_3_r', 'touch_time_sin',
            'touch_time_cos') + tuple('session_'+s for s in SESSIONS)


def model_ok(at_ms=None):
    try:
        raw = (HERE/MODEL_FILE).read_bytes()
        if sha256(raw).hexdigest() != MODEL_SHA256:
            return False
        model = json.loads(raw)
        if model['features'] != list(FEATURES) or model['threshold'] != .75:
            return False
        return at_ms is None or pd.Timestamp(int(at_ms), unit='ms', tz='UTC').tz_convert('America/New_York').year == 2026
    except (OSError, ValueError, KeyError, TypeError):
        return False


@lru_cache(maxsize=1)
def _model():
    raw = (HERE/MODEL_FILE).read_bytes()
    if sha256(raw).hexdigest() != MODEL_SHA256:
        raise ValueError('runner_model_hash_mismatch')
    return json.loads(raw)


def score(features):
    model = _model()
    x = np.asarray([features[k] for k in FEATURES], dtype=float)
    x = np.where(np.isnan(x), model['medians'], x)
    if not np.isfinite(x).all():
        raise ValueError('invalid_runner_features')
    x = (x-np.asarray(model['mean']))/np.asarray(model['scale'])
    logit = float(x @ np.asarray(model['weights'][:-1]) + model['weights'][-1])
    return 1/(1+math.exp(-max(-700., min(700., logit))))


def features(order, position, minutes, touch_ms, require_dense=True):
    touch = int(touch_ms)//60000*60000
    fill = int(position['fill_ms'])
    fill_bar = fill//60000*60000
    entry = float(position['entry_price']); risk = float(order['risk_points'])
    z = 1 if order['direction'] == 'LONG' else -1
    if risk <= 0 or not math.isfinite(risk) or touch <= fill_bar:
        raise ValueError('invalid_runner_geometry')
    ms = np.fromiter((s.value//1000000 for s in minutes.index), np.int64)
    selected = (ms > fill_bar) & (ms < touch)
    times = ms[selected]
    b = minutes.loc[selected, ['open','high','low','close']].to_numpy(float)
    if require_dense:
        expected = np.arange(fill_bar+60000, touch, 60000, dtype=np.int64)
        if not np.array_equal(times, expected):
            raise ValueError('runner_prebar_m1_gap')
    if not np.isfinite(b).all():
        raise ValueError('invalid_runner_m1')
    # Historical Model 0 timestamps are M1 opens, including fill and touch.
    fill_reference = fill_bar
    if len(b):
        fav = b[:,1] if z == 1 else b[:,2]
        adv = b[:,2] if z == 1 else b[:,1]
        if np.any(z*(fav-(entry+z*2*risk)) >= -1e-8):
            raise ValueError('runner_earlier_2r_touch_missing')
        mfe = max(0., float(np.max(z*(fav-entry))))/risk
        mae = max(0., float(np.max(-z*(adv-entry))))/risk
        giveback = mfe-z*(float(b[-1,3])-entry)/risk
        velocity = z*(float(b[-1,3])-float(b[-4,3]))/risk if len(b)>=4 else float('nan')
        def first_level(level):
            hit = np.flatnonzero(z*(fav-(entry+z*level*risk)) >= -1e-8)
            return (int(times[hit[0]])-fill_reference)/60000 if len(hit) else float('nan')
        t1,t15 = first_level(1),first_level(1.5)
    else:
        mfe=mae=giveback=velocity=t1=t15=float('nan')
    local = pd.Timestamp(touch, unit='ms', tz='UTC').tz_convert('America/New_York')
    minute = local.hour*60+local.minute
    session = ('ASIA' if minute>=1080 or minute<120 else 'LO' if minute<300 else
               'PREM' if minute<570 else 'NYAM' if minute<660 else 'NYL' if minute<810 else
               'NYPM' if minute<960 else 'PM_AH')
    result = dict(side=z, risk_points=risk, minutes_fill_to_2r=(touch-fill_reference)/60000,
                  minutes_to_1r_prebar=t1, minutes_to_1p5r_prebar=t15, prebar_mfe_r=mfe,
                  prebar_mae_r=mae, prebar_giveback_r=giveback, prebar_velocity_3_r=velocity,
                  touch_time_sin=math.sin(2*math.pi*minute/1440), touch_time_cos=math.cos(2*math.pi*minute/1440))
    result.update({'session_'+s:float(session==s) for s in SESSIONS})
    return result


def decide(order, position, minutes, touch_ms):
    if not model_ok(touch_ms):
        raise ValueError('runner_model_not_valid_for_year')
    values = features(order, position, minutes, touch_ms)
    probability = score(values)
    return dict(probability=probability, selected=probability>.75,
                features={k:None if math.isnan(v) else v for k,v in values.items()},
                model_sha256=MODEL_SHA256, threshold=.75)
