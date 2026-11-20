"""Read-only canonical evidence, verified references, structured + local LSA RAG."""
import hashlib
import re
import sqlite3
from collections import Counter, defaultdict, OrderedDict
import numpy as np
from .common import CORPUS, ATLAS, read_bytes, read_json, read_gzip_json, digest, encode, save

def uncovered(count, ranges):
    cursor = 0
    gaps = []
    for a, b in sorted(ranges):
        if not 0 <= a <= b <= count:
            raise ValueError('INVALID_REVIEW_RANGE')
        if a > cursor:
            gaps.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < count:
        gaps.append([cursor, count])
    return gaps

def normalize(a):
    return a / np.maximum(np.linalg.norm(a,axis=-1,keepdims=True),1e-12)

class LatentIndex:
    """Deterministic local TF-IDF + randomized truncated SVD using NumPy only.

    No pretrained multilingual embedding claim. Exact token/case retrieval remains
    separate; LSA is a co-occurrence approximation requiring relevance evaluation.
    """
    def __init__(self,texts):
        documents=[self.tokens(t) for t in texts]
        df=Counter(t for d in documents for t in set(d))
        words=sorted(df,key=lambda t:(-df[t],t))[:8000]
        self.vocab={w:i for i,w in enumerate(words)}
        self.idf=np.array([math.log((1+len(texts))/(1+df[w]))+1 for w in words],dtype=np.float32)
        self.matrix=self.transform_tokens(documents)
        rng=np.random.default_rng(20261120)
        dims=min(64,len(texts)-1,len(words)-1)
        projection=rng.standard_normal((len(words),dims+8)).astype(np.float32)
        q,_=np.linalg.qr(self.matrix@projection)
        q,_=np.linalg.qr(self.matrix@(self.matrix.T@q))
        _,_,v=np.linalg.svd(q.T@self.matrix,full_matrices=False)
        self.components=v[:dims]
        self.vectors=normalize(self.matrix@self.components.T)

    @staticmethod
    def tokens(text):
        words=re.findall(r'[\w]+',text.casefold())
        return words+[a+' '+b for a,b in zip(words,words[1:])]

    def transform_tokens(self,docs):
        out=np.zeros((len(docs),len(self.vocab)),dtype=np.float32)
        for i,words in enumerate(docs):
            for word,count in Counter(words).items():
                col=self.vocab.get(word)
                if col is not None:out[i,col]=(1+math.log(count))*self.idf[col]
        return normalize(out)

    def scores(self,text):
        tokens=self.transform_tokens([self.tokens(text)])
        return self.vectors@normalize(tokens@self.components.T)[0],(self.matrix@tokens.T).ravel()

import math

