"""Private, operator-reported balance; never asserts broker synchronization."""
from datetime import datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')


def save_snapshot(directory, body, now):
    base={'balance','minimum_balance','target_balance'}
    extra={'daily_loss_remaining','committed_risk_usd','committed_micro_contracts'}
    if not isinstance(body, dict) or set(body) not in (base,base|extra):
        raise ValueError('Balance, loss threshold and target are required')
    for key in base:
        value=body[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1_000_000 or Decimal(str(value)) % Decimal('.01'):
            raise ValueError('Use positive dollar amounts with at most two decimal places')
    if extra<=set(body):
        for key in ('daily_loss_remaining','committed_risk_usd'):
            v=body[key]
            if type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1_000_000 or Decimal(str(v))%Decimal('.01'):
                raise ValueError('Risk amounts must be nonnegative dollars with at most two decimals')
        if body['daily_loss_remaining']>1000:raise ValueError('Builder daily allowance cannot exceed $1,000')
        if type(body['committed_micro_contracts']) is not int or not 0<=body['committed_micro_contracts']<=10000:
            raise ValueError('Committed contracts must be a nonnegative integer')
    if body['minimum_balance'] >= body['target_balance']:
        raise ValueError('Loss threshold must be below the target')
    value = dict(body, as_of=datetime.fromtimestamp(now, NY).isoformat(),
                 reported_at=now, source='daily_manual_dashboard', manual=True, broker_verified=False)
    directory=Path(directory)
    # Unique temporary file plus atomic replacement: a reader never sees half JSON.
    fd, name=tempfile.mkstemp(prefix='.account-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, allow_nan=False);out.flush();os.fsync(out.fileno())
        os.replace(name, directory/'account_snapshot.json')
    finally:
        if os.path.exists(name):os.unlink(name)
    return value


def snapshot_current(snapshot, now):
    if not snapshot:return False
    try:
        reported=snapshot.get('reported_at')
        if reported is None:
            when=datetime.fromisoformat(snapshot['as_of'])
            if when.tzinfo is None:return False
            reported=when.timestamp()
        if type(reported) not in (int,float) or not math.isfinite(reported) or not 0<=reported<=now:return False
        return datetime.fromtimestamp(reported,NY).date()==datetime.fromtimestamp(now,NY).date()
    except (ValueError,TypeError,KeyError,OverflowError):
        return False
