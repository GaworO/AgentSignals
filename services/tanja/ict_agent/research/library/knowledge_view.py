"""Public projections over the existing versioned library. No compiler or model load.

Every connection is short-lived, query-only, and page bounded. Source text is
content, never instructions. This module cannot call a provider or broker.
"""
import json
import math
import re
import unicodedata
from .repository import Library

PRIVATE_KEYS = {'raw_path', 'local_source_file', 'evidence_storage', 'database', 'manifest',
                'api_key', 'authorization', 'password', 'secret', 'token', 'headers'}
PATH = re.compile(r'(?:/(?:Users|private|var|tmp|data|home|opt|etc)/[^\s"<>]*|[A-Za-z]:[\\/][^\s"<>]*|(?:local-runtime|research|sources)/[^\s"<>]*)')
VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')


def youtube(video, start=0):
    if not isinstance(video, str) or not VIDEO.fullmatch(video): return None
    if not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0: start = 0
    return f'https://www.youtube.com/watch?v={video}&t={int(start)}s'


def public(value):
    """Drop storage metadata recursively; only construct trusted YouTube links."""
    if isinstance(value, dict):
        out = {k: public(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS
               and not k.lower().endswith(('_path', '_file', '_dir'))}
        if 'url' in out: out['url'] = youtube(value.get('video_id'), value.get('start', 0))
        return out
    if isinstance(value, list): return [public(v) for v in value]
    if isinstance(value, str): return PATH.sub('[private location]', value)
    return value


def bounds(limit, offset):
    if not 1 <= limit <= 50 or not 0 <= offset <= 100000: raise ValueError('PAGE_BOUNDS')


def page(items, total, limit, offset):
    return dict(items=public(items), total=total, limit=limit, offset=offset,
                next_offset=offset+limit if offset+limit < total else None)


def source_summary(s):
    return dict(video_id=s['video_id'], catalogue_index=s['catalogue_index'], title=s['title'],
                series=s.get('series', []), cues=s['cues'], words=s['words'],
                source_rules=len(s.get('source_rule_ids', [])), url=youtube(s['video_id']),
                source_completeness=s.get('source_completeness'),
                prior_reviewed_cues=s.get('prior_reviewed_cues'),
                visual_verified=s.get('visual_verified', False), audio_verified=s.get('audio_verified', False))


def family_summary(f):
    return {**{k:f.get(k) for k in ('id', 'name', 'kind', 'readiness_counts')},
            'sources':len(f.get('supporting_video_ids', [])), 'rules':len(f.get('supporting_rule_ids', [])),
            'fragments':len(f.get('variant_fragment_ids', [])), 'execution_qualified':False}


class KnowledgeView:
    def __init__(self, library: Library): self.lib = library

    def overview(self):
        with self.lib.read() as db:
            kinds=[r[0] for r in db.execute("SELECT DISTINCT json_extract(payload,'$.kind') FROM families ORDER BY 1")]
            series=[r[0] for r in db.execute("SELECT DISTINCT j.value FROM sources,json_each(sources.payload,'$.series') j ORDER BY 1")]
        return dict(status='RESEARCH_LIBRARY', summary=public(self.lib.summary()), kinds=kinds, series=series,
                    freshness='VERSIONED_RESEARCH_SNAPSHOT', execution_enabled=False,
                    search_method='SQLite FTS5 full available text; lexical ranking, no generated answer')

    def families(self, q='', kind='', limit=20, offset=0):
        bounds(limit,offset)
        if len(q)>500 or len(kind)>60: raise ValueError('QUERY_BOUNDS')
        where="(?='' OR instr(lower(json_extract(payload,'$.name')||' '||id),lower(?))>0) AND (?='' OR json_extract(payload,'$.kind')=?)"
        args=(q,q,kind,kind)
        with self.lib.read() as db:
            total=db.execute('SELECT count(*) FROM families WHERE '+where,args).fetchone()[0]
            rows=[family_summary(json.loads(r[0])) for r in db.execute('SELECT payload FROM families WHERE '+where+' ORDER BY id LIMIT ? OFFSET ?',args+(limit,offset))]
        return page(rows,total,limit,offset)

    def family(self, ident, section='variants', limit=20, offset=0):
        bounds(limit,offset)
        if section not in ('variants','rules','programs'): raise ValueError('SECTION')
        with self.lib.read() as db:
            row=db.execute('SELECT payload FROM families WHERE id=?',(ident,)).fetchone()
            if not row:return None
            f=json.loads(row[0])
            if section=='rules':
                ids=f.get('supporting_rule_ids',[]);total=len(ids)
                rows=[json.loads(db.execute('SELECT payload FROM rules WHERE id=?',(i,)).fetchone()[0]) for i in ids[offset:offset+limit]]
            else:
                total=db.execute(f'SELECT count(*) FROM {section} WHERE family=?',(ident,)).fetchone()[0]
                rows=[json.loads(r[0]) for r in db.execute(f'SELECT payload FROM {section} WHERE family=? ORDER BY id LIMIT ? OFFSET ?',(ident,limit,offset))]
        return dict(family=public({**family_summary(f), 'sides':f.get('sides',{}), 'navigation_is_not_one_conjoined_program':True}),
                    section=section, **page(rows,total,limit,offset))

    def sources(self, q='', series='', limit=20, offset=0):
        bounds(limit,offset)
        if len(q)>500 or len(series)>200: raise ValueError('QUERY_BOUNDS')
        where="(?='' OR instr(lower(json_extract(s.payload,'$.title')||' '||s.video_id),lower(?))>0) AND (?='' OR EXISTS(SELECT 1 FROM json_each(s.payload,'$.series') j WHERE j.value=?))"
        args=(q,q,series,series)
        with self.lib.read() as db:
            total=db.execute('SELECT count(*) FROM sources s WHERE '+where,args).fetchone()[0]
            rows=[source_summary(json.loads(r[0])) for r in db.execute("SELECT s.payload FROM sources s WHERE "+where+" ORDER BY cast(json_extract(s.payload,'$.catalogue_index') AS INTEGER),s.video_id LIMIT ? OFFSET ?",args+(limit,offset))]
        return page(rows,total,limit,offset)

    def source(self, ident, limit=40, offset=0):
        bounds(limit,offset)
        with self.lib.read() as db:
            row=db.execute('SELECT payload FROM sources WHERE video_id=?',(ident,)).fetchone()
            if not row:return None
            s=json.loads(row[0])
            rows=[dict(index=i,**json.loads(p),video_id=ident,url=youtube(ident,start)) for i,start,p in db.execute('SELECT idx,start,payload FROM cues WHERE video_id=? ORDER BY idx LIMIT ? OFFSET ?',(ident,limit,offset))]
            total=db.execute('SELECT count(*) FROM cues WHERE video_id=?',(ident,)).fetchone()[0]
        return dict(source=public({**source_summary(s),**{k:s.get(k) for k in ('provenance','quality','prior_audit','cues_sha256','verified_references','source_rule_ids')}}),**page(rows,total,limit,offset))

    def rule(self, ident, reference=0, limit=40, offset=0):
        bounds(limit,offset)
        with self.lib.read() as db:
            row=db.execute('SELECT payload FROM rules WHERE id=?',(ident,)).fetchone()
            if not row:return None
            r=json.loads(row[0]);refs=r.get('references',[])
            if not 0<=reference<len(refs):raise ValueError('REFERENCE_BOUNDS')
            ref=refs[reference];args=(ref['video_id'],ref['cue_start'],ref['cue_end'])
            total=db.execute('SELECT count(*) FROM cues WHERE video_id=? AND idx BETWEEN ? AND ?',args).fetchone()[0]
            rows=[dict(index=i,**json.loads(p),video_id=ref['video_id'],url=youtube(ref['video_id'],start)) for i,start,p in db.execute('SELECT idx,start,payload FROM cues WHERE video_id=? AND idx BETWEEN ? AND ? ORDER BY idx LIMIT ? OFFSET ?',args+(limit,offset))]
            conditions=[json.loads(p) for (p,) in db.execute('SELECT payload FROM conditions WHERE rule_id=? ORDER BY id',(ident,))]
        return dict(rule=public(r),conditions=public(conditions),reference=reference,text_is_not_chart_verification=True,**page(rows,total,limit,offset))

    def completeness(self, category='programs', q='', family='', limit=20, offset=0):
        bounds(limit,offset)
        if category not in ('programs','conflicts','incomplete'):raise ValueError('CATEGORY')
        if len(q)>500 or len(family)>100:raise ValueError('QUERY_BOUNDS')
        table='variants' if category=='conflicts' else 'programs'
        where="(?='' OR family=?) AND (?='' OR instr(lower(id||' '||payload),lower(?))>0)"
        args=(family,family,q,q)
        if category=='conflicts':where+=" AND json_extract(payload,'$.readiness')='SOURCE_CONFLICT'"
        if category=='incomplete':where+=" AND json_extract(payload,'$.readiness')!='COMPLETE_FOR_RESEARCH'"
        with self.lib.read() as db:
            total=db.execute(f'SELECT count(*) FROM {table} WHERE '+where,args).fetchone()[0]
            rows=[json.loads(r[0]) for r in db.execute(f'SELECT payload FROM {table} WHERE '+where+' ORDER BY id LIMIT ? OFFSET ?',args+(limit,offset))]
        return dict(category=category,**page(rows,total,limit,offset))

    def search(self,q,video='',family='',series='',limit=10,offset=0):
        bounds(limit,offset)
        if not isinstance(q,str) or len(q)>500 or len(video)>100 or len(family)>100 or len(series)>200:raise ValueError('QUERY_BOUNDS')
        # Explicit bilingual query expansion only, not an LLM or inferred answer.
        folded=''.join(c for c in unicodedata.normalize('NFKD',q.lower().replace('ł','l')) if not unicodedata.combining(c))
        aliases={'plynn':'liquidity','wejsc':'entry','stop loss':'stop loss','cel':'target','luka':'gap','luki':'gap','uniewaz':'invalidation','zakres':'range','sesj':'session','potwierdz':'confirmation','odwroc':'inversion'}
        extras=[v for k,v in aliases.items() if k in folded]
        stop={'the','a','an','how','what','when','where','is','are','and','to','of','in','for','do','does','jak','jakie','kiedy','gdzie','czy','na','i','w','z','jest','sie','to'}
        terms=list(dict.fromkeys(t for t in re.findall(r'\w+',q.lower()+' '+' '.join(extras)) if t not in stop))[:20]
        if not terms:return dict(**page([],0,limit,offset),terms=[],method='FTS5_LEXICAL',answer_generated=False)
        expr=' OR '.join('"'+t+'"' for t in terms)
        where="text_search MATCH ? AND (?='' OR c.video_id=?) AND (?='' OR EXISTS(SELECT 1 FROM families f,json_each(f.payload,'$.supporting_video_ids') j WHERE f.id=? AND j.value=c.video_id)) AND (?='' OR EXISTS(SELECT 1 FROM sources s,json_each(s.payload,'$.series') j WHERE s.video_id=c.video_id AND j.value=?))"
        args=(expr,video,video,family,family,series,series)
        join=' FROM text_search JOIN chunks c ON c.id=text_search.id WHERE '+where
        with self.lib.read() as db:
            total=db.execute('SELECT count(*)'+join,args).fetchone()[0]
            rows=[]
            for (p,) in db.execute('SELECT c.payload'+join+' ORDER BY bm25(text_search),c.id LIMIT ? OFFSET ?',args+(limit,offset)):
                c=json.loads(p);s=json.loads(db.execute('SELECT payload FROM sources WHERE video_id=?',(c['video_id'],)).fetchone()[0]);c['source']=source_summary(s)
                c['related_rule_ids']=[r[0] for r in db.execute("SELECT DISTINCT r.id FROM rules r,json_each(r.payload,'$.references') j WHERE json_extract(j.value,'$.video_id')=? AND json_extract(j.value,'$.cue_start')<? AND json_extract(j.value,'$.cue_end')>=? ORDER BY r.id",(c['video_id'],c['cue_end_exclusive'],c['cue_start']))]
                rows.append(c)
        return dict(**page(rows,total,limit,offset),terms=terms,method='FTS5_LEXICAL',answer_generated=False,
                    filter_semantics='Family filter selects associated sources, not proof that every passage applies to that model.')


def mount(bp, library_store, historical_index):
    from flask import jsonify,request,abort
    def view():
        lib=library_store()
        if lib is None:abort(503,description='KNOWLEDGE_LIBRARY_NOT_CONFIGURED')
        return KnowledgeView(lib)
    def args(default=20):
        try:
            limit=int(request.args.get('limit',default));offset=int(request.args.get('offset',0));bounds(limit,offset)
            return dict(limit=limit,offset=offset)
        except ValueError:abort(400,description='PAGE_BOUNDS')
    def respond(fn):
        try:
            value=fn()
            if value is None:abort(404)
            return jsonify(value)
        except ValueError:abort(400,description='INVALID_KNOWLEDGE_QUERY')
    @bp.get('/api/knowledge/overview')
    def knowledge_overview():return respond(lambda:view().overview())
    @bp.get('/api/knowledge/families')
    def knowledge_families():return respond(lambda:view().families(request.args.get('q',''),request.args.get('kind',''),**args()))
    @bp.get('/api/knowledge/families/<ident>')
    def knowledge_family(ident):return respond(lambda:view().family(ident,request.args.get('section','variants'),**args()))
    @bp.get('/api/knowledge/sources')
    def knowledge_sources():return respond(lambda:view().sources(request.args.get('q',''),request.args.get('series',''),**args()))
    @bp.get('/api/knowledge/sources/<ident>')
    def knowledge_source(ident):return respond(lambda:view().source(ident,**args(40)))
    @bp.get('/api/knowledge/rules/<ident>')
    def knowledge_rule(ident):return respond(lambda:view().rule(ident,int(request.args.get('reference',0)),**args(40)))
    @bp.get('/api/knowledge/completeness')
    def knowledge_completeness():return respond(lambda:view().completeness(request.args.get('category','programs'),request.args.get('q',''),request.args.get('family',''),**args()))
    @bp.get('/api/knowledge/search')
    def knowledge_search():return respond(lambda:view().search(request.args.get('q',''),request.args.get('video',''),request.args.get('family',''),request.args.get('series',''),**args(10)))
    @bp.get('/api/knowledge/history')
    def knowledge_history():
        data=historical_index()
        if not data:return jsonify(status='NOT_COMPUTED',items=[],total=0)
        if data['library_release'] != view().lib.summary().get('release_id'):abort(503,description='KNOWLEDGE_HISTORY_VERSION_MISMATCH')
        options=args();family=request.args.get('family','');rule=request.args.get('rule','')
        rows=[r for r in data['items'] if (not family or family in r['family_ids']) and (not rule or rule in r['rule_ids'])]
        return jsonify(status='RECORDED_RESEARCH',library_release=data['library_release'],**page(rows[options['offset']:options['offset']+options['limit']],len(rows),**options))
