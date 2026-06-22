#!/usr/bin/env python3
"""gate_check.py — regression gate for the det_v11 split.

Runs the old monolith (det_v11_monolith.py) and the new thin entry (det_v11.py) over a
matrix of env configs and asserts the output pickles are byte-for-byte identical. This is
the proof that extracting detcore/ did NOT change a single signal.

Usage:
    python3 gate_check.py [DATA_CSV]      # defaults to seed.csv

After you intentionally start changing detcore/, this gate will (correctly) start failing
against the monolith — at that point delete det_v11_monolith.py and re-baseline.
"""
import os
import sys
import subprocess
import hashlib
import tempfile
import itertools

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_CSV = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'seed.csv')

MODES = ['confirm', 'sweep']
ENTRIES = ['fvg', 'fibo']
CUTOFFS = ['', '2026-05-17']


def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


def run(script, env, out_pkl):
    e = dict(os.environ, DATA_CSV=DATA_CSV, OUT_PKL=out_pkl, **env)
    r = subprocess.run([sys.executable, os.path.join(HERE, script)],
                       env=e, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"{script} failed ({env}):\n{r.stderr[-2000:]}")


def main():
    tmp = tempfile.mkdtemp()
    fails = []; n = 0
    for mode, entry, cutoff in itertools.product(MODES, ENTRIES, CUTOFFS):
        n += 1
        env = dict(MODE=mode, ENTRY_PRIMARY=entry, CUTOFF=cutoff)
        old = os.path.join(tmp, f'old_{mode}_{entry}_{cutoff or "none"}.pkl')
        new = os.path.join(tmp, f'new_{mode}_{entry}_{cutoff or "none"}.pkl')
        run('det_v11_monolith.py', env, old)
        run('det_v11.py', env, new)
        ok = md5(old) == md5(new)
        tag = f"MODE={mode:7} ENTRY={entry:4} CUTOFF={cutoff or '(none)':10}"
        print(f"  [{'OK ' if ok else 'FAIL'}] {tag}  {md5(new)}")
        if not ok:
            fails.append(tag)

    print('-' * 64)
    if fails:
        print(f"GATE FAILED: {len(fails)}/{n} configs differ")
        for f in fails:
            print('   diff:', f)
        sys.exit(1)
    print(f"GATE PASSED: {n}/{n} configs byte-identical (monolith == detcore)")


if __name__ == '__main__':
    main()
