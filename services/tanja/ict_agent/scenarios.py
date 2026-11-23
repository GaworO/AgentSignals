"""Competing hypotheses, driven by observations; no concept popularity vote."""
from .common import digest
from .store import transition

TERMINAL={'CLOSED','INVALIDATED','EXPIRED','REJECTED','CANCELLED'}
POLICY={
    'id':'exploratory-sweep-sequence-v1','scope':'RESEARCH_HYPOTHESIS_NOT_VERIFIED_COMPLETE_ICT_MODEL',
    'sequence':['RECLAIM_OF_M5_OR_HIGHER_CONFIRMED_LEVEL','LATER_DISPLACEMENT','LATER_CLOSE_BREAK','CLOSED_FVG'],
    'expiry_minutes':90,'entry':'near FVG edge after confirmation','stop':'sweep extreme plus one tick',
    'target':'nearest already confirmed unconsumed opposing pivot',
    'warning':'These are explicit research choices, not combined parameters from different ICT films.',
    'no_calibrated_confidence':True}

def make_setup(e):
    ident=digest(['opportunity',e['symbol'],e['contract'],e['available_at'],e['direction']])[:24]
    d=e['direction'];opposite='LONG' if d=='SHORT' else 'SHORT'
    return dict(id=ident,opportunity_id=ident,root_event=e['id'],symbol=e['symbol'],contract=e['contract'],
        direction=d,state='DISCOVERED',created_at=e['available_at'],updated_at=e['available_at'],
        expires_at=e['available_at']+POLICY['expiry_minutes']*60,sweep_price=e['price'],sweep_extreme=e['extreme'],
        supporting=[e['id']],correlated_root_events=[e['id']],contradicting=[],sequence=[],missing=['displacement','structure_break','closed_fvg','target'],
        plan=None,source_rule_ids=[],model_families=[],research_policy=POLICY['id'],
        hypotheses=[
            dict(id=ident+':reversal',kind='REVERSAL',direction=d,status='PLAUSIBLE',support=[e['id']],contradictions=[],next='Oczekuj displacement, przełamania struktury i zamkniętego FVG w kierunku odwrócenia.'),
            dict(id=ident+':continuation',kind='CONTINUATION',direction=opposite,status='PLAUSIBLE',support=[],contradictions=[e['id']],next='Zamknięcie poza ekstremum sweepu podważy scenariusz odwrócenia.'),
            dict(id=ident+':failure',kind='FAILED_REVERSAL',direction=opposite,status='WATCHING',support=[],contradictions=[],next='Po potwierdzeniu odwrócenia obserwuj ewentualną utratę ekstremum sweepu.'),
            dict(id=ident+':wait',kind='NO_TRADE',direction='NONE',status='PREFERRED_UNTIL_CONFIRMED',support=[],contradictions=[],next='Czekaj, dopóki kontekst, parametry źródłowe lub warunki wykonania są nierozstrzygnięte.')])

