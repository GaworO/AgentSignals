"""Export repository source using an allowlist; exclude runtime/private state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ROOT_EXT={'.py','.txt','.md','.html','.json','.pine'}
ROOT_JSON={'railway.json','decision_examples.json','entry-validation.json','context-atr-validation.json','nq-loop-validation.json','ui-validation.json','pine-validation.json'}
DIRECTORIES={'vendor','static','integration','tests','tools'}
FORBIDDEN={'.env','.venv','node_modules','__pycache__','models','output','examples','data','.git'}
PRIVATE_NAMES={'account_snapshot.json','manifest.json','test-results.txt','ui-validation.json','pine-validation.json'}


def source_files():
    result=[]
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file() or p.is_symlink():continue
        rel=p.relative_to(ROOT)
        if any(x in FORBIDDEN or x.startswith('.env') for x in rel.parts) or p.name in PRIVATE_NAMES:continue
        if len(rel.parts)==1:
            include=p.name in ('Dockerfile','.dockerignore','.gitignore') or p.suffix in ROOT_EXT and (p.suffix!='.json' or p.name in ROOT_JSON)
        elif rel.parts[0] in DIRECTORIES:
            include=p.suffix in ('.py','.json','.js','.cjs','.html','.css','.md','.txt','.pine')
        elif rel.parts[0]=='live_transcription':
            include=len(rel.parts)==2 and p.suffix in ('.py','.json','.md','.txt')
            include=include or str(rel) in ('live_transcription/tests/synthetic_speech.wav','live_transcription/tests/synthetic_speech.txt','live_transcription/tests/source.json')
        else:include=False
        if not include:continue
        if p.suffix!='.wav':
            content=p.read_text()
            if re.search(r'sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',content):
                raise ValueError('Potential private credential in '+str(rel))
        result.append(p)
    return result


def build(destination):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    files=source_files();manifest={}
    guide='''# Tanja 3.2 source update — 2026-10-15

Extract this ZIP into your repository root. It contains the complete current
services/tanja source tree, tests and documentation; it does not contain a .git
directory or commit history. Review existing local changes before replacing files.

Start with services/tanja/CROSSTRADE_QUOTES_ACCOUNTING.md. This update adds separate
quote intake, GET-only CrossTrade capture and a revisable account-wide execution
ledger. It does not provide a data subscription or automatically prove fee finality.
The existing 3.1 live deployment has not been replaced by producing this package.
Strategy background: NQ_EXECUTION_LOOP_UPDATE.md, CONTEXT_ATR_UPDATE.md and ENTRY_IMPLEMENTATION.md.
Deploy services/tanja as the Railway service root, preserving the existing /data
volume, private environment variables, one worker and one replica. No deployment
or live broker order is performed by unpacking or starting this source package.

Defaults: TANJA_ANALYSIS_SYMBOL=NQ; TANJA_TRADING_LOOP_MODE=off;
TANJA_STRATEGY_POLICY=source_selections; TANJA_DAILY_ATR_PERIOD=5;
TANJA_DAILY_ATR_SESSION=futures; TANJA_ATR_SESSION_OVERRIDES={};
TANJA_CROSSTRADE_CAPTURE_ENABLED=false. Quote/accounting providers are opt-in.
An uncertain account-wide loss count blocks new entries when CrossTrade accounting
is selected. Current broker protection/order management remains unconnected.
Do not copy a
runtime account snapshot into Git. No API keys, .env files, SQLite databases,
models, virtual environments or captured transcripts are included.

Run: cd services/tanja && python3 -m unittest discover -s tests -q
The local-ASR integration is optional and requires its separate dependencies and
cached model. The core service uses requirements.txt. Browser checks use Playwright:
python3 tests/build_entry_ui_fixtures.py /tmp/tanja-entry-fixtures.json
cd ../..
ENTRY_UI_FIXTURES=/tmp/tanja-entry-fixtures.json node services/tanja/tests/browser_entry_sequences.cjs
Run browser scripts from the repository root; set PLAYWRIGHT_MODULE / CHROME_PATH
to installed runtime paths if necessary. Saved-context browser fixtures are built
with tests/build_context_atr_ui_fixtures.py and browser_context_atr.cjs.
NQ chart/loop checks use tests/build_nq_ui_fixtures.py and browser_nq_analysis.cjs.
New quote/accounting dashboard checks use the same NQ_UI_FIXTURES with
tests/browser_quotes_accounting.cjs. They mock every service request and send no orders.

PACKAGE_MANIFEST.json records SHA-256 for every included source file. The .wav
fixture is original synthetic local test speech; it is not a Tanja recording.
'''
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('README_UPLOAD.md',guide)
        for p in files:
            name='services/tanja/'+p.relative_to(ROOT).as_posix();data=p.read_bytes()
            manifest[name]=hashlib.sha256(data).hexdigest();z.writestr(name,data)
        z.writestr('PACKAGE_MANIFEST.json',json.dumps(dict(version='tanja-3.2-quotes-accounting-20261015',files=manifest),indent=2)+'\n')
    with zipfile.ZipFile(destination) as z:
        if z.testzip() is not None:raise ValueError('ZIP integrity failure')
        for name,digest in manifest.items():
            if hashlib.sha256(z.read(name)).hexdigest()!=digest:raise ValueError('Manifest mismatch')
    report=dict(path=str(destination.resolve()),files=len(files),bytes=destination.stat().st_size,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),manifest_verified=True,private_runtime_files_included=False)
    destination.with_suffix('.manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output')
    print(json.dumps(build(parser.parse_args().output),indent=2))
