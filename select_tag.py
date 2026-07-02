# select_tag.py — ⭐ SELECT tagger for A/B alerts (tier T4 from AB_AUDIT_6K_2026-07).
# Pure function, zero side effects: returns a tag line to prepend to the alert text, or ''.
# Historical basis (4yr, n=1,089): +0.361R/trade vs +0.195R baseline; expect ~+0.30R live.
#
# ENV (all optional):
#   SELECT_TAG=1          master switch (default ON)
#   SELECT_CATS=...       comma list of premium base catalysts
#                         default: PREMH,PREML,LH,LL,VI,BSL H1,SSL H1
#   SELECT_DAYS=2,4,5     ISO weekdays 1=Mon..5=Fri (default Tue/Thu/Fri)
#   SELECT_ALLOW_DIB=0    DIB/class-B excluded by default
#   SELECT_ALLOW_N=0      bias-opposed excluded by default (leave 0)
#   SELECT_VERBOSE=0      1 = also annotate non-SELECT alerts with the reason
import os, datetime as dt

DEF_CATS = 'PREMH,PREML,LH,LL,VI,BSL H1,SSL H1'
_PL = ['', 'Pn', 'Wt', 'Sr', 'Cz', 'Pt', 'So', 'Nd']

def _cats():
    return {c.strip() for c in os.environ.get('SELECT_CATS', DEF_CATS).split(',') if c.strip()}

def _days():
    try:
        return {int(x) for x in os.environ.get('SELECT_DAYS', '2,4,5').split(',') if x.strip()}
    except Exception:
        return {2, 4, 5}

def why_not(x, members=None):
    """[] -> qualifies as SELECT. Otherwise list of failed criteria (for logging/verbose)."""
    if os.environ.get('SELECT_TAG', '1') != '1':
        return ['off']
    fails = []
    # 1) weekday of the setup date (Tue/Thu/Fri by default)
    try:
        wd = dt.date.fromisoformat(str(x.get('date', ''))[:10]).isoweekday()
        if wd not in _days():
            fails.append('dzien ' + _PL[wd])
    except Exception:
        fails.append('data?')
    # 2) class A only (no DIB), unless explicitly allowed
    if 'DIB' in str(x.get('cat', '')) and os.environ.get('SELECT_ALLOW_DIB', '0') != '1':
        fails.append('DIB (klasa B)')
    # 3) never bias-opposed
    if str(x.get('bias_align', '?')) == 'N' and os.environ.get('SELECT_ALLOW_N', '0') != '1':
        fails.append('bias przeciwny')
    # 4) premium catalyst family (any member / any merged part qualifies)
    names = set()
    for m in (members or []):
        names.add(str(m.get('cat', '')).replace('+DIB', '').strip())
    for part in str(x.get('cat', '')).replace('+DIB', '').split('+'):
        if part.strip():
            names.add(part.strip())
    if not (names & _cats()):
        fails.append('katalizator: ' + '/'.join(sorted(names)) if names else 'katalizator?')
    return fails

def tagline(x, members=None):
    """Line to PREPEND to the alert text ('' if nothing to add)."""
    f = why_not(x, members)
    if f == ['off']:
        return ''
    if not f:
        return '⭐⭐ SELECT — sygnal premium (hist. ~2x lepszy od sredniej). BIERZ.\n'
    if os.environ.get('SELECT_VERBOSE', '0') == '1':
        return 'zwykly sygnal (nie-SELECT: ' + ', '.join(f) + ')\n'
    return ''

if __name__ == '__main__':
    # self-test
    ok = dict(date='2026-07-02', cat='PREML', bias_align='?')            # Thu, premium -> star
    bad1 = dict(date='2026-07-06', cat='PREML', bias_align='?')          # Monday
    bad2 = dict(date='2026-07-02', cat='F.P.FVG', bias_align='?')       # weak catalyst
    bad3 = dict(date='2026-07-02', cat='PREML+DIB', bias_align='?')     # DIB
    bad4 = dict(date='2026-07-02', cat='PREML', bias_align='N')          # counter-bias
    conf = dict(date='2026-07-03', cat='NYPMH + LL', bias_align='Y')     # Fri, confluence incl London -> star
    for nm, x in (('ok', ok), ('mon', bad1), ('cat', bad2), ('dib', bad3), ('biasN', bad4), ('confl', conf)):
        print(nm, '->', why_not(x) or 'SELECT', '|', repr(tagline(x)))
