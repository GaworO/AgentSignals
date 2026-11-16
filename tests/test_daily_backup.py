from datetime import datetime
from pathlib import Path
import fcntl
import io
import json
import tarfile
from zoneinfo import ZoneInfo
import pytest
import daily_backup as d


def stamp(day, clock):
    return datetime.fromisoformat(day+'T'+clock).replace(tzinfo=ZoneInfo('America/New_York')).timestamp()


def artifact(root, at):
    p=root/'backups/recovery-current.tar.gz';p.parent.mkdir(exist_ok=True)
    payload=json.dumps({'created_at':at}).encode()
    with tarfile.open(p,'w:gz') as a:
        m=tarfile.TarInfo('manifest.json');m.size=len(payload);a.addfile(m,io.BytesIO(payload))
    return p


def test_schedule_and_dst():
    for day in ['2026-03-08','2026-11-01','2026-11-16']:
        assert d.due_day(stamp(day,'17:09:59')) is None
        assert d.due_day(stamp(day,'17:10:00')) == day
    assert datetime.fromtimestamp(stamp('2026-07-01','17:10:00'),ZoneInfo('UTC')).hour==21
    assert datetime.fromtimestamp(stamp('2026-11-16','17:10:00'),ZoneInfo('UTC')).hour==22


def test_once_per_day_even_after_restart(tmp_path):
    calls=[];now=stamp('2026-11-16','17:10:00')
    def backup(root):
        calls.append(1);artifact(root,now);return {'verified':True}
    worker=d.DailyBackups(tmp_path,backup)
    assert worker.tick(now-1)['status']=='WAITING'
    assert worker.tick(now)['status']=='SUCCESS'
    assert d.DailyBackups(tmp_path,backup).tick(now+900)['status']=='SUCCESS'
    assert len(calls)==1
    assert worker.tick(now+86400)['status']=='SUCCESS'
    assert len(calls)==2


def test_restart_after_archive_publish_avoids_duplicate(tmp_path):
    now=stamp('2026-11-16','18:00:00');artifact(tmp_path,now-1200)
    worker=d.DailyBackups(tmp_path,lambda *_:pytest.fail('already completed'))
    assert worker.tick(now)['recovered_from_archive'] is True


def test_morning_manual_backup_does_not_skip_daily_rotation(tmp_path):
    now=stamp('2026-11-16','17:10:00');artifact(tmp_path,stamp('2026-11-16','10:00:00'))
    calls=[]
    worker=d.DailyBackups(tmp_path,lambda *_:calls.append(1) or {'verified':True})
    assert worker.tick(now)['status']=='SUCCESS';assert len(calls)==1


def test_retry_limit_durable_and_previous_copy_kept(tmp_path):
    now=stamp('2026-11-16','17:10:00');p=artifact(tmp_path,now-86400);original=p.read_bytes();calls=[]
    def broken(*_):calls.append(1);raise OSError('disk full')
    for seconds in [0,60,600,1200,1800,3600]:
        d.DailyBackups(tmp_path,broken).tick(now+seconds)
    assert len(calls)==3 and p.read_bytes()==original
    assert json.loads((tmp_path/'backup_daily.json').read_text())['status']=='FAILED'


def test_other_process_lock_prevents_duplicate(tmp_path):
    with (tmp_path/'.backup_daily.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        w=d.DailyBackups(tmp_path,lambda *_:pytest.fail('locked'))
        assert w.tick(stamp('2026-11-16','17:10:00'))['status']=='BUSY'


def test_unverified_result_is_not_success(tmp_path):
    w=d.DailyBackups(tmp_path,lambda *_:{'verified':False})
    assert w.tick(stamp('2026-11-16','17:10:00'))['status']=='FAILED'


def test_failed_status_write_cannot_start_backup(tmp_path,monkeypatch):
    monkeypatch.setattr(d,'save_status',lambda *_:(_ for _ in ()).throw(OSError('full')))
    w=d.DailyBackups(tmp_path,lambda *_:pytest.fail('no durable attempt'))
    with pytest.raises(OSError):w.tick(stamp('2026-11-16','17:10:00'))
