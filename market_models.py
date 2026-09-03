"""Causal, dependency-light market classification for the Monitor page.

The models in this module are research/shadow outputs only.  They never import
the executor and never return an order or position size.

Two deliberately different models are exposed:

* a four-state diagonal Gaussian Hidden Markov Model for latent regime state;
* an explainable three-class softmax classifier for the next trading day.

Both use stationary OHLCV features.  The classifier validation is walk-forward
with a one-day embargo.  The HMM's live probability is *filtered* (past/current
observations only), rather than a retrospectively smoothed state.
"""
from __future__ import annotations

import math
import threading

import numpy as np
import pandas as pd


AI_CLASSES = ("BEARISH", "NEUTRAL", "BULLISH")
HMM_STATES = 4
_CACHE = {"key": None, "value": None}
_LOCK = threading.Lock()


def _num(value, digits=3):
    try:
        value = float(value)
        return round(value, digits) if math.isfinite(value) else None
    except Exception:
        return None


def _true_range(frame):
    prev = frame["close"].shift(1)
    return pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - prev).abs(),
        (frame["low"] - prev).abs(),
    ], axis=1).max(axis=1)


def _rolling_rank_last(values):
    s = pd.Series(values)
    if len(s) < 2 or s.isna().all():
        return np.nan
    return float(s.rank(pct=True).iloc[-1])


def build_features(daily):
    """Create stationary, past-only daily features and a one-day target."""
    d = daily.sort_index().copy()
    close = d["close"].astype(float)
    ret = np.log(close / close.shift(1))
    tr = _true_range(d).astype(float)
    atr = tr.rolling(14, min_periods=10).mean()
    rv20 = ret.rolling(20, min_periods=15).std() * np.sqrt(252.0)
    rv63 = ret.rolling(63, min_periods=40).std() * np.sqrt(252.0)
    price_move = close.diff().abs()
    efficiency = close.diff(20).abs() / price_move.rolling(20, min_periods=15).sum().replace(0, np.nan)
    mid = close.rolling(20, min_periods=15).mean()
    std = close.rolling(20, min_periods=15).std()
    bb_width = (4.0 * std) / mid.replace(0, np.nan)
    range_med = tr.rolling(20, min_periods=10).median()
    volume_mean = d["volume"].astype(float).rolling(20, min_periods=10).mean()
    candle_range = (d["high"] - d["low"]).replace(0, np.nan)

    f = pd.DataFrame(index=d.index)
    f["ret_1"] = ret
    f["ret_vol_ratio"] = rv20 / rv63.replace(0, np.nan)
    f["range_ratio"] = tr / range_med.replace(0, np.nan)
    f["trend_efficiency"] = efficiency.clip(0, 1)
    f["momentum_5_atr"] = (close - close.shift(5)) / (atr * np.sqrt(5.0)).replace(0, np.nan)
    f["momentum_20_atr"] = (close - close.shift(20)) / (atr * np.sqrt(20.0)).replace(0, np.nan)
    f["volume_ratio"] = d["volume"].astype(float) / volume_mean.replace(0, np.nan)
    f["close_position"] = ((close - d["low"]) / candle_range).clip(0, 1)
    f["gap_atr"] = (d["open"] - close.shift(1)) / atr.replace(0, np.nan)
    f["bb_width_percentile"] = bb_width.rolling(252, min_periods=60).apply(_rolling_rank_last, raw=False)
    f["atr_percentile"] = atr.rolling(252, min_periods=60).apply(_rolling_rank_last, raw=False)
    f["realized_vol_20"] = rv20
    f["atr"] = atr

    # Target: next close move expressed in today's ATR.  The final row is always
    # unknown and therefore never enters training.
    fwd_atr = (close.shift(-1) - close) / atr.replace(0, np.nan)
    target = pd.Series(1, index=d.index, dtype=float)
    target[fwd_atr < -0.25] = 0
    target[fwd_atr > 0.25] = 2
    target[fwd_atr.isna()] = np.nan
    f["target"] = target
    f["forward_atr"] = fwd_atr
    return f.replace([np.inf, -np.inf], np.nan)


