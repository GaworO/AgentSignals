# detcore/primitives.py
# Small pure helpers shared by more than one stage. Kept separate so confirmation.py and
# entries.py can both use them without importing each other.
#   fvgs            -> list FVGs in a bar range (used by displacement + entry)
#   impulse_end_v10 -> swing extreme + confirmation bar of an impulse (used by fibo entry)
# Bodies are verbatim from det_v11; only the array/param sources are taken from ctx.


def fvgs(ctx, a, b, bull):
    lo, hi, n, TOL = ctx.lo, ctx.hi, ctx.n, ctx.cfg.tol
    out = []
    for k in range(max(a, 2), min(b, n)):
        if bull and lo[k] > hi[k - 2] and lo[k] - hi[k - 2] >= TOL:
            out.append((round(hi[k - 2], 1), round(lo[k], 1), k))
        if not bull and hi[k] < lo[k - 2] and lo[k - 2] - hi[k] >= TOL:
            out.append((round(hi[k], 1), round(lo[k - 2], 1), k))
    return out


def impulse_end_v10(ctx, ob, bb, bull, K=2, cap=40):
    hi, lo, n = ctx.hi, ctx.lo, ctx.n
    ext = hi[ob] if bull else lo[ob]; eb = ob; stall = 0
    for m in range(ob + 1, min(n, bb + cap)):
        if (hi[m] > ext) if bull else (lo[m] < ext):
            ext = hi[m] if bull else lo[m]; eb = m; stall = 0
        elif m > bb:
            stall += 1
            if stall >= K: break
    return round(float(ext), 2), min(eb + K, n - 1)   # bar of confirmation of the extreme
