"""Run all repository unit suites in independent, network-disabled processes.

Production and frozen research detcore have identical import names and MUST NOT
share sys.modules. Each file receives its own data directory and process.
Example: python tools/run_offline_tests.py /tmp/readiness-tests
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get('OFFLINE_TEST_ROOT', SCRIPT_ROOT)).resolve()
OUTPUT = Path(sys.argv[1]).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
FILES = sorted(set(ROOT.glob('tests/test_*.py')) | set(ROOT.glob('services/tanja/tests/test_*.py')) | set(ROOT.glob('test_*.py')))


def run(path):
    name = str(path.relative_to(ROOT)).replace('/', '__')
    folder = OUTPUT/name
    folder.mkdir(exist_ok=True)
    environment = dict(os.environ, HEARTBEAT='0', AB_AI_ENABLED='false', AUTO_SUBMIT='0',
                       EXEC_MODE='off', BUILDER_BROKER_SYNC_ENABLED='0',
                       LIVE_EXECUTION_ENABLED='true', EXEC_KILL_SWITCH='false',
                       DATA_DIR=str(folder/'data'), PYTHONPYCACHEPREFIX=str(OUTPUT/'pycache'))
    # true only inside a network-disabled process: existing unit tests exercise
    # the historical enabled branch using mocked transports. Production is false.
    for key in list(environment):
        if any(part in key for part in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY', 'WEBHOOK')):
            environment.pop(key)
    imports = [SCRIPT_ROOT/'tools/offline_test_runtime', ROOT/'tests', ROOT]
    bootstrap = 'import pytest;raise SystemExit(pytest.main(__import__("sys").argv[1:]))'
    if path.is_relative_to(ROOT/'services/tanja'):
        imports.insert(1, ROOT/'services/tanja')
        imports.insert(2, ROOT/'services/tanja/vendor')
        # python -c puts cwd ahead of PYTHONPATH. The root has different
        # modules with the same names; select the actual service runtime.
        bootstrap = ('import sys;sys.path[:0]='+repr([str(ROOT/'services/tanja'),str(ROOT/'services/tanja/vendor')])+';' + bootstrap)
    if path.name in ('test_ab_directional_engine.py', 'test_ab_directional_entry_rules.py',
                     'test_continuation_thesis_mode.py'):
        imports.insert(1, ROOT/'continuation_runtime')
        bootstrap = ('import sys;sys.path.insert(0,'+repr(str(ROOT/'continuation_runtime'))+');'
                     'import detcore;' + bootstrap)
    environment['PYTHONPATH'] = os.pathsep.join(map(str, imports))
    with (folder/'output.log').open('w') as out:
        try:
            result = subprocess.run([sys.executable, '-c', bootstrap, '-q', str(path),
                                     '--junitxml='+str(folder/'results.xml')],
                                    cwd=ROOT, env=environment, stdout=out, stderr=subprocess.STDOUT, timeout=180)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 'timeout'
    counts = {}
    if (folder/'results.xml').exists():
        xml = ET.parse(folder/'results.xml')
        for key in ('tests', 'failures', 'errors', 'skipped'):
            counts[key] = sum(int(x.get(key, 0)) for x in xml.findall('testsuite'))
    return dict(file=str(path.relative_to(ROOT)), exit_code=code, **counts)


results = []
with ThreadPoolExecutor(max_workers=4) as pool:
    for future in as_completed([pool.submit(run, path) for path in FILES]):
        result = future.result()
        results.append(result)
        print(json.dumps(result), flush=True)
(OUTPUT/'summary.json').write_text(json.dumps(sorted(results, key=lambda x:x['file']), indent=2)+'\n')
raise SystemExit(1 if any(r['exit_code'] != 0 for r in results) else 0)
