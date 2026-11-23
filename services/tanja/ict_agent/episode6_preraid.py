"""Versioned pre-raid witness assessment. Never chooses a profitable reference.

Original cases, detector events and source mapping remain immutable. New OHLC
witnesses are explicitly research-derived observations, not detector MSS events.
"""
import bisect
import copy
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import digest, encode
from .episode6_qualification import Archive, sha, immutable_json, classification

VERSION='episode6-preraid-witness-v2'
SOURCE_RULES=['full-assistant-Bkt8B3kLATQ-009','full-assistant-Bkt8B3kLATQ-019']


def first_below(archive,start,end,price):
    """Earliest *available* own M1 crossing, using a range-min search."""
    times=archive.times.get(1,[])
    lo=bisect.bisect_right(times,start);hi=bisect.bisect_right(times,end)
    if hi<=lo or archive.lowest_after(start,end)>=price:return None
    left,right=lo,hi-1
    while left<right:
        mid=(left+right)//2
        if archive.lowest_after(start,times[mid])<price:right=mid
        else:left=mid+1
    return archive.frames[1][left]


def enumerate_references(archive,chain):
    raid=chain['raid'];gap=chain['fvg'];disp=chain['displacement']
    raid_bar=archive.record(raid['evidence_ids'][-1]);cutoff=gap['available_at']
    old_id=chain['cross']['evidence_ids'][0]
    rows=[];omitted=Counter();omitted_ids=[]
    for level in archive.levels:
        if level['kind']!='PIVOT' or level.get('side')!='LOW' or level['available_at']>cutoff:continue
        formed=level['pivot_ts'];formed_end=formed+level['timeframe']*60
        # Old already-breached lows are counted and reproducibly hashed, not
        # silently reused as fresh structural breaks. Keep original regardless.
        prior=archive.lowest_after(level['available_at'],raid_bar['ts'])
        old_broken=prior is not None and prior<level['price']
        if old_broken and level['id']!=old_id:
            omitted['PREVIOUSLY_BREACHED_BEFORE_RAID_REQUIRES_RECROSS_CONTEXT']+=1
            omitted_ids.append(level['id']);continue
        row={k:level[k] for k in ('id','symbol','contract','timeframe','price','available_at','pivot_ts','evidence_ids')}
        row.update(formed_end=formed_end,original_selected=level['id']==old_id,
                   known_before_raid=level['available_at']<=raid_bar['ts'],
                   first_cross=None,chain_compatible=False,mss_qualified=False,
                   source_relevance='CONTEXT_UNRESOLVED',previously_breached_before_raid=old_broken)
        if formed>=raid_bar['end']:
            row['status']='FORMED_AFTER_RAID'
        elif formed_end>raid_bar['ts']:
            row['status']='FORMATION_OVERLAPS_RAID'
        elif old_broken:
            row['status']='RECROSS_CONTEXT_UNRESOLVED'
        else:
            cross=first_below(archive,level['available_at'],cutoff,level['price'])
            row['first_cross']=cross
            if cross is None:row['status']='NOT_YET_OBSERVED'
            elif cross['ts']<level['available_at']:row['status']='REFERENCE_UNAVAILABLE_AT_CROSS_OPEN'
            elif cross['ts']<raid_bar['end']:row['status']='CROSS_BEFORE_OR_WITHIN_RAID'
            elif level['timeframe']!=chain['cross']['timeframe']:
                row['status']='OTHER_TIMEFRAME_CONTEXT_ONLY'
            elif cross['available_at']<disp['available_at']:
                row['status']='CROSS_PRECEDES_FROZEN_DISPLACEMENT'
            else:
                row['status']='ASOF_GEOMETRIC_WITNESS'
                row['chain_compatible']=True
        # No intrabar ordering, source quality or relative importance inferred.
        row['source_rule_ids']=SOURCE_RULES
        rows.append(row)
    rows.sort(key=lambda r:(r['pivot_ts'],r['timeframe'],r['id']))
    witnesses=[]
    for row in rows:
        if not row['chain_compatible']:continue
        cross=row['first_cross'];ident=digest([VERSION,raid['id'],row['id'],cross['id'],gap['id']])[:24]
        witnesses.append({'id':ident,'kind':'PRE_RAID_STRUCTURE_CROSS_WITNESS',
            'origin':'RESEARCH_OHLC_RECONSTRUCTION_NOT_DETECTOR_MSS','symbol':archive.symbol,
            'contract':archive.contract,'timeframe':1,'direction':'SHORT','ts':cross['ts'],
            'available_at':cross['available_at'],'price':row['price'],
            'reference_id':row['id'],'evidence_ids':[raid['id'],row['id'],cross['id'],disp['id'],gap['id']],
            'reference_formed_at':row['pivot_ts'],'reference_available_at':row['available_at'],
            'close_below_reference':cross['close']<row['price'],
            'body':abs(cross['close']-cross['open']),'range':cross['high']-cross['low'],
            'source_quality':'CONTEXT_UNRESOLVED','selected':False,'mss_qualified':False})
    return {'as_of':cutoff,'raid_open':raid_bar['ts'],'raid_closed':raid_bar['end'],
            'original_reference_id':old_id,'references':rows,'witnesses':witnesses,
            'selected_reference':None,'competing_references_preserved':True,
            'omitted_already_breached':dict(omitted),'omitted_reference_ids_sha256':digest(omitted_ids),
            'scope':'All available native confirmed LOW pivots. Same original M1 cross scope for comparable witnesses; other timeframes remain contextual.',
            'assumptions':['Three-candle pivot confirmation is inherited detector convention, not an ICT universal rule.',
                'Fresh-break witness excludes earlier breached lows; a recross is unresolved, not a universal model failure.',
                'Cross must follow completed raid and be at/after frozen displacement availability to connect the same recorded chain.',
                'Earlier crosses are preserved as alternative temporal paths, not silently merged with this displacement.',
                'No source-prescribed pivot age threshold or nearest-low/lowest-low preference is invented.']}