def _standardize(train, other=None):
    mean = np.nanmean(train, axis=0)
    std = np.nanstd(train, axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    z_train = np.clip((train - mean) / std, -8.0, 8.0)
    if other is None:
        return z_train, mean, std
    return z_train, np.clip((other - mean) / std, -8.0, 8.0), mean, std


def _logsumexp(a, axis=None):
    a = np.asarray(a, dtype=float)
    m = np.max(a, axis=axis, keepdims=True)
    out = m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True) + 1e-300)
    if axis is not None:
        out = np.squeeze(out, axis=axis)
    return out


def _log_emissions(x, means, variances):
    diff = x[:, None, :] - means[None, :, :]
    return -0.5 * np.sum(np.log(2.0 * np.pi * variances)[None, :, :] +
                         diff * diff / variances[None, :, :], axis=2)


def _forward(log_emit, start, trans):
    t_count, states = log_emit.shape
    log_start = np.log(np.clip(start, 1e-12, 1.0))
    log_trans = np.log(np.clip(trans, 1e-12, 1.0))
    alpha = np.empty((t_count, states), dtype=float)
    alpha[0] = log_start + log_emit[0]
    for t in range(1, t_count):
        alpha[t] = log_emit[t] + _logsumexp(alpha[t - 1][:, None] + log_trans, axis=0)
    return alpha, float(_logsumexp(alpha[-1], axis=0))


def _backward(log_emit, trans):
    t_count, states = log_emit.shape
    log_trans = np.log(np.clip(trans, 1e-12, 1.0))
    beta = np.zeros((t_count, states), dtype=float)
    for t in range(t_count - 2, -1, -1):
        beta[t] = _logsumexp(log_trans + log_emit[t + 1][None, :] + beta[t + 1][None, :], axis=1)
    return beta