def update_symbol_setups(setups, events, context, as_of, knowledge, symbol):
    transitions=[];new_retrieval={}
    nq=context.get(symbol)
    if not nq or not nq['latest']:return transitions,new_retrieval
    last=nq['latest'];price=last['close']
    active=[s for s in setups.values() if s['state'] not in TERMINAL]
    for s in active:
        reason=None
        if s['contract']!=nq['contract']:reason='CONTRACT_CHANGE'
        elif nq['stale']:reason='STALE_MARKET'
        elif as_of>=s['expires_at']:reason='RESEARCH_EXPIRY'
        elif any(e['kind']=='DATA_GAP' and e['symbol']==symbol for e in events):reason='MISSING_PRICE_PATH'
        if reason:
            t=transition(s,'EXPIRED',as_of,reason,[])
            if t:transitions.append(t)
            continue
        if s['state']=='READY' and as_of>s['plan']['available_at']:
            p=s['plan'];stop_hit=last['high']>=p['stop'] if s['direction']=='SHORT' else last['low']<=p['stop']
            target_hit=last['low']<=p['target'] if s['direction']=='SHORT' else last['high']>=p['target']
            if stop_hit or target_hit:
                transitions.append(transition(s,'INVALIDATED' if stop_hit else 'EXPIRED',as_of,
                    'STOP_TRADED_BEFORE_ANY_AUTHORIZED_ORDER' if stop_hit else 'TARGET_ALREADY_TRADED_NO_ORDER',[last['id']]))
                continue
        opposite=(s['direction']=='SHORT' and price>s['sweep_extreme']) or (s['direction']=='LONG' and price<s['sweep_extreme'])
        if opposite and as_of>s['created_at']:
            s['contradicting'].append(last['id'])
            s['hypotheses'][0]['status']='INVALIDATED'
            s['hypotheses'][1]['status']='SUPPORTED_GEOMETRY_ONLY'
            s['hypotheses'][1]['support'].append(last['id'])
            if s['sequence']:s['hypotheses'][2]['status']='SUPPORTED_GEOMETRY_ONLY'
            transitions.append(transition(s,'INVALIDATED',as_of,'CLOSE_BEYOND_SWEEP_EXTREME',[last['id']]))
            continue
        if s['state']=='READY':continue
        relevant=[e for e in events if e['symbol']==symbol and e.get('direction') == s['direction'] and
                  e['timeframe'] in (1,5) and e['available_at']>s['created_at']] if events else []
        # Order within one OHLC candle is never reconstructed; events at the same close
        # are simultaneous observations, not an asserted intrabar sequence.
        disp=next((e for e in relevant if e['kind']=='DISPLACEMENT'),None)
        if disp and 'DISPLACEMENT' not in s['sequence']:
            s['sequence'].append('DISPLACEMENT');s['supporting'].append(disp['id']);s['displacement_at']=as_of
        br=next((e for e in relevant if e['kind']=='STRUCTURE_BREAK'),None)
        if br and 'DISPLACEMENT' in s['sequence'] and 'STRUCTURE_BREAK' not in s['sequence']:
            s['sequence'].append('STRUCTURE_BREAK');s['supporting'].append(br['id']);s['break_at']=as_of
        gap=next((e for e in relevant if e['kind']=='FVG'),None)
        if gap and 'STRUCTURE_BREAK' in s['sequence']:
            if 'FVG' not in s['sequence']:s['sequence'].append('FVG')
            if gap['id'] not in s['supporting']:s['supporting'].append(gap['id'])
            entry=gap['lower'] if s['direction']=='SHORT' else gap['upper']
            stop=s['sweep_extreme']+(.25 if s['direction']=='SHORT' else -.25)
            levels=[l for l in nq['active_levels'] if l['available_at']<=last['ts'] and
                    ((s['direction']=='SHORT' and l['side']=='LOW' and l['price']<entry) or
                     (s['direction']=='LONG' and l['side']=='HIGH' and l['price']>entry))]
            target=min(levels,key=lambda l:abs(l['price']-entry)) if levels else None
            oriented=stop>entry if s['direction']=='SHORT' else stop<entry
            if target and oriented:
                s['plan']=dict(direction=s['direction'],symbol=symbol,contract=s['contract'],entry=entry,stop=stop,
                    target=target['price'],entry_zone=[gap['lower'],gap['upper']],rr=round(abs(target['price']-entry)/abs(stop-entry),3),
                    available_at=as_of,expires_at=s['expires_at'],evidence_ids=[gap['id'],target['id'],s['root_event']],
                    order_type='LIMIT',variant=POLICY['id'],orders_enabled=False,
                    constraint='Exploratory paper proposal. Not approved source-faithful execution.')
                s['supporting'].append(target['id'])
                s['missing']=[]
                s['hypotheses'][0]['status']='GEOMETRY_SUPPORTED_CONTEXT_UNRESOLVED'
                transitions.append(transition(s,'READY',as_of,'RESEARCH_SEQUENCE_AND_PRICE_PLAN_COMPLETE',s['supporting']))
        if not s['plan']:
            s['missing']=[k.lower() for k in ('DISPLACEMENT','STRUCTURE_BREAK','FVG') if k not in s['sequence']]+['target_or_valid_stop']
        s['hypotheses'][0]['support']=list(s['supporting'])
        s['hypotheses'][0]['contradictions']=list(s['contradicting'])
        s['hypotheses'][1]['contradictions']=list(s['supporting'])
    # One opportunity per sweep. Related FVG/IFVG/Silver Bullet terms annotate it;
    # they never add independent confidence votes or duplicate orders.
    roots=[e for e in events if e['symbol']==symbol and e['kind']=='SWEEP' and max(e['timeframe'],e.get('level_timeframe',1))>=5]
    roots.sort(key=lambda e:(-max(e['timeframe'],e.get('level_timeframe',1)),e['id']))
    for e in roots:
        new=make_setup(e)
        if new['id'] in setups:
            prior=setups[new['id']]
            if e['id'] not in prior['correlated_root_events']:prior['correlated_root_events'].append(e['id'])
            continue
        if sum(s['state'] not in TERMINAL for s in setups.values())>=6:continue
        retrieval=knowledge.retrieve('liquidity sweep reversal continuation failed reversal market structure displacement fair value gap invalidation',
                                     new['direction'],('SWEEP',))
        records={r['id']:r for r in retrieval['records']}
        notes=list(retrieval['audit_notes'])
        for h in new['hypotheses']:
            terms={'REVERSAL':'liquidity sweep reversal market structure shift fair value gap',
                   'CONTINUATION':'trend continuation displacement pullback fair value gap',
                   'FAILED_REVERSAL':'failed reversal inversion invalidation failed swing',
                   'NO_TRADE':'no trade uncertainty missing confirmation news risk'}[h['kind']]
            ret=knowledge.retrieve(terms,h['direction'] if h['direction']!='NONE' else 'ANY',('SWEEP',),limit=2)
            h['source_rule_ids']=[r['id'] for r in ret['records']]
            records.update({r['id']:r for r in ret['records']});notes.extend(ret['audit_notes'])
        retrieval=dict(retrieval,records=list(records.values()),audit_notes=list({digest(n):n for n in notes}.values()))
        new['source_rule_ids']=[r['id'] for r in retrieval['records']]
        new['model_families']=sorted({f for r in retrieval['records'] for f in r['families']})
        new['source_limits']=['Curated family associations are retrieval context, not a verified execution recipe.',
                              'HTF range selection, news and contract mapping remain unresolved.']
        new['htf_context_at_creation']={tf:r for tf,r in nq['ranges'].items() if tf in ('60','240','1440')}
        setups[new['id']]=new
        transitions.append(transition(new,'WAITING_FOR_CONFIRMATION',as_of,'SWEEP_OPENS_COMPETING_HYPOTHESES',[e['id']]))
        new_retrieval[new['id']]=retrieval
    return [t for t in transitions if t],new_retrieval

