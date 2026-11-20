"""Compatibility development entry mounts the native ICT Shadow integration."""
from .preview import create_preview

def create_app(path):
    app=create_preview(path)
    # Legacy read-only prototype API bookmarks. The UI remains native Shadow.
    for rule in list(app.url_map.iter_rules()):
        prefix='/ict-shadow/ai-trader/api/'
        if rule.rule.startswith(prefix):
            app.add_url_rule('/api/'+rule.rule[len(prefix):],endpoint='legacy_'+rule.endpoint,
                view_func=app.view_functions[rule.endpoint],methods=['GET'])
    return app