def revise_case(original,review,source):
    conditions=copy.deepcopy(original['conditions']);by={c['condition_id']:c for c in conditions}
    refs=review['references'];w=review['witnesses'];at=review['as_of']
    reference_ids=[r['id'] for r in refs]
    raw_ids=list(dict.fromkeys(reference_ids+[x['first_cross']['id'] for x in refs if x['first_cross']]))
    original_failure=by['E6R-04']['status']=='FAILED'
    for ident in ('E6R-04','E6R-05'):
        c=by[ident];c['original_assessment']=copy.deepcopy(c)
        c['evidence_ids']=raw_ids;c['assessed_at']=at
        c['assessment_scope']='ALL_ASOF_PRE_RAID_REFERENCES_NO_SELECTED_SOURCE_MSS'
        c['status']='CONTEXT_UNRESOLVED' if w else 'NOT_YET_OBSERVED'
        c['geometry_status']='VERIFIED' if w else 'NOT_YET_OBSERVED'
        c['reason']=('One or more pre-raid, known-before-cross references connect the frozen raid/displacement/FVG; all retained without source ranking or MSS promotion.'
                     if w else 'No compatible fresh-break witness established within this frozen chain; missing completion is not proof that every ICT model failed.')
        c['measurements']={'witness_count':len(w),'candidate_statuses':dict(Counter(r['status'] for r in refs)),
                           'selected_reference':None,'original_reference_failed':original_failure}
    # Once alternatives replace the cross, old single-reference leg/body evidence
    # stays visible but cannot silently become the new selected interpretation.
    for ident in ('E6R-08','E6R-13'):
        c=by[ident];c['prior_reference_scope']='ORIGINAL_CHAIN_ONLY_NOT_REBINDING_TO_A_FAVOURABLE_ALTERNATIVE'
        c['replacement_selection']='UNRESOLVED';c['status']='CONTEXT_UNRESOLVED'
    status,failed,missing,unresolved=classification(conditions,source['qualification_logic']['mandatory_condition_ids'])
    if not w and not failed:
        status='STRUCTURE_SEQUENCE_UNRESOLVED'
    patch={k:original[k] for k in ('id','candidate_id','symbol','direction','contract','decision_at','chain_id','exposure_group')}
    patch.update(version=VERSION,status=status,conditions=conditions,structural_review=review,
       source_qualified=False,execution_eligible=False,entry=None,stop=None,target=None,rr=None,
       failed_mandatory_conditions=failed,missing_mandatory_data=missing,unresolved_mandatory_conditions=unresolved,
       original_classification=original['status'],original_case_sha256=digest(original),
       corrected_reference_defect=original_failure and bool(w),
       qualification_scope='COMPETING_RESEARCH_WITNESSES_NOT_A_SELECTED_SOURCE_QUALIFIED_TRADE',
       original_chain_preserved=True)
    return patch


