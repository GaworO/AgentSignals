"""Explicit, fail-closed production write interlock; independent of strategy arms."""
import os


def writes_enabled():
    return os.environ.get('LIVE_EXECUTION_ENABLED', 'false') == 'true'


def entry_blocker():
    if not writes_enabled():
        return 'live_execution_disabled'
    if os.environ.get('EXEC_KILL_SWITCH', 'false') != 'false':
        return 'execution_kill_switch'
    return None
