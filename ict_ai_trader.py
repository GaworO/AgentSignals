"""Agent50k AI ICT Trader mount. Does not import a broker or mutate existing journals."""
import base64
import hmac
import os
from pathlib import Path


def render_shadow(html):
    from services.tanja.ict_agent.common import read_bytes
    fragment=Path(__file__).parent/'services/tanja/ict_agent/static/view.html'
    knowledge=fragment.with_name('knowledge.html')
    from services.tanja.ict_agent.shadow_panel import render
    return html.replace('<!--ICT_AI_TRADER_VIEW-->',render(read_bytes(fragment).decode())).replace('<!--ICT_KNOWLEDGE_VIEW-->',read_bytes(knowledge).decode())


def register(app, config=None, *, start=True, authorize=None):
    from flask import request, Response
    from services.tanja.ict_agent.integrated_web import blueprint
    cfg=dict(os.environ if config is None else config)
    # Deployment-owned private configuration contains no credentials. The key
    # reference explicitly opts this separately budgeted researcher into one
    # existing server environment secret; never inferred from key presence.
    if cfg.get('DATA_DIR') or config is None:
        from services.tanja.ict_agent.common import read_json
        file=Path(cfg.get('DATA_DIR','/data/builder50'))/'ict_ai/runtime-config.json'
        if file.is_file():
            settings=read_json(file)
            if any(not k.startswith('ICT_AI_') or k=='ICT_AI_API_KEY' for k in settings):raise ValueError('INVALID_ICT_PRIVATE_CONFIG')
            cfg.update(settings)
            if cfg.get('ICT_AI_API_KEY_ENV')=='OPENAI_API_KEY':cfg['ICT_AI_API_KEY']=cfg.get('OPENAI_API_KEY','')
    if 'ict_ai_trader' in app.blueprints:return app.blueprints['ict_ai_trader']
    def fallback_authorize():
        password=cfg.get('AB_CONTEXT_SOURCE_PASSWORD') or cfg.get('TANJA_DASHBOARD_PASSWORD','')
        expected='Basic '+base64.b64encode(('tanja:'+password).encode()).decode()
        return bool(password) and hmac.compare_digest(request.headers.get('Authorization',''),expected)
    bp=blueprint(cfg,authorize or fallback_authorize)
    app.register_blueprint(bp,url_prefix='/ict-shadow/ai-trader')
    # Read-only compatibility for saved API URLs, with the SAME authentication.
    app.register_blueprint(bp,url_prefix='/ict-ai-trader',name='ict_ai_trader_legacy')
    if start and cfg.get('ICT_AI_MODE','OFF') in ('OBSERVE','SHADOW_AI'):
        from services.tanja.ict_agent.runtime import Worker
        worker=Worker(cfg); app.extensions['ict_ai_trader_worker']=worker;worker.start()
    return bp
