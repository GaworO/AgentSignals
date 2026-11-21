"""Bounded, read-only RAG over the complete selected texts and source rules."""
import json
import re
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path


class Library:
    def __init__(self, database):
        self.path = Path(database).resolve()
        if not self.path.is_file(): raise FileNotFoundError('LIBRARY_NOT_COMPILED')

    @contextmanager
    def read(self):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            yield db

    def summary(self):
        with self.read() as db:
            return json.loads(db.execute('SELECT payload FROM metadata WHERE key=?', ('summary',)).fetchone()[0])

    def catalogue(self):
        with self.read() as db:
            return [json.loads(x[0]) for x in db.execute('SELECT payload FROM families ORDER BY id')]

    def sources(self):
        with self.read() as db:
            return [dict(video_id=x['video_id'], catalogue_index=x['catalogue_index'], title=x['title'],
                         cues=x['cues'], words=x['words'], source_rules=len(x['source_rule_ids']),
                         visual_verified=x['visual_verified'], source_completeness=x['source_completeness'])
                    for (p,) in db.execute('SELECT payload FROM sources') for x in [json.loads(p)]]

    def variants(self, family, limit=100, offset=0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000: raise ValueError('PAGE_BOUNDS')
        with self.read() as db:
            return [json.loads(x[0]) for x in db.execute(
                'SELECT payload FROM variants WHERE family=? ORDER BY id LIMIT ? OFFSET ?', (family, limit, offset))]

    def programs(self, family):
        with self.read() as db:
            return [json.loads(x[0]) for x in db.execute('SELECT payload FROM programs WHERE family=? ORDER BY id', (family,))]

    def records(self, family, limit=100, offset=0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000: raise ValueError('PAGE_BOUNDS')
        with self.read() as db:
            f=db.execute('SELECT payload FROM families WHERE id=?',(family,)).fetchone()
            ids=json.loads(f[0]).get('supporting_rule_ids',[])[offset:offset+limit] if f else []
            return [json.loads(db.execute('SELECT payload FROM rules WHERE id=?',(i,)).fetchone()[0]) for i in ids]

    def rule(self, ident):
        with self.read() as db:
            row = db.execute('SELECT payload FROM rules WHERE id=?', (ident,)).fetchone()
            if not row: return None
            rule = json.loads(row[0]); passages = []
            for ref in rule['references']:
                cues = [json.loads(x[0]) for x in db.execute(
                    'SELECT payload FROM cues WHERE video_id=? AND idx BETWEEN ? AND ? ORDER BY idx',
                    (ref['video_id'], ref['cue_start'], ref['cue_end']))]
                passages.append(dict(reference=ref, cues=cues))
            return dict(rule=rule, passages=passages, text_is_not_chart_verification=True)

    def search(self, query, limit=12):
        if not 1 <= limit <= 30 or not isinstance(query, str) or len(query) > 500:
            raise ValueError('QUERY_BOUNDS')
        # Treat user/model text as words, never FTS syntax or SQL.
        terms = list(dict.fromkeys(re.findall(r'[\w]+', query.casefold(), re.UNICODE)))[:20]
        if not terms: return []
        expression = ' OR '.join('"' + t + '"' for t in terms)
        with self.read() as db:
            found = db.execute('SELECT id FROM text_search WHERE text_search MATCH ? ORDER BY bm25(text_search),id LIMIT ?',
                               (expression, limit)).fetchall()
            results = []
            for (ident,) in found:
                row = json.loads(db.execute('SELECT payload FROM chunks WHERE id=?', (ident,)).fetchone()[0])
                related = []
                for (payload,) in db.execute('SELECT payload FROM rules WHERE id LIKE ?', ('full-assistant-' + row['video_id'] + '-%',)):
                    r = json.loads(payload)
                    if any(q['video_id'] == row['video_id'] and q['cue_start'] < row['cue_end_exclusive'] and q['cue_end'] >= row['cue_start']
                           for q in r['references']):
                        related.append(r)
                row['reviewed_rules'] = related
                row['whole_source_audit'] = [json.loads(p) for (p,) in db.execute('SELECT payload FROM audit_notes')
                                            if row['video_id'] in json.loads(p).get('video_ids', [])]
                row['source_rules_absent_does_not_mean_no_knowledge'] = not related
                row['supplemental_context_audit'] = [json.loads(p) for (p,) in db.execute(
                    'SELECT payload FROM gap_annotations WHERE video_id=? AND cue_index>=? AND cue_index<?',
                    (row['video_id'],row['cue_start'],row['cue_end_exclusive']))]
                results.append(row)
            return results