class Knowledge:
    def __init__(self, audit_path=None, *, corpus=None, atlas=None):
        from pathlib import Path
        self.corpus=Path(corpus or CORPUS);self.atlas=Path(atlas or ATLAS)
        raw = read_bytes(self.corpus / 'detailed-rules.json')
        self.version = hashlib.sha256(raw).hexdigest()
        self.rules = __import__('json').loads(raw)
        self.by_id = {r['id']: r for r in self.rules}
        atlas = read_json(self.atlas / 'reference.json')
        if [r['source_record'] for r in atlas['records']] != self.rules:
            raise ValueError('STALE_ATLAS')
        self.tags = {r['id']: r['curated_family_ids'] for r in atlas['records']}
        self.families = atlas['families']
        self.notes = atlas['audit_notes']
        self.audit = self._audit()
        if audit_path:
            save(audit_path, self.audit)
        self.texts = [encode({k: r.get(k) for k in ('model','concepts','description','fields','exceptions','ambiguities','contradictions')}) for r in self.rules]
        self.index=LatentIndex(self.texts)
        self.index_version = digest(dict(rules=self.version, algorithm='tfidf+latent-semantic-analysis', dimensions=64,
                                        random_state=20261120, numpy=np.__version__,max_features=8000,sequence_templates='source-bound-v1',contextual='bidirectional-context-v1'))
        self.retrieval_kind = 'LOCAL_LSA_64D_PLUS_TFIDF_STRUCTURED_CASES_EXCEPTIONS'
        self.cache = OrderedDict()

    def _audit(self):
        catalogue = read_json(self.corpus / 'catalogue.json')
        coverage = read_json(self.corpus / 'exhaustive/coverage.json')['videos']
        if len(self.by_id) != len(self.rules):
            raise ValueError('DUPLICATE_RULE')
        with sqlite3.connect((self.corpus / 'knowledge.sqlite').as_uri() + '?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('CORPUS_SQLITE_INTEGRITY')
            db_rules = {i: __import__('json').loads(p) for i,p in db.execute('SELECT id,payload FROM rules')}
            if db_rules != self.by_id:
                raise ValueError('CANONICAL_SQLITE_JSON_MISMATCH')
        refs = defaultdict(list)
        for r in self.rules:
            if r['status'] == 'ASSISTANT_FULL_TRANSCRIPT_REVIEW':
                for ref in r['references']:
                    refs[ref['video_id']].append((r['id'], ref))
        verified = 0
        for vid, items in sorted(refs.items()):
            cues = read_gzip_json(self.corpus / 'exhaustive/private_sources' / (vid + '.json.gz'))
            for ident, ref in items:
                a, b = ref['cue_start'], ref['cue_end']
                if not 0 <= a <= b < len(cues) or digest(cues[a:b+1]) != ref['passage_sha256']:
                    raise ValueError('SOURCE_HASH_MISMATCH:' + ident)
                if ref['url'] != f'https://www.youtube.com/watch?v={vid}&t={int(cues[a]["start"])}s':
                    raise ValueError('SOURCE_URL_MISMATCH:' + ident)
                verified += 1
        sections = []
        for v in coverage:
            count = v.get('cue_count', 0)
            ranges = v.get('assistant_read_ranges_exclusive', [])
            missing = uncovered(count, ranges)
            if v['status'] == 'FULL_AVAILABLE_TRANSCRIPT_REVIEWED' and missing:
                raise ValueError('FALSE_FULL_COVERAGE:' + v['video_id'])
            sections.append(dict(video_id=v['video_id'], catalogue_index=v['catalogue_index'], title=v['title'],
                status=v['status'], available_cues=count, unreviewed_cue_ranges_exclusive=missing,
                full_audio_visual_verified=v.get('full_video_completed', False),
                source_completeness=v.get('source_completeness', 'UNKNOWN'),
                limitation=v.get('completion_limitation'), source_quality=v.get('source_quality')))
        self.verified = {r['id'] for r in self.rules if r['status']=='ASSISTANT_FULL_TRANSCRIPT_REVIEW'}
        case_ids = [r['id'] for r in self.rules if re.search(r'example|case.study|worked|przykład', r['category']+' '+r['model'], re.I)]
        self.case_ids = set(case_ids)
        # Categorization describes stored fields, never certifies a market outcome.
        result = dict(catalogue_entries=len(catalogue), unique_catalogue_videos=len({r['video_id'] for r in catalogue}),
            review_states=dict(Counter(v['status'] for v in coverage)), rules=len(self.rules),
            full_available_text_records=len(self.verified), legacy_partial_records=len(self.rules)-len(self.verified),
            videos_referenced=len({ref['video_id'] for r in self.rules for ref in r['references']}),
            extraction_classifications=dict(Counter(r['classification'] for r in self.rules)),
            cue_hash_references_verified=verified, verified_reference_videos=len(refs),
            stored_case_candidates=len(case_ids), case_rule_ids=case_ids,
            case_classification='Lexical category/model identification; not independent verification of complete example sequences or outcomes.',
            records_with_exceptions=sum(bool(r.get('exceptions')) for r in self.rules),
            exception_items=sum(len(r.get('exceptions', [])) for r in self.rules),
            records_with_ambiguities=sum(bool(r.get('ambiguities')) for r in self.rules),
            records_with_contradictions=sum(bool(r.get('contradictions')) for r in self.rules),
            audit_notes=len(self.notes), curated_families=len(self.families),
            all_media_verified=False, cross_video_conflict_audit_complete=False,
            independently_verified_profitable_rules=0, rules_sha256=self.version,
            outside_catalogue_downloads='Not counted as analyzed unless integrated into canonical coverage.',
            sections=sections)
        if hashlib.sha256(read_bytes(self.corpus/'detailed-rules.json')).hexdigest()!=self.version:
            raise ValueError('KNOWLEDGE_CHANGED_DURING_AUDIT')
        return result

    def record(self, ident):
        r = self.by_id[ident]
        return dict(id=ident, model=r['model'], direction=r['direction'], classification=r['classification'],
            fields=r['fields'], description=r['description'], concepts=r.get('concepts', []),
            families=self.tags[ident], exceptions=r.get('exceptions', []),
            ambiguities=r.get('ambiguities', []), contradictions=r.get('contradictions', []),
            references=r['references'], visual_required=r.get('visual_required', False),
            evidence_status='CUE_HASH_VERIFIED_TEXT_ONLY' if ident in self.verified else 'LEGACY_PARTIAL_NOT_REVERIFIED',
            executable=False, case=dict(context=r['fields'].get('market_conditions',r['fields'].get('prerequisites')),
                sequence={k:r['fields'].get(k) for k in ('entry_confirmation','entry_trigger','invalidation','take_profit')},
                rationale=r['description'], outcome='UNKNOWN_NOT_INFERRED',
                classification='LEXICAL_CASE_CANDIDATE_WITH_VERIFIED_TEXT_ANCHORS') if ident in self.case_ids else None)

    def retrieve(self, query, direction='ANY', event_sequence=(), limit=5):
        if direction not in ('ANY','LONG','SHORT') or not 1<=limit<=12:
            raise ValueError('RETRIEVAL_CONTRACT')
        key = (query, direction, tuple(event_sequence), limit)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        semantic,lexical=self.index.scores(query+' '+' '.join(event_sequence))
        score = .55*semantic + .45*lexical
        eligible = [i for i,r in enumerate(self.rules) if r['id'] in self.verified and
                    (direction=='ANY' or r['direction'] in (direction,'BOTH')) and lexical[i] > .015]
        ranked = sorted(eligible, key=lambda i:(-float(score[i]), self.rules[i]['id']))
        # A separate case lane and exception lane keep caveats in the packet.
        selected = ranked[:limit]
        for condition in (lambda r:r['id'] in self.case_ids,
                          lambda r:bool(r.get('exceptions') or r.get('contradictions') or r['fields'].get('invalidation'))):
            extra = next((i for i in ranked if condition(self.rules[i]) and i not in selected), None)
            if extra is not None:
                selected.append(extra)
        from .sequences import match, TEMPLATES
        # Explicit source templates enrich retrieval, not the trading rule engine.
        for i in ranked:
            template=TEMPLATES.get(self.rules[i]['id'])
            if template and template['topic'].casefold() in query.casefold() and i not in selected:selected.append(i)
        records = [dict(self.record(self.rules[i]['id']), relevance=round(float(score[i]),6),
                        source_sequence=match(self.rules[i]['id'],list(event_sequence))) for i in selected]
        vids = {ref['video_id'] for r in records for ref in r['references']}
        ids = {r['id'] for r in records}
        result = dict(knowledge_version=self.version,index_version=self.index_version,method=self.retrieval_kind,
            semantic_limit='Local latent semantic vectors; no pretrained embedding model, no measured relevance benchmark.',
            event_sequence=list(event_sequence),sequence_matching='Explicit ordered concept matching for two source-bound templates; others uncompiled. No assertion of market-condition fulfillment.',
            records=records, audit_notes=[n for n in self.notes if ids.intersection(n.get('rule_ids',[])) or vids.intersection(n.get('video_ids',[]))],
            status='MATCHES_REQUIRE_CONDITION_VALIDATION' if records else 'KNOWLEDGE_GAP',
            family_count_in_index=len(self.families))
        self.cache[key] = result
        while len(self.cache)>256:self.cache.popitem(last=False)
        return result

    def contextual(self, context, events):
        """Search both directions and cautions before a model chooses a thesis."""
        event_sequence=[e['kind'] for e in sorted(events,key=lambda e:(e['available_at'],e['id']))
                        if e['kind'] in ('SWEEP','STRUCTURE_BREAK','DISPLACEMENT','FVG','IFVG')][-10:]
        descriptors=[]
        for symbol,ctx in sorted(context.items()):
            for tf in ('1440','240','60','15','5','1'):
                r=ctx.get('ranges',{}).get(tf)
                if r:descriptors.append(symbol+' '+tf+' '+r['price_location'])
            descriptors += [z['kind']+' '+z.get('direction','') for z in ctx.get('active_zones',[])]
            descriptors += [l['kind']+' '+l.get('side','') for l in ctx.get('active_levels',[])]
        base=' '.join(sorted(set(descriptors)))+' '+' '.join(event_sequence)
        queries=[('LONG_HYPOTHESIS','LONG',base+' bullish discount reversal continuation prerequisites'),
                 ('SHORT_HYPOTHESIS','SHORT',base+' bearish premium reversal continuation prerequisites'),
                 ('EXCEPTIONS_AND_NO_TRADE','ANY',base+' exception contradiction invalidation no trade missing confirmation worked example')]
        groups=[self.retrieve(q,d,event_sequence,limit=3) for _,d,q in queries]
        records={};notes={}
        # Round-robin keeps both directional lanes before whole-record packet limits.
        for rank in range(max(len(g['records']) for g in groups)):
            for (role,_,_),group in zip(queries,groups):
                if rank>=len(group['records']):continue
                record=group['records'][rank]
                if record['id'] not in records:records[record['id']]=dict(record,retrieval_roles=[])
                records[record['id']]['retrieval_roles'].append(role)
        for g in groups:
            for n in g['audit_notes']:notes[digest(n)]=n
        return dict(knowledge_version=self.version,index_version=self.index_version,
            method=self.retrieval_kind+'+BIDIRECTIONAL_CONTEXT',queries=[dict(role=r,direction=d,query=q) for r,d,q in queries],
            records=list(records.values()),audit_notes=list(notes.values()),event_sequence=event_sequence,
            role_limit='Retrieval intent only; model/human must assess actual support or contradiction.')
