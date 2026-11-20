import hashlib
import json
import importlib.util
import sys
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CORPUS = Path(os.environ.get('ICT_AI_CORPUS_DIR', REPO / 'research/ict_knowledge_20261119/corpus_615'))
ATLAS = Path(os.environ.get('ICT_AI_ATLAS_DIR', REPO / 'research/ict_entry_reference_20261120'))
OUTPUT = REPO / 'research/ict_agent_validation_20261120'

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result

from . import resilient_io as _io
read_bytes, read_json, read_gzip_json = _io.read_bytes, _io.read_json, _io.read_gzip_json

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    tmp.replace(path)

def code_hash():
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): hashlib.sha256(read_bytes(p)).hexdigest()
                   for p in sorted(root.rglob('*')) if p.suffix in ('.py', '.js', '.html')})