def _kmeans_start(x, states, seed):
    rng = np.random.default_rng(seed)
    centers = [x[int(rng.integers(0, len(x)))]]
    while len(centers) < states:
        dist = np.min(np.stack([np.sum((x - c) ** 2, axis=1) for c in centers]), axis=0)
        total = float(dist.sum())
        idx = int(rng.integers(0, len(x))) if total <= 0 else int(rng.choice(len(x), p=dist / total))
        centers.append(x[idx])
    centers = np.asarray(centers, dtype=float)
    labels = np.zeros(len(x), dtype=int)
    for _ in range(25):
        labels = np.argmin(((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2), axis=1)
        fresh = centers.copy()
        for state in range(states):
            if np.any(labels == state):
                fresh[state] = x[labels == state].mean(axis=0)
        if np.allclose(fresh, centers, atol=1e-5):
            break
        centers = fresh
    return centers, labels


def _fit_hmm_once(x, states=HMM_STATES, seed=7, iterations=70):
    means, labels = _kmeans_start(x, states, seed)
    base_var = np.var(x, axis=0) + 0.05
    variances = np.stack([
        np.var(x[labels == state], axis=0) + 0.05 if np.sum(labels == state) >= 3 else base_var
        for state in range(states)
    ])
    start = np.full(states, 1.0 / states)
    trans = np.full((states, states), 0.12 / max(states - 1, 1))
    np.fill_diagonal(trans, 0.88)
    previous = -np.inf
    converged = False

    for iteration in range(iterations):
        log_emit = _log_emissions(x, means, variances)
        alpha, log_likelihood = _forward(log_emit, start, trans)
        beta = _backward(log_emit, trans)
        log_gamma = alpha + beta - log_likelihood
        gamma = np.exp(log_gamma - _logsumexp(log_gamma, axis=1)[:, None])

        xi_sum = np.zeros_like(trans)
        log_trans = np.log(np.clip(trans, 1e-12, 1.0))
        for t in range(len(x) - 1):
            log_xi = (alpha[t][:, None] + log_trans + log_emit[t + 1][None, :] +
                      beta[t + 1][None, :] - log_likelihood)
            log_xi -= float(_logsumexp(log_xi.ravel(), axis=0))
            xi_sum += np.exp(log_xi)

        weights = gamma.sum(axis=0) + 1e-9
        start = (gamma[0] + 0.01) / (gamma[0].sum() + 0.01 * states)
        trans = xi_sum + np.eye(states) * 0.25 + 0.01
        trans /= trans.sum(axis=1, keepdims=True)
        means = (gamma.T @ x) / weights[:, None]
        for state in range(states):
            diff = x - means[state]
            variances[state] = (gamma[:, state][:, None] * diff * diff).sum(axis=0) / weights[state]
        variances = np.clip(variances, 0.025, 25.0)

        if iteration > 2 and abs(log_likelihood - previous) < 1e-4 * max(1.0, abs(previous)):
            converged = True
            break
        previous = log_likelihood

    log_emit = _log_emissions(x, means, variances)
    alpha, log_likelihood = _forward(log_emit, start, trans)
    beta = _backward(log_emit, trans)
    log_gamma = alpha + beta - log_likelihood
    gamma = np.exp(log_gamma - _logsumexp(log_gamma, axis=1)[:, None])
    filtered = np.exp(alpha - _logsumexp(alpha, axis=1)[:, None])
    return {"start": start, "trans": trans, "means": means, "variances": variances,
            "gamma": gamma, "filtered": filtered, "log_likelihood": log_likelihood,
            "iterations": iteration + 1, "converged": converged}


def _fit_hmm(x, states=HMM_STATES):
    models = [_fit_hmm_once(x, states=states, seed=seed) for seed in (7, 19, 43)]
    return max(models, key=lambda model: model["log_likelihood"])


def _state_profiles(model, raw):
    hard = np.argmax(model["gamma"], axis=1)
    summaries = []
    for state in range(len(model["trans"])):
        part = raw.iloc[np.where(hard == state)[0]]
        if part.empty:
            part = raw
        summaries.append({
            "state": state,
            "rv": float(part["ret_vol_ratio"].median()),
            "trend": float(part["trend_efficiency"].median()),
            "momentum": float(part["momentum_20_atr"].median()),
            "range": float(part["range_ratio"].median()),
            "volume": float(part["volume_ratio"].median()),
            "observations": int(len(part)),
        })
    vol_cut = float(np.median([x["rv"] for x in summaries]))
    trend_cut = float(np.median([x["trend"] for x in summaries]))
    for item in summaries:
        vol = "high_vol" if item["rv"] >= vol_cut else "low_vol"
        trend = "trending" if item["trend"] >= trend_cut else "ranging"
        direction = "up" if item["momentum"] > 0.12 else "down" if item["momentum"] < -0.12 else "flat"
        if trend == "trending":
            code = f"{vol}_trend_{direction}"
            label = ("Volatile" if vol == "high_vol" else "Quiet") + f" trend {direction}"
        else:
            code = f"{vol}_range"
            label = "Choppy high-vol range" if vol == "high_vol" else "Quiet mean-reverting range"
        item.update({"code": code, "label": label,
                     "persistence": float(model["trans"][item["state"], item["state"]])})
    return summaries


def hmm_classification(daily):
    features = build_features(daily)
    columns = ["ret_1", "ret_vol_ratio", "range_ratio", "trend_efficiency",
               "momentum_20_atr", "volume_ratio"]
    valid = features[columns].dropna().iloc[-1000:]
    if len(valid) < 260:
        return {"ok": False, "status": "insufficient_history", "samples": int(len(valid)),
                "minimum_samples": 260, "shadow_only": True}
    z, _, _ = _standardize(valid.to_numpy(dtype=float))
    model = _fit_hmm(z)
    profiles = _state_profiles(model, valid)
    current = model["filtered"][-1]
    next_prob = current @ model["trans"]
    current_state = int(np.argmax(current))
    profile = profiles[current_state]
    probabilities = sorted([
        {"state": item["state"], "code": item["code"], "label": item["label"],
         "probability": _num(current[item["state"]] * 100, 1),
         "next_probability": _num(next_prob[item["state"]] * 100, 1),
         "persistence": _num(item["persistence"] * 100, 1)}
        for item in profiles
    ], key=lambda item: item["probability"], reverse=True)
    return {"ok": True, "status": "experimental_shadow", "shadow_only": True,
            "as_of": str(valid.index[-1].date()), "state": current_state,
            "code": profile["code"], "label": profile["label"],
            "confidence": _num(current[current_state] * 100, 1),
            "persistence": _num(profile["persistence"] * 100, 1),
            "probabilities": probabilities, "training_samples": int(len(valid)),
            "model": "4-state diagonal Gaussian HMM",
            "filtered_not_smoothed": True, "converged": bool(model["converged"]),
            "iterations": int(model["iterations"]),
            "log_likelihood_per_sample": _num(model["log_likelihood"] / len(valid), 3)}


def _softmax(logits):
    logits = logits - np.max(logits, axis=1, keepdims=True)
    values = np.exp(np.clip(logits, -50, 50))
    return values / values.sum(axis=1, keepdims=True)


def _fit_softmax(x, y, classes=3, iterations=700, l2=0.08):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=int)
    design = np.column_stack([np.ones(len(x)), x])
    weights = np.zeros((design.shape[1], classes), dtype=float)
    counts = np.bincount(y, minlength=classes).astype(float)
    class_weight = np.where(counts > 0, len(y) / (classes * counts), 0.0)
    sample_weight = class_weight[y]
    one_hot = np.eye(classes)[y]
    for iteration in range(iterations):
        probs = _softmax(design @ weights)
        error = (probs - one_hot) * sample_weight[:, None]
        grad = design.T @ error / max(sample_weight.sum(), 1.0)
        grad[1:] += l2 * weights[1:]
        rate = 0.08 / math.sqrt(1.0 + iteration / 80.0)
        weights -= rate * grad
    return weights


