"""Private Railway service entrypoint. One Gunicorn worker, four reader threads."""
import os
from .worker import create_app,check_environment
check_environment(os.environ)
app=create_app(os.environ['ICT_SHADOW_CONFIG'],start=os.environ.get('ICT_SHADOW_ACTIVATE')=='approved-passive-only')
