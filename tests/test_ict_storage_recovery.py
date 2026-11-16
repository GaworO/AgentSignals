from pathlib import Path
import sqlite3
import pytest
from ict_shadow_cloud import Cloud

class StopLoop(BaseException):pass


def test_disk_full_and_error_write_failure_do_not_kill_worker(tmp_path,monkeypatch):
    cloud=Cloud.__new__(Cloud);cloud.root=tmp_path
    attempts=[];pauses=[];messages=[]
    monkeypatch.setattr("builtins.print",lambda *args,**kwargs:messages.append(str(args[0])))
    def tick():
        attempts.append(1)
        if len(attempts)==1:raise sqlite3.OperationalError('database or disk is full')
    def put(*args):raise sqlite3.OperationalError('database or disk is full')
    def pause():
        pauses.append(1)
        if len(pauses)==2:raise StopLoop()
    cloud.tick=tick;cloud.put=put;cloud.pause=pause
    with pytest.raises(StopLoop):cloud.run()
    assert len(attempts)==2 and len(pauses)==2
    assert any('persistence unavailable; retrying' in m for m in messages)


def test_lock_open_failure_backs_off_and_retries(tmp_path):
    cloud=Cloud.__new__(Cloud);cloud.root=tmp_path/'absent'
    errors=[];attempts=[]
    cloud.put=lambda *args:errors.append(args)
    cloud.tick=lambda:attempts.append(1)
    pauses=[]
    def pause():
        pauses.append(1)
        if len(pauses)==1:cloud.root.mkdir()
        else:raise StopLoop()
    cloud.pause=pause
    with pytest.raises(StopLoop):cloud.run()
    assert len(errors)==1 and len(attempts)==1


def test_other_worker_lock_never_bypassed(tmp_path):
    import fcntl
    cloud=Cloud.__new__(Cloud);cloud.root=tmp_path
    cloud.tick=lambda:pytest.fail('must not call tick while lock held')
    cloud.put=lambda *args:pytest.fail('lock contention is not a failure')
    cloud.pause=lambda:(_ for _ in ()).throw(StopLoop())
    with (tmp_path/'ict_cloud.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(StopLoop):cloud.run()