def run(original_root,frequency_path,output):
    original_root=Path(original_root).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    if (output/'qualification.sqlite').exists():raise ValueError('IMMUTABLE_VERSION_ALREADY_EXISTS')
    source_path=original_root/'source/episode6-reference.json';source=json.loads(source_path.read_text())
    before=json.loads((original_root/'summary.json').read_text())
    manifest={'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
       'original_database_sha256':sha(original_root/'qualification.sqlite'),
       'original_summary_sha256':sha(original_root/'summary.json'),
       'original_manifest_sha256':sha(original_root/'manifest.json'),
       'source_map_sha256':sha(source_path),'frequency_database_sha256':sha(frequency_path),
       'runtime_sha256':sha(__file__),'inherited_runtime_sha256':sha(Path(__file__).with_name('episode6_qualification.py')),
       'population':544,'selection':'All original cases, no profitability/future-outcome selection',
       'policy':'Enumerate pre-raid native pivot candidates and all compatible own-M1 fresh-cross witnesses. Select none.',
       'source_rule_ids':SOURCE_RULES,'source_timestamps_seconds':[[1109,1194],[1252,1316],[2068,2145]],
       'formed_before':'Pivot candle fully closed before raid opens','confirmed_before':'Available no later than crossing candle open',
       'cross_relation':'Own M1 crossing after raid closes and at/after original displacement availability, by original FVG availability',
       'old_breached_lows':'Not fresh-break witnesses; recross context explicitly unresolved',
       'source_selected_reference':None,'c3_requirement_unchanged':True,'no_paid_calls':True,'orders':False,
       'execution_policy':'NOT_FROZEN_NO_COMPLETE_SOURCE_TRADE_PLAN','pf_status':'PF_NOT_ESTIMABLE'}
    immutable_json(output/'manifest.json',manifest)
    src=sqlite3.connect(Path(frequency_path).resolve().as_uri()+'?mode=ro',uri=True)
    old=sqlite3.connect((original_root/'qualification.sqlite').as_uri()+'?mode=ro',uri=True)
    db=sqlite3.connect(output/'qualification.sqlite')
    db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT);
      CREATE TABLE cases(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,decision_at INTEGER,status TEXT,chain_id TEXT,exposure_group TEXT,payload TEXT);
      CREATE TABLE conditions(case_id TEXT,condition_id TEXT,status TEXT,payload TEXT,PRIMARY KEY(case_id,condition_id));
      CREATE INDEX condition_filter ON conditions(condition_id,status);
      CREATE TABLE evidence(id TEXT PRIMARY KEY,available_at INTEGER,kind TEXT,payload TEXT);''')
    db.execute('INSERT INTO metadata VALUES(?,?)',('original_directory',str(original_root)))
    counts=Counter();funnel=defaultdict(Counter);instruments=defaultdict(Counter);archive=None;examples={};checks=Counter()
    for raw, in old.execute('SELECT payload FROM cases ORDER BY symbol,decision_at,id'):
        original=json.loads(raw);symbol=original['symbol'];chain=original['observed_chain']
        if archive is None or archive.symbol!=symbol:archive=Archive(src,symbol,original['contract'])
        review=enumerate_references(archive,chain);patch=revise_case(original,review,source)
        refids=set()
        for r in review['references']:
            refids.add(r['id'])
            if r['first_cross']:refids.add(r['first_cross']['id'])
        evidence=archive.evidence(refids,review['as_of'])
        for e in evidence.values():db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)',(e['id'],e['available_at'],e.get('kind','CANDLE'),encode(e)))
        for w in review['witnesses']:
            r=next(x for x in review['references'] if x['id']==w['reference_id']);bar=r['first_cross']
            assert r['formed_end']<=review['raid_open'] and r['available_at']<=bar['ts']
            assert bar['ts']>=review['raid_closed'] and chain['displacement']['available_at']<=bar['available_at']<=review['as_of']
            assert bar['low']<r['price'] and r['symbol']==symbol and r['contract']==original['contract']
            checks['independent_witness_relationships']+=1
            db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)',(w['id'],w['available_at'],w['kind'],encode(w)))
        by={c['condition_id']:c for c in patch['conditions']}
        oldby={c['condition_id']:c for c in original['conditions']}
        for c in patch['conditions']:
            if c['condition_id'] not in ('E6R-04','E6R-05','E6R-08','E6R-13'):assert c==oldby[c['condition_id']]
            funnel[c['condition_id']][c['status']]+=1
            db.execute('INSERT INTO conditions VALUES(?,?,?,?)',(original['id'],c['condition_id'],c['status'],encode(c)))
        witnesses=bool(review['witnesses']);oldfailed=oldby['E6R-04']['status']=='FAILED'
        geom=witnesses and oldby['E6R-03']['geometry_status']=='VERIFIED' and all(by[k]['status']=='VERIFIED' for k in ('E6R-07','E6R-09','E6R-10','E6R-11','E6R-12'))
        retest=original['retest_observation']['status']=='VERIFIED'
        metrics={'cases':1,'original_reference_failed':oldfailed,'reference_defect_corrected':oldfailed and witnesses,
          'original_failure_still_unresolved':oldfailed and not witnesses,'with_pre_raid_witness':witnesses,
          'without_compatible_witness':not witnesses,'multiple_witnesses':len(review['witnesses'])>1,
          'witness_count':len(review['witnesses']),'decidable_mandatory_geometry_verified':geom,
          'decidable_geometry_and_contiguous_retest':geom and retest,
          'corrected_with_full_geometry_and_retest':oldfailed and geom and retest,
          'corrected_but_C3_failed':oldfailed and witnesses and not original['geometry']['c3_below_c2'],
          patch['status']:1}
        for k,v in metrics.items():counts[k]+=v;instruments[symbol][k]+=v
        label=('corrected_C3_failure' if oldfailed and witnesses and not original['geometry']['c3_below_c2'] else
               'corrected_geometry' if oldfailed and geom and retest else
               'still_unresolved' if oldfailed and not witnesses else 'other')
        if label not in examples:examples[label]=original['id']
        db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?,?,?)',(*[patch[k] for k in ('id','candidate_id','symbol','decision_at','status','chain_id','exposure_group')],encode(patch)))
        checks['cases']+=1
        if checks['cases']%100==0:db.commit();print(json.dumps({'cases':checks['cases'],'corrected':counts['reference_defect_corrected']}),flush=True)
    assert checks['cases']==544 and counts['original_reference_failed']==309
    db.commit();db.close();src.close();old.close()
    assert sha(original_root/'qualification.sqlite')==manifest['original_database_sha256']
    assert sha(frequency_path)==manifest['frequency_database_sha256'] and sha(source_path)==manifest['source_map_sha256']
    summary=copy.deepcopy(before);summary.update(version=VERSION,generated_at=datetime.now(timezone.utc).isoformat(),
       manifest_sha256=sha(output/'manifest.json'),structural_comparison={'before':before['measured_context_differences'],
       'after':dict(counts),'by_instrument':{s:dict(c) for s,c in instruments.items()},'examples':examples},
       parent_artifact_preserved=True,original_population_counts=before['population_counts'],validation=dict(checks))
    for k in ('decidable_mandatory_geometry_verified','decidable_geometry_and_contiguous_retest'):summary['population_counts'][k]=counts[k]
    summary['classification_counts']={k:counts[k] for k in ('SOURCE_QUALIFIED_HISTORICAL_SETUP','QUALIFIED_GEOMETRY_CONTEXT_UNRESOLVED','SOURCE_CONDITION_FAILED','INSUFFICIENT_SOURCE_OR_MARKET_DATA','STRUCTURE_SEQUENCE_UNRESOLVED')}
    for c in summary['condition_funnel']:c['counts']={s:funnel[c['condition_id']][s] for s in c['counts']}
    summary['limitations'].extend(['Pre-raid witnesses are derived research observations, not automatic ICT MSS certification. No alternative is selected.',
      'Six inherited clock discontinuities fall around 17:00–18:00 NY. April/May calendar coverage is uncertified; these are not proven feed missing bars.',
      'Source-qualified, complete plans and simulated trades remain zero; no fill/PnL evaluated.'])
    immutable_json(output/'summary.json',summary)
    immutable_json(output/'publication.json',{'manifest_sha256':sha(output/'manifest.json'),'database_sha256':sha(output/'qualification.sqlite'),
        'summary_sha256':sha(output/'summary.json'),'runtime_sha256':sha(__file__),'original_database_unchanged':True})
    return summary
