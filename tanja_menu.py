"""Read-only menu extension. Copy this file beside the existing dashboard.py.

After `dashboard.register(app)` add:
    import tanja_menu
    tanja_menu.install(dashboard)

Set TANJA_URL on the AgentSignals service. No execution or data routes change.
"""
import json
import os
import re


def install(dashboard, url=None):
    url = (os.environ.get('TANJA_URL','') if url is None else url).strip().rstrip('/')
    if not url:
        return False
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?',url):
        raise ValueError('TANJA_URL must be an HTTPS origin without a path or credentials')
    marker='/* TANJA_MENU_V1 */'
    if marker in dashboard.PAGE:
        return True
    anchor="var frame=document.getElementById('frame')"
    if dashboard.PAGE.count(anchor) != 1 or 'var NAV=' not in dashboard.PAGE or 'STRAT' not in dashboard.PAGE:
        raise ValueError('Dashboard layout differs; inspect before installing Tanja menu')
    snippet=marker+"\nvar TANJA="+json.dumps(url)+";\n"+"""
STRAT.tanja={name:'Tanja',sub:'ES + MNQ · observation service · 50K Builder execution not connected',ext:TANJA,
 tabs:[['overview','Overview',TANJA+'/?embed=1#overview','grid'],
 ['candidates','Trade candidates',TANJA+'/?embed=1#candidates','list'],
 ['context','AI context',TANJA+'/?embed=1#context','target'],
 ['executions','50K Builder executions',TANJA+'/?embed=1#executions','book'],
 ['feed','Market data',TANJA+'/?embed=1#feed','chart']]};
NAV.splice(3,0,['Tanja',[['tanja/overview','Overview','grid'],['tanja/candidates','Trade candidates','list'],['tanja/context','AI context','target'],['tanja/executions','50K Builder executions','book'],['tanja/feed','Market data','chart']]]);
"""
    dashboard.PAGE=dashboard.PAGE.replace(anchor,snippet+'\n'+anchor)
    return True
