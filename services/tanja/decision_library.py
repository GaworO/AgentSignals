"""Versioned teaching examples. Never send the current session's answer as input."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PATH = Path(__file__).with_name('decision_examples.json')


def library():
    return json.loads(PATH.read_text())


def library_hash():
    return hashlib.sha256(PATH.read_bytes()).hexdigest()


def teaching_context(as_of):
    day = datetime.fromtimestamp(as_of, ZoneInfo('America/New_York')).date().isoformat()
    examples = [dict(id=x['id'], context=x['context'], decision=x['decision'],
                     uncertainty=x['uncertainty'], source=dict(recording=Path(x['source']['path']).name,
                         video_timestamp=x['source']['video_timestamp']))
        for x in library()['examples'] if x['session_date'] < day]
    if not examples: return ''
    return ('\n\nEarlier-session teaching examples (development material, not independent test results). '
        'These are source-linked summaries, not a complete raw-candle dataset. Match the contextual reasoning, '
        'not historical prices, fixed quantities or outcome. Do not quote their references as evidence for today. '
        'Ground every current claim in the supplied market packet. Missing inputs mean uncertainty or abstention. '
        'Do not add fields to the required response schema. These examples do not expand the supported executor families.\n'
        + json.dumps(examples, ensure_ascii=False, separators=(',', ':')))
