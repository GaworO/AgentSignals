"""Exercise the actual agent callback without starting its web server/workers.

Only the broker snapshot is synthetic. Do not inject callback global names:
that would mask missing imports in the production execution path.
"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import builder_broker_sync


def production_budget():
    source = Path(__file__).resolve().parents[1] / 'agent.py'
    tree = ast.parse(source.read_text())
    callback = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_directional_live_budget')
    namespace = {'os': os}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[callback.name]


@pytest.fixture
def budget(monkeypatch):
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_MODE', 'EQUITY_PERCENT')
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT', '0.5')
    sync = SimpleNamespace(enabled=True, current=True, equity=48694.75)
    sync.state = lambda: {'current': sync.current}
    sync.snapshot = lambda: {'account': {'balance': {'netLiq': sync.equity}}}
    monkeypatch.setattr(builder_broker_sync, 'instance', lambda: sync)
    return production_budget(), sync


def test_actual_live_callback_computes_current_equity_budget(budget):
    callback, sync = budget
    assert callback({'plan': 'builder50'}) == 243.47
    sync.equity = 47000
    assert callback({'plan': 'builder50'}) == 235.0


@pytest.mark.parametrize('field', ['enabled', 'current'])
def test_disconnected_or_stale_broker_still_blocks(budget, field):
    callback, sync = budget
    setattr(sync, field, False)
    assert callback({'plan': 'builder50'}) == 0.0


@pytest.mark.parametrize('equity', [None, 0, -1, True, '48694.75', float('nan'), float('inf')])
def test_unknown_equity_never_falls_back_to_fixed_budget(budget, equity):
    callback, sync = budget
    sync.equity = equity
    assert callback({'plan': 'builder50'}) == 0.0


@pytest.mark.parametrize('pct', ['0', '-0.5', '0.51', 'NaN', ''])
def test_invalid_percentage_still_blocks(budget, monkeypatch, pct):
    monkeypatch.setenv('AB_DIRECTIONAL_RISK_PCT', pct)
    assert budget[0]({'plan': 'builder50'}) == 0.0
