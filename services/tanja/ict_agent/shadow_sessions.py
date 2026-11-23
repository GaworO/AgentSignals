"""Source windows are distinct from research observation scheduling."""
from .market import clock

OBSERVATION_WINDOWS={'LO':(120,300),'NYAM':(510,720),'NYPM':(780,960)}
WINDOWS=[
    ('CHARTER_AM',(510,660),'full-assistant-kNlySn81dmo-002'),
    ('SILVER_BULLET_LO',(180,240),'full-assistant-tRq1hyGGtl4-007'),
    ('SILVER_BULLET_NYAM',(600,660),'full-assistant-tRq1hyGGtl4-011'),
    ('SILVER_BULLET_NYPM',(840,900),'full-assistant-tRq1hyGGtl4-012')]

def session_at(at):
    local=clock(at);m=local.hour*60+local.minute
    return next((name for name,(lo,hi) in OBSERVATION_WINDOWS.items() if lo<=m<hi),'BETWEEN_WINDOWS')

def window_checks(at,formation=None):
    local=clock(at);m=local.hour*60+local.minute
    fm=None if formation is None else clock(formation).hour*60+clock(formation).minute
    return [dict(model=name,rule_id=rid,timezone='America/New_York',start_minute=lo,end_minute=hi,
                 observed_at=at,status='IN_WINDOW_TIME_ONLY' if lo<=m<hi else 'OUTSIDE_ENTRY_WINDOW',
                 formation_time_status='NOT_ESTABLISHED' if fm is None else 'IN_WINDOW_TIME_ONLY' if lo<=fm<hi else 'OUTSIDE_WINDOW',
                 source_qualified=False,boundary_policy='Half-open interval; exact boundary interpretation not promoted to entry qualification')
            for name,(lo,hi),rid in WINDOWS]

def scoped_rules(knowledge,records,symbol):
    merged={r['id']:r for r in records}
    for _,_,rid in WINDOWS:
        if rid in knowledge.verified:merged[rid]=knowledge.record(rid)
    result=[]
    for record in merged.values():
        r=dict(record);rid=r['id']
        r['instrument_scope_assessment']='RELEVANT_ONLY_EXECUTION_APPLICABILITY_UNRESOLVED'
        if rid=='full-assistant-kNlySn81dmo-002':
            r['instrument_scope_assessment']='INDEX_FUTURES_SESSION_SCOPE_ONLY'
        elif rid in {x[2] for x in WINDOWS}:
            r['instrument_scope_assessment']='WINDOW_CONTEXT_ONLY_EXAMPLE_PARAMETERS_NOT_TRANSFERRED'
        r['assessed_for_instrument']=symbol;r['execution_applicable']=False
        result.append(r)
    return result

def correlation(setup):
    # A disclosure group, not a claim of statistical independence or equivalence.
    family='NASDAQ' if setup['symbol'] in ('NQ','MNQ') else 'SP500'
    return dict(exposure_family=family,independent_opportunity=False,
                grouping_policy='Same underlying exposure; separate own-candle evidence, no confidence aggregation')
