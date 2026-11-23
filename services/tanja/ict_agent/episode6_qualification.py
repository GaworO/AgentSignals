"""Read-only, event-time Episode 6 research qualification; never an order engine.

The source mapping is separately reviewed and hash-pinned. Existing frequency
selector candidates define the population, not a source-qualified model. Final
selector states and later profitability never choose a historical interpretation.
"""
import bisect
import hashlib
import json
import sqlite3
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import digest, encode
from .market import clock

VERSION='episode6-qualification-v1'
STATUSES={'VERIFIED','FAILED','NOT_YET_OBSERVED','DATA_UNAVAILABLE','SOURCE_AMBIGUOUS','CONTEXT_UNRESOLVED'}


def sha(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def immutable_json(path,value):
    data=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    path=Path(path)
    if path.exists():
        if path.read_bytes()!=data:raise ValueError('IMMUTABLE_OUTPUT_CONFLICT:'+path.name)
    else:
        with path.open('xb') as f:f.write(data)


class Archive:
    """One-instrument bounded historical cache, never combines correlated bars."""
    def __init__(self,db,symbol,contract):
        self.db=db;self.symbol=symbol;self.contract=contract;self.frames={};self.times={};self.by_id={};self.event_cache={}
        for raw, in db.execute('SELECT payload FROM frames WHERE symbol=? ORDER BY at',(symbol,)):
            f=json.loads(raw)
            if f['contract']!=contract:continue
            self.frames.setdefault(f['timeframe'],[]).append(f);self.by_id[f['id']]=f
        self.times={tf:[f['available_at'] for f in fs] for tf,fs in self.frames.items()}
        lows=array('d',[f['low'] for f in self.frames.get(1,[])])
        self.low_min=[lows];span=2
        while span<=len(lows):
            prev=self.low_min[-1];half=span//2
            self.low_min.append(array('d',(min(prev[i],prev[i+half]) for i in range(len(lows)-span+1))))
            span*=2
        self.levels=[];self.bull_zones=[]
        for raw, in db.execute("SELECT payload FROM events WHERE symbol=? AND kind IN ('PIVOT','EQUAL_EXTREMES','PRIOR_SESSION_LEVEL','PRIOR_RTH_LEVEL','PRIOR_WEEK_LEVEL','FVG') ORDER BY at",(symbol,)):
            e=json.loads(raw)
            if e['contract']!=contract:continue
            if e['kind']=='FVG':
                if e['direction']=='LONG':self.bull_zones.append(e)
            else:self.levels.append(e)

    def event(self,ident):
        if ident not in self.event_cache:
            row=self.db.execute('SELECT payload FROM events WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('MISSING_EVENT:'+ident)
            self.event_cache[ident]=json.loads(row[0])
        event=self.event_cache[ident]
        if event['symbol']!=self.symbol or event['contract']!=self.contract:
            raise ValueError('CROSS_INSTRUMENT_EVIDENCE')
        return event

    def closed(self,tf,at,count=None):
        fs=self.frames.get(tf,[]);end=bisect.bisect_right(self.times.get(tf,[]),at)
        return fs[max(0,end-count):end] if count else fs[:end]

    def between(self,start,end):
        fs=self.frames.get(1,[]);times=self.times.get(1,[])
        lo=bisect.bisect_right(times,start);hi=bisect.bisect_right(times,end)
        return fs[lo:hi]

    def record(self,ident):
        return self.by_id.get(ident) or self.event(ident)

    def lowest_after(self,start,end):
        times=self.times.get(1,[]);lo=bisect.bisect_right(times,start);hi=bisect.bisect_right(times,end)
        if hi<=lo:return None
        k=(hi-lo).bit_length()-1;span=1<<k;level=self.low_min[k]
        return min(level[lo],level[hi-span])

    def evidence(self,ids,at):
        seen={};stack=list(ids)
        while stack:
            ident=stack.pop()
            if ident in seen:continue
            value=self.record(ident)
            if value['available_at']>at:raise ValueError('FUTURE_EVIDENCE:'+ident)
            seen[ident]=value;stack.extend(value.get('evidence_ids',[]))
        return seen


def reconstruct_chain(archive,setup):
    """Select only the originally recorded chain, never a later better gap."""
    root=archive.event(setup['root_event'])
    records=[archive.event(i) for i in setup['supporting'] if i not in archive.by_id]
    displacement=next(e for e in records if e['kind']=='DISPLACEMENT')
    cross=next(e for e in records if e['kind']=='STRUCTURE_BREAK')
    gap=archive.event(setup['plan']['evidence_ids'][0])
    if gap['kind']!='FVG' or gap['direction']!='SHORT':raise ValueError('SELECTED_GAP_NOT_SHORT_FVG')
    candles=[archive.record(i) for i in gap['evidence_ids']]
    if len(candles)!=3:raise ValueError('FVG_THREE_CANDLES_REQUIRED')
    a,b,c=candles
    if not(a['end']==b['ts'] and b['end']==c['ts']):raise ValueError('FVG_CANDLES_NONCONTIGUOUS')
    if not all(x['symbol']==setup['symbol'] and x['contract']==setup['contract'] for x in [root,displacement,cross,gap,*candles]):
        raise ValueError('CHAIN_INSTRUMENT_MISMATCH')
    if not root['available_at']<displacement['available_at']<=cross['available_at']<=gap['available_at']:
        raise ValueError('CHAIN_AVAILABILITY_ORDER')
    if gap['lower']!=c['high'] or gap['upper']!=a['low'] or not c['high']<a['low']:
        raise ValueError('RECORDED_FVG_GEOMETRY_MISMATCH')
    return {'raid':root,'displacement':displacement,'cross':cross,'fvg':gap,'c1':a,'c2':b,'c3':c}


def retest_observation(archive,chain,horizon):
    gap=chain['fvg'];start=gap['available_at'];end=chain['raid']['available_at']+horizon*60
    bars=archive.between(start,end);expected=start;missing=[];touch=None;invalidation=None
    for bar in bars:
        if bar['ts']<start:continue
        if bar['ts']!=expected:missing.append({'from':expected,'until':bar['ts']})
        expected=bar['end']
        # This is a descriptive reversal challenge, not an asserted source stop.
        if invalidation is None and bar['close']>chain['raid']['extreme']:invalidation=bar
        if bar['high']>=gap['lower'] and bar['low']<=gap['upper']:
            touch=bar;break
    if touch:
        status='DATA_UNAVAILABLE' if missing else 'VERIFIED'
        reason='EARLIEST_AVAILABLE_OWN_M1_ZONE_OVERLAP_AFTER_GAP_CLOSED'
        if missing:reason='EARLIEST_RETEST_CANNOT_BE_PROVEN_ACROSS_MISSING_M1'
    else:
        if expected<end:missing.append({'from':expected,'until':end})
        status='DATA_UNAVAILABLE' if missing else 'NOT_YET_OBSERVED'
        reason='NO_RETEST_OBSERVED_IN_ENGINEERING_HORIZON_NOT_SOURCE_FAILURE'
    return {'status':status,'reason':reason,'formation_at':start,'observation_end':end,
            'horizon_minutes':horizon,'horizon_scope':'ENGINEERING_OBSERVATION_LIMIT_NOT_SOURCE_EXPIRY',
            'first_available_touch':touch,'missing_intervals':missing,
            'engineering_challenge_before_or_same_touch':invalidation,
            'same_bar_touch_and_challenge':bool(touch and invalidation and touch['id']==invalidation['id']),
            'touch_wick_reference_checks':None if not touch else {
                'high_above_raid_extreme':touch['high']>chain['raid']['extreme'],
                'high_above_C2_high':touch['high']>chain['c2']['high'],
                'high_above_C1_high':touch['high']>chain['c1']['high'],
                'meaning':'Conditional structural references only; no selected buffered stop or intrabar fill ordering.'},
            'intrabar_order_verified':False,'fill_inferred':False}


def htf_ranges(archive,at,price):
    ranges=[]
    daily=archive.closed(1440,at,1)
    if daily:
        f=daily[0];ranges.append({'id':'range:prior-d1:'+f['id'],'kind':'LAST_COMPLETED_D1',
            'timeframe':1440,'low':f['low'],'high':f['high'],'anchor_ids':[f['id']],
            'available_at':f['available_at'],'anchor_selection':'COMPLETE_SESSION_BOUNDS_NOT_CERTIFIED_ICT_RANGE'})
    for tf in (15,60,240):
        levels=[e for e in archive.levels if e['kind']=='PIVOT' and e['timeframe']==tf and e['available_at']<=at]
        hi=next((e for e in reversed(levels) if e['side']=='HIGH'),None)
        lo=next((e for e in reversed(levels) if e['side']=='LOW'),None)
        if hi and lo and hi['price']>lo['price']:
            ranges.append({'id':digest(['range',hi['id'],lo['id']])[:24],'kind':'LATEST_CONFIRMED_PIVOT_PAIR',
                'timeframe':tf,'low':lo['price'],'high':hi['price'],'anchor_ids':[lo['id'],hi['id']],
                'available_at':max(lo['available_at'],hi['available_at']),
                'anchor_selection':'CANDIDATE_PAIR_CONTEXT_RELEVANCE_UNRESOLVED'})
    for r in ranges:
        r['equilibrium']=(r['low']+r['high'])/2
        r['price_location']='PREMIUM' if price>r['equilibrium'] else 'DISCOUNT' if price<r['equilibrium'] else 'EQUILIBRIUM'
        r['source_selected']=False;r['status']='CONTEXT_UNRESOLVED'
    return ranges


def target_inventory(archive,at,entry):
    levels=[e for e in archive.levels if e.get('side')=='LOW' and e['available_at']<=at and e['price']<entry]
    rows=[]
    for level in levels:
        # Lower lows already printed after confirmation make an old low consumed.
        lowest=archive.lowest_after(level['available_at'],at)
        if lowest is not None and lowest<level['price']:continue
        rows.append({'id':level['id'],'kind':level['kind'],'timeframe':level['timeframe'],
            'price':level['price'],'distance_points':entry-level['price'],'available_at':level['available_at'],
            'evidence_ids':[level['id']],'directionally_eligible':True,'strictly_crossed_asof':False,
            'exact_touch_observed':lowest==level['price'],
            'consumption_scope':'AVAILABLE_ARCHIVE_BARS_ONLY_DISCONTINUITIES_NOT_FILLED',
            'source_eligibility':'CONTEXT_UNRESOLVED','source_preference':'UNRESOLVED',
            'detector_tolerance_ticks':level.get('tolerance_ticks'),
            'tolerance_is_engineering_convention':level['kind']=='EQUAL_EXTREMES',
            'role':'EXTERNAL_OR_LOCAL_LOW_CANDIDATE_NOT_SELECTED'})
    # Internal opposing imbalance is an alternative objective, never assumed a resting-stop pool.
    for zone in archive.bull_zones:
        if zone['available_at']>at or zone['upper']>=entry:continue
        lowest=archive.lowest_after(zone['available_at'],at)
        if lowest is not None and lowest<=zone['lower']:continue
        rows.append({'id':zone['id'],'kind':'OPPOSING_FVG_UPPER_BOUNDARY','timeframe':zone['timeframe'],
            'price':zone['upper'],'lower':zone['lower'],'upper':zone['upper'],
            'distance_points':entry-zone['upper'],'available_at':zone['available_at'],
            'evidence_ids':[zone['id']],'directionally_eligible':True,
            'upper_boundary_touched_asof':lowest is not None and lowest<=zone['upper'],
            'mitigation_status':'PARTIALLY_TOUCHED_UPPER_BOUNDARY' if lowest is not None and lowest<=zone['upper'] else 'NO_UPPER_BOUNDARY_TOUCH_IN_AVAILABLE_BARS',
            'consumption_scope':'AVAILABLE_ARCHIVE_BARS_ONLY_DISCONTINUITIES_NOT_FILLED',
            'source_eligibility':'CONTEXT_UNRESOLVED','source_preference':'UNRESOLVED',
            'role':'INTERNAL_IMBALANCE_OBJECTIVE_CANDIDATE_NOT_SELECTED'})
    rows.sort(key=lambda x:(x['distance_points'],x['available_at'],x['id']))
    for index,row in enumerate(rows):row['numerical_rank']=index+1
    return rows


def target_availability_at_retest(archive,targets,formation,at):
    """Update frozen objectives using only information received by the retest.

    No new preferred target is selected; no later price outcome can backdate
    availability into the formation decision.
    """
    rows=[]
    for target in targets:
        row=dict(target);lowest=archive.lowest_after(formation,at)
        if target['kind']=='OPPOSING_FVG_UPPER_BOUNDARY':
            consumed=lowest is not None and lowest<=target['lower']
            upper_touch=lowest is not None and lowest<=target['upper']
            state='ZONE_FULLY_TOUCHED' if consumed else 'UPPER_BOUNDARY_TOUCHED' if upper_touch else 'NO_NEW_TOUCH_IN_AVAILABLE_BARS'
        else:
            consumed=lowest is not None and lowest<target['price']
            state='LOW_STRICTLY_CROSSED' if consumed else 'LOW_EXACTLY_TOUCHED' if lowest==target['price'] else 'NO_NEW_TOUCH_IN_AVAILABLE_BARS'
        row.update({'formation_cutoff':formation,'retest_assessment_cutoff':at,
                    'later_availability_status':state,'consumed_before_or_on_retest':consumed,
                    'intrabar_order_on_retest_known':False,'source_preference':'UNRESOLVED'})
        rows.append(row)
    return rows


def freeze_policy(source_path,frequency_path,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    manifest={'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),
        'source_map_sha256':sha(source_path),'frequency_database_sha256':sha(frequency_path),
        'qualification_code_sha256':sha(__file__),
        'population':{'all_short_selector_candidates':1931,'complete_engineering_chains':544,
            'selection':'All originally selected SHORT chains; not ranked by outcome or profitability.'},
        'observation_policy':{'horizon_minutes':90,'origin':'Original engineering selector expires_at from raid detection',
            'source_expiry_claim':False,'retest':'Earliest later own-instrument closed M1 overlaps frozen gap after formation',
            'missing_m1':'DATA_UNAVAILABLE, never manufacture earliest event or failure',
            'context_and_target_cutoff':'Original selected FVG availability; later retest is separately labelled observation',
            'fill_inferred':False},
        'source_mapping_provenance':'RECOVERED_SOURCE_MAPPING_NOT_VERIFIED_IDENTICAL_TO_MISSING_PRIOR_20_CONDITION_ARTIFACT',
        'no_charter_transfer':True,'source_stop_alternatives_not_merged':True,
        'no_source_universal_numeric_displacement_threshold':True,
        'paid_calls':0,'orders':False,'simulation':False,'PF':'PF_NOT_ESTIMABLE',
        'execution_blockers':['DATED_CONTRACT_MAPPING_UNVERIFIED','FEES_SLIPPAGE_AND_FILL_MODEL_NOT_FROZEN',
            'SOURCE_POLICY_QUALIFICATION_PENDING','NO_UNTOUCHED_OOS_ARCHIVE_ESTABLISHED']}
    immutable_json(output/'manifest.json',manifest)
    return manifest


def assess_conditions(archive,setup,chain,retest,ranges,targets,source):
    formation=chain['fvg']['available_at'];a,b,c=(chain[x] for x in ('c1','c2','c3'))
    root=chain['raid'];cross=chain['cross'];disp=chain['displacement'];gap=chain['fvg']
    raid_frame=archive.record(root['evidence_ids'][-1]);raid_ref=archive.record(root['evidence_ids'][0])
    cross_frame=archive.record(cross['evidence_ids'][-1]);low_ref=archive.record(cross['evidence_ids'][0])
    disp_frame=archive.record(disp['evidence_ids'][0]);touch=retest['first_available_touch']
    local=clock(formation);anchor_cutoff=int(local.replace(hour=8,minute=30,second=0,microsecond=0).timestamp())
    m15=[e for e in archive.levels if e['kind']=='PIVOT' and e['side']=='HIGH' and e['timeframe']==15
         and e['available_at']<=min(formation,anchor_cutoff) and e.get('pivot_ts',e['ts'])<anchor_cutoff]
    m15_anchor=max(m15,key=lambda e:e.get('pivot_ts',e['ts'])) if m15 else None
    sequence_ids=[root['id'],disp['id'],cross['id'],gap['id']]
    geometry={
        'reference_known_before_raid':raid_ref['available_at']<=raid_frame['ts'],
        'raid_above_reference':raid_frame['high']>raid_ref['price'],
        'reclaim_close_below_reference':raid_frame['close']<raid_ref['price'],
        'low_formed_before_raid':low_ref.get('pivot_ts',low_ref['ts'])<raid_frame['ts'],
        'low_known_before_cross':low_ref['available_at']<=cross_frame['ts'],
        'cross_low_below_reference':cross_frame['low']<low_ref['price'],
        'raid_closed_before_cross_bar':root['available_at']<=cross_frame['ts'],
        'c2_below_c1':b['low']<a['low'],'c3_below_c2':c['low']<b['low'],
        'bearish_nonoverlap':c['high']<a['low'],
        'displacement_completed':disp_frame['end']<=disp_frame['available_at']<=formation,
        'displacement_close_below_structural_low':disp_frame['close']<low_ref['price']}
    cases={}
    def result(ident,status,reason,ids=(),measurements=None,**extra):
        if status not in STATUSES:raise ValueError('UNKNOWN_CONDITION_STATUS')
        card=next(x for x in source['conditions'] if x['id']==ident)
        cases[ident]={'condition_id':ident,'title':card['title'],'semantic_role':card['semantic_role'],
            'status':status,'reason':reason,'evidence_ids':list(dict.fromkeys(ids)),
            'source_rule_ids':card['source_rule_ids'],'requirement':card['requirement'],'scope':card['scope'],
            'mandatory':ident in source['qualification_logic']['mandatory_condition_ids'],
            'applicability':True,'assessment_scope':'RECORDED_ENGINEERING_CHAIN_UNDER_SOURCE_REVIEW',
            'assessed_at':formation,'measurements':measurements or {},**extra}
    range_ids=[i for r in ranges for i in r['anchor_ids']]
    result('E6R-01','CONTEXT_UNRESOLVED' if ranges else 'DATA_UNAVAILABLE',
           'Closed ranges exist but source-grounded bearish draw/context has not been selected.' if ranges else 'No completed HTF range anchors available.',
           range_ids,{'range_candidates':len(ranges),'locations':dict(Counter(r['price_location'] for r in ranges)),
                       'source_selected_range':None,'numerically_nearest_target':targets[0]['id'] if targets else None})
    if formation<anchor_cutoff:anchor_status='NOT_YET_OBSERVED';anchor_reason='Today 08:30 anchor-selection point has not yet occurred; no universal session rejection.'
    elif not m15_anchor:anchor_status='DATA_UNAVAILABLE';anchor_reason='No confirmed M15 swing-high anchor available at 08:30.'
    else:anchor_status='CONTEXT_UNRESOLVED';anchor_reason='Latest confirmed M15 high left of 08:30 supplied; contextual selection and demonstrated-branch correspondence remain unresolved.'
    result('E6R-02',anchor_status,anchor_reason,[m15_anchor['id']] if m15_anchor else [],
        {'anchor_cutoff':anchor_cutoff,'anchor':m15_anchor,'raid_uses_this_exact_anchor':bool(m15_anchor and raid_ref['id']==m15_anchor['id'])},
        mandatory_for=['episode6.demonstrated.daily-range.short'],applicability='CONDITIONAL_BRANCH')
    raid_ok=geometry['reference_known_before_raid'] and geometry['raid_above_reference']
    result('E6R-03','CONTEXT_UNRESOLVED' if raid_ok else 'FAILED',
        'Own-candle raid geometry verified; meaningful liquidity-reference selection is not independently established.' if raid_ok else 'Recorded raid does not cross a reference known before the raid candle.',
        [root['id']],{k:v for k,v in geometry.items() if k.startswith(('reference_','raid_above','reclaim_'))},
        geometry_status='VERIFIED' if raid_ok else 'FAILED')
    low_ok=geometry['low_formed_before_raid'] and geometry['low_known_before_cross']
    low_after_raid=low_ref.get('pivot_ts',low_ref['ts'])>=raid_frame['end']
    low_failed=low_after_raid or not geometry['low_known_before_cross']
    result('E6R-04','FAILED' if low_failed else 'CONTEXT_UNRESOLVED',
        'Pre-raid structural-low geometry is available; contextual relevance remains unselected.' if low_ok else
        'The selected low formed after the completed raid or was unavailable before the break. This rejects this chain reference, not every possible setup.' if low_failed else
        'The low belongs to an overlapping raid candle; intrabar ordering cannot prove that it preceded the raid.',
        [low_ref['id'],root['id'],cross['id']],{'low_pivot_time':low_ref.get('pivot_ts',low_ref['ts']),
            'raid_open_time':raid_frame['ts'],'low_available_at':low_ref['available_at'],'cross_open_time':cross_frame['ts'],
            'formed_before_raid':geometry['low_formed_before_raid'],'known_before_cross':geometry['low_known_before_cross']},
        geometry_status='VERIFIED' if low_ok else 'FAILED' if low_failed else 'CONTEXT_UNRESOLVED')
    cross_ok=geometry['cross_low_below_reference']
    cross_status='FAILED' if not cross_ok else 'VERIFIED' if geometry['raid_closed_before_cross_bar'] else 'CONTEXT_UNRESOLVED'
    result('E6R-05',cross_status,'Downside crossing of the recorded low after the raid verified; relevance of that low is assessed separately.' if cross_status=='VERIFIED' else 'Crossing order cannot be certified within an overlapping aggregated candle.' if cross_ok else 'Required downside crossing absent.',
        [root['id'],cross['id']],{'low':cross_frame['low'],'reference_price':low_ref['price'],
        'raid_available_at':root['available_at'],'cross_open_time':cross_frame['ts']})
    result('E6R-06','CONTEXT_UNRESOLVED','Existing energetic-move detector supplies measurements, not a source-certified universal displacement threshold.',
        [disp['id']],{'range':disp_frame['high']-disp_frame['low'],'body':abs(disp_frame['close']-disp_frame['open']),
         'down_body':disp_frame['close']<disp_frame['open'],'engineering_definition':disp.get('definition'),
         'C2_is_original_detector_displacement_candle':b['id']==disp_frame['id']})
    result('E6R-07','VERIFIED' if geometry['displacement_completed'] else 'FAILED','Displacement candle was closed and available before frozen FVG decision.',[disp['id']],
        {'end':disp_frame['end'],'available_at':disp_frame['available_at'],'formation_decision':formation})
    result('E6R-08','CONTEXT_UNRESOLVED','Numerical close relation measured; source preference for strong body remains a qualitative optional assessment.',
        [disp['id'],low_ref['id']],{'close_below':geometry['displacement_close_below_structural_low'],
        'close':disp_frame['close'],'reference':low_ref['price']},mandatory=False)
    result('E6R-09','VERIFIED','Own-instrument consecutive completed C1/C2/C3 verified.',[a['id'],b['id'],c['id']],
        {'timeframe':a['timeframe'],'formation_decision':formation})
    result('E6R-10','VERIFIED' if geometry['c2_below_c1'] else 'FAILED','Strict source inequality tested independently.',
        [a['id'],b['id']],{'C1_low':a['low'],'C2_low':b['low'],'required':'C2.low < C1.low'})
    result('E6R-11','VERIFIED' if geometry['c3_below_c2'] else 'FAILED','Strict source inequality tested independently; generic FVG does not imply this condition.',
        [b['id'],c['id']],{'C2_low':b['low'],'C3_low':c['low'],'required':'C3.low < C2.low'})
    result('E6R-12','VERIFIED','Bearish nonoverlap and exact recorded FVG boundaries verified.',[gap['id']],
        {'C1_low':a['low'],'C3_high':c['high'],'zone_lower':gap['lower'],'zone_upper':gap['upper']})
    leg_frames=archive.between(root['available_at'],formation)
    low_frame=min(leg_frames,key=lambda f:f['low']) if leg_frames else c
    leg_choices=[{'end_role':'RECORDED_STRUCTURAL_LOW','low':low_ref['price'],'high':root['extreme'],'evidence_ids':[root['id'],low_ref['id']]},
                 {'end_role':'LOWEST_OBSERVED_POST_RAID_LOW_BY_FORMATION','low':low_frame['low'],'high':root['extreme'],'evidence_ids':[root['id'],low_frame['id']]}]
    before_break=archive.between(raid_frame['ts'],cross['available_at'])
    leg_high=max(before_break,key=lambda f:f['high']) if before_break else raid_frame
    leg_choices.append({'end_role':'EXPANDED_HIGH_THROUGH_CROSS_TO_POST_RAID_LOW','low':low_frame['low'],'high':leg_high['high'],
                        'evidence_ids':[leg_high['id'],low_frame['id']]})
    for leg in leg_choices:leg['gap_inside_price_bounds']=leg['low']<=gap['lower']<=gap['upper']<=leg['high']
    result('E6R-13','CONTEXT_UNRESOLVED',
        'Original raid high, expanded high through cross, structural low and later displacement low retained; source-relevant leg has not been selected. One failed arbitrary leg bound is not source failure.',
        sequence_ids+[low_frame['id'],low_ref['id'],leg_high['id']],{'leg_candidates':leg_choices})
    alternatives=[]
    for raw, in archive.db.execute("SELECT payload FROM events WHERE symbol=? AND kind='FVG' AND direction='SHORT' AND at>=? AND at<=? ORDER BY at,id",(setup['symbol'],root['available_at'],formation)):
        e=json.loads(raw)
        if e['contract']==setup['contract']:alternatives.append({'id':e['id'],'timeframe':e['timeframe'],'available_at':e['available_at'],'lower':e['lower'],'upper':e['upper']})
    result('E6R-14','CONTEXT_UNRESOLVED','Original engineering-selected gap preserved; no unsupported first-chronological rule or timeframe tie-break imposed.',
        [e['id'] for e in alternatives],{'selected':gap['id'],'available_detected_alternatives':alternatives,
         'native_detector_timeframes':[1,5,15,60,240,1440],
         'M2_M3_M4':'Raw M1 may permit derivation, but source-context selection not performed by recovered detector.'},
        applicability='CONDITIONAL_CHOICE',mandatory_for=['WHEN_MATERIAL_GAP_SELECTION_UNRESOLVED'])
    source_retest_status='CONTEXT_UNRESOLVED' if retest['status']=='VERIFIED' else retest['status']
    result('E6R-15',source_retest_status,retest['reason']+'; qualified gap and entry branch remain unresolved, so observed overlap alone does not verify the full source return-entry condition.',
        [gap['id']]+([touch['id']] if touch else []),{'zone_retest_observed':bool(touch),
        'source_gap_geometry_passes':geometry['c2_below_c1'] and geometry['c3_below_c2'],
        'exact_selected_entry_touch':'UNRESOLVED_NO_ENTRY_BRANCH_OR_DATED_TICK_BINDING',
        'horizon':retest['observation_end'],'missing_intervals':retest['missing_intervals']},
        assessed_at=touch['available_at'] if touch else retest['observation_end'],
        assessment_scope='LATER_RETEST_OBSERVATION_NOT_RETROACTIVE_FORMATION_DECISION',
        geometry_status=retest['status'])
    result('E6R-16','SOURCE_AMBIGUOUS','Source alternatives retained, but one entry branch/offset policy is not fixed. No candle touch is a market fill.',
        [gap['id']],{'C3_high_reference':c['high'],'preferred_limit_formula':'C3.high + one verified tick',
        'other_source_alternatives':['one/two ticks or handle variation in example','market on return; fill unknown'],
        'numeric_executable_entry':None})
    result('E6R-17','SOURCE_AMBIGUOUS','C2 and C1/swing-high alternatives remain separate; no exact above-buffer or branch is selected.',
        [b['id'],a['id'],root['id']],{'C2_high':b['high'],'C1_high':a['high'],
        'recorded_raid_swing_high':root['extreme'],'C2_above_FVG_lower':b['high']>gap['lower'],
        'C1_above_FVG_lower':a['high']>gap['lower'],'stop_buffer':None,'selected_stop':None})
    result('E6R-18','DATA_UNAVAILABLE','No dated contract mapping, frozen cost/fill policy or separately authorized execution/risk evaluation.',[],
        {'market_qualification_blocker':False,'broker_execution':False},mandatory=False,assessment_scope='EXECUTION_ONLY')
    target_ids=[t['id'] for t in targets]
    result('E6R-19','CONTEXT_UNRESOLVED',
        'Numerical target inventory retained; nearest level is not automatically preferred ICT objective. Internal and external branches are alternatives.' if targets else 'No numerically eligible observed downside objective found; contextual choice or older source data must be resolved, not invented.',
        target_ids,{'targets':len(targets),'nearest_numerical_id':targets[0]['id'] if targets else None,
        'preferred_source_target':None,'internal_external_are_and_or':True})
    result('E6R-20','SOURCE_AMBIGUOUS','Optional post-entry management is not evaluated; full thesis invalidation and exact trailing policy are not defined. Engineering expiry/challenge remain separately labelled.',
        [],{'post_entry_events_used':False,'later_engineering_challenge':'REPORTED_SEPARATELY_NOT_USED_AT_FORMATION'},
        mandatory=False,applicability=False,assessment_scope='OPTIONAL_MANAGEMENT_NOT_APPLIED')
    return [cases[c['id']] for c in source['conditions']],geometry,leg_choices


def classification(conditions,mandatory_ids):
    mandatory=[c for c in conditions if c['condition_id'] in mandatory_ids]
    failed=[c['condition_id'] for c in mandatory if c['status']=='FAILED']
    missing=[c['condition_id'] for c in mandatory if c['status']=='DATA_UNAVAILABLE']
    unresolved=[c['condition_id'] for c in mandatory if c['status']!='VERIFIED']
    if failed:return 'SOURCE_CONDITION_FAILED',failed,missing,unresolved
    if missing:return 'INSUFFICIENT_SOURCE_OR_MARKET_DATA',failed,missing,unresolved
    if unresolved:return 'QUALIFIED_GEOMETRY_CONTEXT_UNRESOLVED',failed,missing,unresolved
    return 'SOURCE_QUALIFIED_HISTORICAL_SETUP',failed,missing,unresolved


def make_chart(archive,chain,retest,targets,at):
    frames=archive.closed(1,at,150);annotations=[]
    for key in ('raid','cross','displacement','fvg'):
        e=chain[key];price=e.get('price')
        if key=='displacement':price=archive.record(e['evidence_ids'][0])['close']
        annotations.append({'kind':e['kind'],'observation_id':e['id'],
            'available_at':e['available_at'],'ts':e['ts'],'price':price,
            'lower':e.get('lower'),'upper':e.get('upper'),'status':'OBSERVED_GEOMETRY',
            'evidence_ids':e.get('evidence_ids',[])})
    touch=retest['first_available_touch']
    if touch:annotations.append({'kind':'RETEST','observation_id':touch['id'],'available_at':touch['available_at'],
        'ts':touch['ts'],'lower':chain['fvg']['lower'],'upper':chain['fvg']['upper'],
        'status':'OBSERVED_ZONE_TOUCH_NOT_FILL','evidence_ids':[chain['fvg']['id'],touch['id']]})
    for label in ('c1','c2'):
        f=chain[label];annotations.append({'kind':'STOP_REFERENCE_'+label.upper(),'observation_id':f['id'],
            'available_at':f['available_at'],'ts':f['ts'],'price':f['high'],
            'status':'CONDITIONAL_STRUCTURAL_REFERENCE_NOT_ORDER_STOP','evidence_ids':[f['id']]})
    for target in targets[:10]:
        e=archive.record(target['id']);annotations.append({'kind':'TARGET_CANDIDATE','observation_id':e['id'],
            'available_at':e['available_at'],'ts':e['ts'],'price':target['price'],
            'status':'NUMERICAL_RANK_'+str(target['numerical_rank'])+'_NOT_SOURCE_PREFERENCE','evidence_ids':[e['id']]})
    return {'symbol':archive.symbol,'direction':'SHORT','as_of':at,'frames':{'1':frames},
        'annotations':annotations,'prices_source':'ACTUAL_ARCHIVE_OHLC','orders_or_fills':False,
        'chart_limit':150,'target_annotations_limit':10,'full_target_inventory_retained':True}


def run(frequency_path,source_path,output):
    output=Path(output);source=json.loads(Path(source_path).read_text())
    if (output/'qualification.sqlite').exists():raise ValueError('QUALIFICATION_OUTPUT_ALREADY_EXISTS')
    if set(c['id'] for c in source['conditions'])!={f'E6R-{i:02}' for i in range(1,21)}:raise ValueError('RECOVERED_SOURCE_CONDITIONS_CHANGED_REVIEW_REQUIRED')
    manifest=freeze_policy(source_path,frequency_path,output)
    src=sqlite3.connect(Path(frequency_path).resolve().as_uri()+'?mode=ro',uri=True);src.execute('PRAGMA query_only=ON')
    population=[json.loads(r[0]) for r in src.execute("SELECT payload FROM candidates WHERE direction='SHORT' ORDER BY symbol,created_at,id")]
    rows=[json.loads(r[0]) for r in src.execute("SELECT payload FROM candidates WHERE direction='SHORT' AND complete_geometry=1 ORDER BY symbol,created_at,id")]
    if len(population)!=1931 or len(rows)!=544:raise ValueError('FROZEN_POPULATION_MISMATCH')
    db=sqlite3.connect(output/'qualification.sqlite')
    db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT);
      CREATE TABLE population(candidate_id TEXT PRIMARY KEY,symbol TEXT,created_at INTEGER,complete_geometry INTEGER);
      CREATE TABLE cases(id TEXT PRIMARY KEY,candidate_id TEXT,symbol TEXT,decision_at INTEGER,status TEXT,chain_id TEXT,exposure_group TEXT,payload TEXT);
      CREATE INDEX case_filter ON cases(symbol,status,decision_at);
      CREATE TABLE conditions(case_id TEXT,condition_id TEXT,status TEXT,payload TEXT,PRIMARY KEY(case_id,condition_id));
      CREATE INDEX condition_filter ON conditions(condition_id,status);
      CREATE TABLE evidence(id TEXT PRIMARY KEY,available_at INTEGER,kind TEXT,payload TEXT);
      CREATE TABLE validation(key TEXT PRIMARY KEY,value INTEGER);''')
    db.execute('INSERT INTO metadata VALUES(?,?)',('manifest',encode(manifest)))
    complete_ids={s['id'] for s in rows}
    for s in population:db.execute('INSERT INTO population VALUES(?,?,?,?)',(s['id'],s['symbol'],s['created_at'],int(s['id'] in complete_ids)))
    archive=None;checks=Counter();counts=Counter();funnel=defaultdict(Counter);case_ids=[];exact_chains=set()
    for index,setup in enumerate(rows):
        if archive is None or archive.symbol!=setup['symbol'] or archive.contract!=setup['contract']:
            archive=Archive(src,setup['symbol'],setup['contract'])
        chain=reconstruct_chain(archive,setup);formation=chain['fvg']['available_at']
        retest=retest_observation(archive,chain,manifest['observation_policy']['horizon_minutes'])
        touch=retest['first_available_touch'];evaluation_at=touch['available_at'] if touch else retest['observation_end']
        ranges=htf_ranges(archive,formation,chain['fvg']['lower'])
        targets=target_inventory(archive,formation,chain['fvg']['lower'])
        targets_at_retest=target_availability_at_retest(archive,targets,formation,evaluation_at)
        conditions,geometry,legs=assess_conditions(archive,setup,chain,retest,ranges,targets,source)
        status,failed,missing,unresolved=classification(conditions,source['qualification_logic']['mandatory_condition_ids'])
        # Material gap choice is an additional dependency, not an invented first-FVG test.
        if status=='SOURCE_QUALIFIED_HISTORICAL_SETUP' and next(c for c in conditions if c['condition_id']=='E6R-14')['status']!='VERIFIED':
            status='QUALIFIED_GEOMETRY_CONTEXT_UNRESOLVED';unresolved.append('E6R-14')
        chain_id=digest([setup['symbol'],chain['raid']['id'],chain['cross']['id'],chain['fvg']['id'],touch['id'] if touch else None])[:24]
        ident=digest([VERSION,setup['id'],chain_id])[:24]
        exposure=digest(['NASDAQ' if setup['symbol'] in ('NQ','MNQ') else 'SP500',formation//60,'SHORT'])[:24]
        payload={'id':ident,'candidate_id':setup['id'],'symbol':setup['symbol'],'direction':'SHORT','contract':setup['contract'],
            'model_id':source['id'],'source_map_sha256':manifest['source_map_sha256'],
            'formation_decision_at':formation,'decision_at':evaluation_at,'decision_at_ny':clock(evaluation_at).isoformat(),
            'context_target_cutoff':formation,'status':status,'chain_id':chain_id,'exposure_group':exposure,
            'source_qualified':status=='SOURCE_QUALIFIED_HISTORICAL_SETUP','execution_eligible':False,
            'llm_status':'PENDING_LLM','pf_status':'PF_NOT_ESTIMABLE','observed_chain':chain,
            'initial_assessment':{'at':formation,'retest_status':'NOT_YET_OBSERVED','source_qualified':False,
                'meaning':'Later retest does not backdate knowledge into this formation decision.'},
            'retest_observation':retest,'conditions':conditions,'geometry':geometry,'competing_htf_ranges':ranges,
            'displacement_leg_candidates':legs,'target_inventory':targets,'target_inventory_at_retest':targets_at_retest,'preferred_target':None,
            'target_distance_reference':{'price':chain['fvg']['lower'],'role':'FVG_LOWER_BOUNDARY_NOT_CERTIFIED_ENTRY',
                'equal_price_or_related_observations_may_share_one_objective':True},
            'entry':None,'stop':None,'target':None,'rr':None,
            'conditional_coordinates':{'FVG_bounds':[chain['fvg']['lower'],chain['fvg']['upper']],
                'entry_limit_formula':'C3.high + source-supported verified tick offset; branch not selected',
                'stop_C2':{'reference':chain['c2']['high'],'formula':'above C2 high','buffer':None},
                'stop_C1':{'reference':chain['c1']['high'],'formula':'above C1 / contextually selected swing high','buffer':None}},
            'failed_mandatory_conditions':failed,'missing_mandatory_data':missing,'unresolved_mandatory_conditions':unresolved,
            'source_rule_ids':source['source_rule_ids'],'execution_blockers':manifest['execution_blockers'],
            'failure_scope':'RECORDED_SELECTED_CHAIN_NOT_ALL_POSSIBLE_MARKET_SETUPS',
            'branch_decisions':{'episode6.general.intraday.short':status,
                'episode6.demonstrated.daily-range.short':'SOURCE_CONDITION_FAILED' if failed else 'CONTEXT_UNRESOLVED',
                'daily_branch_additional_dependency':'E6R-02; no universal Charter entry-window gate'},
            'selected_by':'FROZEN_ORIGINAL_ENGINEERING_SELECTOR_NOT_SOURCE_CHOICE',
            'final_selector_outcome_ignored':True,'orders_sent':0,
            'chart':make_chart(archive,chain,retest,targets,evaluation_at)}
        refs=set(i for condition in conditions for i in condition['evidence_ids'])
        refs.update(f['id'] for f in payload['chart']['frames']['1'])
        if retest['engineering_challenge_before_or_same_touch']:refs.add(retest['engineering_challenge_before_or_same_touch']['id'])
        evidence=archive.evidence(refs,evaluation_at)
        for condition in conditions:
            for ref in condition['evidence_ids']:
                if evidence[ref]['available_at']>condition['assessed_at']:raise ValueError('CONDITION_LOOKAHEAD')
                checks['condition_references']+=1
            funnel[condition['condition_id']][condition['status']]+=1
            db.execute('INSERT INTO conditions VALUES(?,?,?,?)',(ident,condition['condition_id'],condition['status'],encode(condition)))
        for e in evidence.values():
            db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)',(e['id'],e['available_at'],e.get('kind','CANDLE'),encode(e)))
            checks['saved_evidence_instances']+=1
        db.execute('INSERT INTO cases VALUES(?,?,?,?,?,?,?,?)',(ident,setup['id'],setup['symbol'],evaluation_at,status,chain_id,exposure,encode(payload)))
        exact_chains.add(chain_id);case_ids.append(ident);counts[status]+=1;counts['zone_retest_observed']+=bool(touch)
        counts['retest_verified_contiguous']+=retest['status']=='VERIFIED';counts['retest_data_unavailable']+=retest['status']=='DATA_UNAVAILABLE'
        counts['c3_condition_verified']+=geometry['c3_below_c2'];counts['c3_condition_failed']+=not geometry['c3_below_c2']
        by_condition={c['condition_id']:c for c in conditions}
        counts['pre_raid_low_geometry_verified']+=by_condition['E6R-04']['geometry_status']=='VERIFIED'
        counts['selected_structural_low_failed']+=by_condition['E6R-04']['status']=='FAILED'
        counts['selected_structural_low_order_unresolved']+=by_condition['E6R-04']['geometry_status']=='CONTEXT_UNRESOLVED'
        counts['C2_matches_original_detector_displacement']+=by_condition['E6R-06']['measurements']['C2_is_original_detector_displacement_candle']
        counts['demonstrated_M15_anchor_available']+=bool(by_condition['E6R-02']['measurements']['anchor'])
        counts['raid_uses_demonstrated_M15_anchor']+=by_condition['E6R-02']['measurements']['raid_uses_this_exact_anchor']
        counts['competing_premium_discount_ranges']+=len({r['price_location'] for r in ranges} & {'PREMIUM','DISCOUNT'})==2
        counts['formation_targets_available']+=bool(targets)
        counts['formation_target_inventory_empty']+=not targets
        counts['cases_with_target_consumed_by_retest']+=any(t['consumed_before_or_on_retest'] for t in targets_at_retest)
        counts['cases_with_partial_opposing_FVG_at_formation']+=any(t.get('upper_boundary_touched_asof') for t in targets)
        counts['c3_and_contiguous_retest']+=geometry['c3_below_c2'] and retest['status']=='VERIFIED'
        decidable=(by_condition['E6R-03']['geometry_status']=='VERIFIED' and by_condition['E6R-04']['geometry_status']=='VERIFIED'
                   and all(by_condition[c]['status']=='VERIFIED' for c in ('E6R-05','E6R-07','E6R-09','E6R-10','E6R-11','E6R-12')))
        counts['decidable_mandatory_geometry_verified']+=decidable
        counts['decidable_geometry_and_contiguous_retest']+=decidable and retest['status']=='VERIFIED'
        checks['cases']+=1
        if index%50==0:
            db.commit();print(json.dumps({'case':index+1,'of':len(rows),'symbol':setup['symbol'],'status':status}),flush=True)
    for key,value in checks.items():db.execute('INSERT INTO validation VALUES(?,?)',(key,value))
    db.commit()
    if sha(source_path)!=manifest['source_map_sha256'] or sha(frequency_path)!=manifest['frequency_database_sha256']:
        raise ValueError('FROZEN_INPUT_CHANGED')
    if sha(__file__)!=manifest['qualification_code_sha256']:raise ValueError('QUALIFICATION_RUNTIME_CHANGED')
    condition_funnel=[{'condition_id':c['id'],'title':c['title'],'requirement':c['requirement'],'scope':c['scope'],
                      'counts':{s:funnel[c['id']][s] for s in sorted(STATUSES)}} for c in source['conditions']]
    summary={'version':VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),'manifest_sha256':sha(output/'manifest.json'),
        'source_map_sha256':manifest['source_map_sha256'],'source_map_status':source['classification'],
        'population_counts':{'short_selector_candidates':1931,'complete_engineering_chains':544,
             'incomplete_engineering_sequences':1931-544,'exact_unique_raid_cross_fvg_retest_chains':len(exact_chains),
             'correlated_exposure_clusters':db.execute('SELECT count(distinct exposure_group) FROM cases').fetchone()[0],
             'zone_retests_observed':counts['zone_retest_observed'],'zone_retests_verified_contiguous':counts['retest_verified_contiguous'],
             'retests_data_unavailable':counts['retest_data_unavailable'],'c3_low_condition_verified':counts['c3_condition_verified'],
             'c3_low_condition_failed':counts['c3_condition_failed'],'source_qualified':counts['SOURCE_QUALIFIED_HISTORICAL_SETUP'],
             'c3_and_contiguous_retest':counts['c3_and_contiguous_retest'],
             'decidable_mandatory_geometry_verified':counts['decidable_mandatory_geometry_verified'],
             'decidable_geometry_and_contiguous_retest':counts['decidable_geometry_and_contiguous_retest'],
             'context_resolved':0,'fully_defined_source_entry_sl_tp':0,'simulation_eligible':0,'simulated_trades':0},
        'classification_counts':{k:counts[k] for k in ('SOURCE_QUALIFIED_HISTORICAL_SETUP','QUALIFIED_GEOMETRY_CONTEXT_UNRESOLVED','SOURCE_CONDITION_FAILED','INSUFFICIENT_SOURCE_OR_MARKET_DATA')},
        'condition_funnel':condition_funnel,'validation':dict(checks),'case_ids':case_ids,
        'intersection_definitions':{
            'c3_and_contiguous_retest':'C3.low<C2.low AND later own-M1 zone overlap with no intervening missing bars.',
            'decidable_mandatory_geometry_verified':'Raid geometry + pre-raid low geometry + E6R05/07/09/10/11/12 numerical conditions. Excludes unresolved source discretion, not source-qualified.',
            'decidable_geometry_and_contiguous_retest':'Previous geometry intersection AND verified contiguous zone overlap. Not a fill or fully qualified source return-entry.'},
        'measured_context_differences':{k:counts[k] for k in (
            'pre_raid_low_geometry_verified','selected_structural_low_failed','selected_structural_low_order_unresolved',
            'C2_matches_original_detector_displacement','demonstrated_M15_anchor_available','raid_uses_demonstrated_M15_anchor',
            'competing_premium_discount_ranges','formation_targets_available','formation_target_inventory_empty',
            'cases_with_target_consumed_by_retest','cases_with_partial_opposing_FVG_at_formation')},
        'by_instrument':[{'symbol':s,'cases':n} for s,n in db.execute('SELECT symbol,count(*) FROM cases GROUP BY symbol')],
        'pf_status':'PF_NOT_ESTIMABLE','pf':None,'win_rate':None,'average_r':None,'expectancy':None,'max_drawdown':None,
        'limitations':[source['identity_warning'],
            '544 selected engineering chains are examined without broadening or selecting by later profit;1387 incomplete selector sequences are not declared universally invalid ICT setups.',
            'All price context and target inventories are frozen at selected FVG formation; a later retest is a separate chronological observation.',
            'C3.low<C2.low is independently source-tested, not assumed from generic bearish FVG.',
            'Source displacement quality, meaningful liquidity reference, structural relevance and HTF narrative are not certified by numerical detector thresholds.',
            'C2/C1 stops and entry/target alternatives are retained. Missing buffers and context are not filled with engineering defaults.',
            'The90-minute observation limit is inherited engineering scope, not Episode6 expiry. No retest within it is not source failure.',
            'All historical archive was previously exposed to research; no untouched OOS profitability claim.',
            'No brokerage, paid LLM, production configuration, source frequency database or active observer code changed.'],
        'paid_calls':0,'orders_sent':0}
    immutable_json(output/'summary.json',summary)
    db.execute('INSERT INTO metadata VALUES(?,?)',('completed',encode({'at':summary['generated_at'],'manifest_sha256':summary['manifest_sha256']})))
    db.commit();db.close();src.close()
    immutable_json(output/'publication.json',{'manifest_sha256':summary['manifest_sha256'],'source_map_sha256':manifest['source_map_sha256'],
         'summary_sha256':sha(output/'summary.json'),'database_sha256':sha(output/'qualification.sqlite'),
         'qualification_code_sha256':manifest['qualification_code_sha256'],'frequency_database_unchanged':True})
    return summary
