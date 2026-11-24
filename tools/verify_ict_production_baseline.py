"""Read-only predeployment gate. Never fetches, deploys or changes trading flags."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

BASELINE = 'fafc6271b2c8fcf0ac9db0f34a9e67b7962310a7'


def check(root, attestation):
    root = Path(root)
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    evidence = json.loads(Path(attestation).read_text())
    if evidence['exact_revision'] != BASELINE:
        raise ValueError('WRONG_BASELINE_ATTESTATION')
    known = git('cat-file', '-e', BASELINE + '^{commit}').returncode == 0
    ancestor = known and git('merge-base', '--is-ancestor', BASELINE, 'HEAD').returncode == 0
    changed = [p for p, h in evidence['hashes'].items()
               if not (root / p).is_file() or hashlib.sha256((root / p).read_bytes()).hexdigest() != h]
    # An ancestor alone cannot detect later deletion/reversion of the UI.
    # Deliberate protected-file edits need an explicit, reviewed new baseline.
    return {'protected_revision': BASELINE, 'head': git('rev-parse', 'HEAD').stdout.decode().strip(),
            'baseline_object_available': known, 'baseline_is_ancestor': ancestor,
            'changed_protected_files': changed,
            'status': 'PASS' if ancestor and not changed else 'BLOCKED_RECONCILIATION_REQUIRED',
            'deployment_authorized': False,
            'instruction': 'Fetch/compare the live deployment branch before any push. Reconcile protected file changes and review a new attestation; never bypass with force-push.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=Path('.'))
    p.add_argument('--attestation', type=Path, required=True)
    a = p.parse_args(); result = check(a.root, a.attestation)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'PASS' else 2)