def update_setups(setups, events, context, as_of, knowledge, symbols=('NQ',)):
    transitions=[];retrievals={}
    for symbol in symbols:
        scoped={i:s for i,s in setups.items() if s['symbol']==symbol}
        tr,ret=update_symbol_setups(scoped,events,context,as_of,knowledge,symbol)
        setups.update(scoped);transitions.extend(tr);retrievals.update(ret)
    return transitions,retrievals

def decide(setups, events, context, as_of):
    active=[s for s in setups.values() if s['state'] not in TERMINAL]
    ready=[s for s in active if s['state']=='READY']
    nq=context.get('NQ')
    missing=[]
    if not nq or nq['stale']:missing.append('FRESH_NQ')
    if not context.get('ES') or context['ES']['stale']:missing.append('SYNCHRONIZED_ES')
    if not nq or '240' not in nq['ranges']:missing.append('H4_WARMUP')
    if missing:decision='NO_TRADE'
    elif ready:decision='TRADE_CANDIDATE'
    elif active:decision='WAIT_FOR_CONFIRMATION'
    elif any(s.get('last_transition',{}).get('at')==as_of and s['state'] in TERMINAL for s in setups.values()):decision='CANCEL_SETUP'
    elif any(e['kind'] in ('SWEEP','IFVG','FVG','STRUCTURE_BREAK') for e in events):decision='WATCHING'
    else:decision='NO_TRADE'
    return dict(action=decision,preferred_hypothesis='NO_TRADE_UNTIL_SOURCE_CONTEXT_AND_RISK_GATES' if not ready else 'RESEARCH_CANDIDATE_PENDING_GATES',
        active_setup_ids=[s['id'] for s in active],candidate_ids=[s['id'] for s in ready],data_missing=missing,
        execution_blockers=['UNVERIFIED_DATED_CONTRACT','NO_EXECUTABLE_QUOTES','NO_RECONCILED_ACCOUNT',
                            'SOURCE_VARIANT_NOT_CERTIFIED','NEWS_FEED_UNAVAILABLE','LIVE_TRANSPORT_ABSENT'],
        commentary_origin='DETERMINISTIC_RECORDED_EVIDENCE_NOT_LLM',model_version='deterministic-observer-v1',
        explanation=f"Nowe obserwacje: {len(events)}. Aktywne okazje: {len(active)}. Kompletne badawcze plany cenowe: {len(ready)}. "
                    +('Czekamy na: '+', '.join(missing) if missing else 'Hipotezy są oceniane na podstawie zapisanej sekwencji. Kontekst źródłowy i bramki wykonania nadal wymagają potwierdzenia.'),
        evidence_ids=[e['id'] for e in events],llm_review='NOT_CALLED',live_enabled=False)
