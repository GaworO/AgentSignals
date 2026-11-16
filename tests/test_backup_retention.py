import json
from pathlib import Path
import sqlite3
import tarfile
import pytest
import backup_retention as b


def seed(root):
    root.mkdir()
    (root/'policy.json').write_text('{"risk":0.5}')
    with sqlite3.connect(root/'orders.sqlite3') as db:
        db.execute('create table orders(id integer)')
        db.execute('insert into orders values(7)')


def test_online_wal_and_single_generation(tmp_path):
    root=tmp_path/'data';seed(root)
    with sqlite3.connect(root/'orders.sqlite3') as live:
        live.execute('pragma journal_mode=WAL')
        live.execute('insert into orders values(8)');live.commit()
        a=b.create_backup(root)
        m=b.verify_archive(Path(a['path']))
        assert m['files']['data/orders.sqlite3']['method']=='sqlite_online_backup'
        assert not any(x.endswith(('-wal','-shm')) for x in m['files'])
        with tarfile.open(a['path']) as archive:
            snapshot=tmp_path/'restored.sqlite3'
            snapshot.write_bytes(archive.extractfile('data/orders.sqlite3').read())
        with sqlite3.connect(snapshot) as d:assert d.execute('select id from orders').fetchall()==[(7,),(8,)]
    (root/'policy.json').write_text('{"risk":0.4}')
    b.create_backup(root)
    assert [p.name for p in (root/'backups').iterdir() if not p.name.startswith('.')]==['recovery-current.tar.gz']
    assert (root/'policy.json').read_text()=='{"risk":0.4}'


def test_failure_keeps_previous_verified_backup(tmp_path,monkeypatch):
    root=tmp_path/'data';seed(root);result=b.create_backup(root)
    previous=Path(result['path']).read_bytes()
    def broken(*a):raise OSError('No space left')
    monkeypatch.setattr(b,'copy_source',broken)
    with pytest.raises(OSError):b.create_backup(root)
    assert Path(result['path']).read_bytes()==previous
    assert not (root/'backups/recovery-current.tar.gz.partial').exists()
    assert (root/'policy.json').exists()


def test_failed_verification_does_not_replace_previous(tmp_path,monkeypatch):
    root=tmp_path/'data';seed(root);result=b.create_backup(root)
    previous=Path(result['path']).read_bytes()
    def broken(*a):raise ValueError('checksum')
    monkeypatch.setattr(b,'verify_archive',broken)
    with pytest.raises(ValueError):b.create_backup(root)
    assert Path(result['path']).read_bytes()==previous


def test_symlink_refused_and_live_data_untouched(tmp_path):
    root=tmp_path/'data';seed(root)
    (root/'linked').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):b.create_backup(root)
    assert (root/'orders.sqlite3').exists()


def test_excludes_legacy_backups_from_snapshot(tmp_path):
    root=tmp_path/'data';seed(root)
    for name in ['backups','operator_backups']:
        (root/name).mkdir();(root/name/'old').write_text('old')
    result=b.create_backup(root);m=b.verify_archive(Path(result['path']))
    assert set(m['files'])=={'data/orders.sqlite3','data/policy.json'}
    assert (root/'backups/old').exists() # no unapproved broad deletion by helper