def _softmax_predict(x, weights):
    x = np.asarray(x, dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    return _softmax(design @ weights)


def _walk_forward_ai(features, columns):
    usable = features[columns + ["target"]].dropna().copy()
    if len(usable) < 380:
        return {"ok": False, "samples": 0, "reason": "insufficient walk-forward history"}
    predictions = []
    actual = []
    baselines = []
    test_start = 300
    while test_start < len(usable):
        # One-row embargo: target t uses close t+1, so the final training label
        # immediately before the test block is deliberately excluded.
        train_end = test_start - 1
        train_start = max(0, train_end - 756)
        train = usable.iloc[train_start:train_end]
        test = usable.iloc[test_start:min(test_start + 40, len(usable))]
        if len(train) < 250 or test.empty:
            test_start += 40
            continue
        x_train = train[columns].to_numpy(float)
        x_test = test[columns].to_numpy(float)
        z_train, z_test, _, _ = _standardize(x_train, x_test)
        weights = _fit_softmax(z_train, train["target"].astype(int).to_numpy())
        probs = _softmax_predict(z_test, weights)
        predictions.extend(np.argmax(probs, axis=1).tolist())
        actual.extend(test["target"].astype(int).tolist())
        majority = int(train["target"].value_counts().idxmax())
        baselines.extend([majority] * len(test))
        test_start += 40
    if not actual:
        return {"ok": False, "samples": 0, "reason": "no walk-forward folds"}
    pred = np.asarray(predictions); truth = np.asarray(actual); base = np.asarray(baselines)
    recalls = [float(np.mean(pred[truth == cls] == cls)) for cls in range(3) if np.any(truth == cls)]
    accuracy = float(np.mean(pred == truth))
    baseline = float(np.mean(base == truth))
    return {"ok": True, "samples": int(len(truth)), "accuracy": _num(accuracy * 100, 1),
            "balanced_accuracy": _num(np.mean(recalls) * 100, 1),
            "baseline_accuracy": _num(baseline * 100, 1),
            "edge_vs_baseline": _num((accuracy - baseline) * 100, 1),
            "beats_baseline": bool(accuracy > baseline),
            "method": "rolling walk-forward, one-day embargo, 40-day test blocks"}


def ai_classification(daily):
    features = build_features(daily)
    columns = ["ret_1", "ret_vol_ratio", "range_ratio", "trend_efficiency",
               "momentum_5_atr", "momentum_20_atr", "volume_ratio",
               "close_position", "gap_atr", "bb_width_percentile", "atr_percentile"]
    train = features[columns + ["target"]].dropna()
    current = features[columns].dropna().iloc[-1:] if len(features) else pd.DataFrame()
    if len(train) < 300 or current.empty:
        return {"ok": False, "status": "insufficient_history", "samples": int(len(train)),
                "minimum_samples": 300, "shadow_only": True}
    train = train.iloc[-800:]
    x_train = train[columns].to_numpy(float)
    x_current = current[columns].to_numpy(float)
    z_train, z_current, _, _ = _standardize(x_train, x_current)
    weights = _fit_softmax(z_train, train["target"].astype(int).to_numpy())
    raw_probs = _softmax_predict(z_current, weights)[0]
    prior = np.bincount(train["target"].astype(int), minlength=3).astype(float)
    prior /= prior.sum()
    probs = 0.82 * raw_probs + 0.18 * prior  # conservative shrinkage, not probability calibration
    predicted = int(np.argmax(probs))
    validation = _walk_forward_ai(features, columns)

    coefficient = weights[1:, predicted] - np.mean(np.delete(weights[1:], predicted, axis=1), axis=1)
    contribution = coefficient * z_current[0]
    order = np.argsort(np.abs(contribution))[::-1][:5]
    drivers = [{"feature": columns[i], "effect": "supports" if contribution[i] >= 0 else "opposes",
                "contribution": _num(contribution[i], 3), "value_z": _num(z_current[0, i], 2)} for i in order]
    return {"ok": True, "status": "experimental_shadow", "shadow_only": True,
            "as_of": str(current.index[-1].date()), "prediction": AI_CLASSES[predicted],
            "confidence": _num(probs[predicted] * 100, 1),
            "probabilities": {AI_CLASSES[i]: _num(probs[i] * 100, 1) for i in range(3)},
            "target": "next trading-day close move: bearish < -0.25 ATR, bullish > +0.25 ATR",
            "training_samples": int(len(train)), "model": "L2 multinomial logistic classifier",
            "probability_note": "raw model score with conservative prior shrinkage; not a guaranteed frequency",
            "drivers": drivers, "validation": validation,
            "validated": bool(validation.get("ok") and validation.get("beats_baseline") and
                              validation.get("samples", 0) >= 250)}


def classify_market(daily, deterministic_bias=None):
    """Return cached HMM + AI research classifications for the latest daily bar."""
    if daily is None or daily.empty:
        return {"hmm": {"ok": False, "status": "no_data"},
                "ai": {"ok": False, "status": "no_data"}, "consensus": {}}
    key = (len(daily), str(daily.index[-1]), _num(daily.close.iloc[-1], 4), deterministic_bias)
    with _LOCK:
        if key == _CACHE.get("key") and _CACHE.get("value") is not None:
            return _CACHE["value"]
    hmm = hmm_classification(daily)
    ai = ai_classification(daily)
    ai_bias = ai.get("prediction") if ai.get("ok") else None
    aligned = bool(deterministic_bias and ai_bias and deterministic_bias == ai_bias)
    consensus = {"rule_bias": deterministic_bias, "ai_bias": ai_bias,
                 "agreement": "aligned" if aligned else "mixed" if ai_bias else "unavailable",
                 "execution_effect": "none", "shadow_only": True,
                 "note": "HMM classifies environment; AI estimates next-day direction. Neither changes orders."}
    value = {"hmm": hmm, "ai": ai, "consensus": consensus}
    with _LOCK:
        _CACHE["key"] = key; _CACHE["value"] = value
    return value
