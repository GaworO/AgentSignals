"""One observer per service. Graceful close, then existing journal recovery."""
workers=1
threads=4
timeout=30
graceful_timeout=30

def worker_exit(server,worker):
    app=getattr(worker,'wsgi',None)
    extensions=getattr(app,'extensions',{})
    if extensions.get('shadow_shutdown'):extensions['shadow_shutdown'].set()
    if extensions.get('shadow_thread'):extensions['shadow_thread'].join(timeout=20)
