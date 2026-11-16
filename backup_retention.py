"""One compressed recovery generation; never delete or modify live source data.

Run: python backup_retention.py --root /data/builder50
Each SQLite database is captured through its online backup API. This is a
per-database consistent snapshot, not a cross-database transaction. Restore
with trading stopped, then reconcile with the broker before permitting entries.
"""
import argparse
from contextlib import closing
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import tempfile
import time

EXCLUDED = {'backups', 'operator_backups', 'lost+found', '__pycache__'}
TRANSIENT = ('-wal', '-shm', '-journal', '.lock')


def digest(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(block)
    return h.hexdigest()


def inventory(root):
    result = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED]
        for name in dirs + files:
            p = Path(folder) / name
            if p.is_symlink():
                raise ValueError('Symlink in live data: ' + str(p.relative_to(root)))
        for name in files:
            if name.endswith(TRANSIENT):
                continue
            p = Path(folder) / name
            if not stat.S_ISREG(p.stat().st_mode):
                raise ValueError('Nonregular source: ' + str(p))
            result.append(p)
    return sorted(result)


def copy_source(source, dest):
    with source.open('rb') as f:
        sqlite = f.read(16) == b'SQLite format 3\x00'
    if sqlite:
        started = time.monotonic()
        def progress(*_):
            if time.monotonic() - started > 120:
                raise TimeoutError('SQLite backup deadline')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as src:
            with closing(sqlite3.connect(dest)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=.05)
                if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('SQLite integrity check failed: ' + source.name)
        return 'sqlite_online_backup'
    # A concurrent writer must not leave a torn JSON/policy/journal file.
    for _ in range(3):
        before = source.stat()
        with source.open('rb') as src, dest.open('wb') as dst:
            for block in iter(lambda: src.read(1024 * 1024), b''):
                dst.write(block)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns):
            return 'stable_file_copy'
    raise ValueError('Source changed during copy: ' + source.name)


def verify_archive(path):
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or 'manifest.json' not in names:
            raise ValueError('Invalid archive manifest')
        manifest = json.load(archive.extractfile('manifest.json'))
        expected = manifest['files']
        if set(names) != set(expected) | {'manifest.json'} or not expected:
            raise ValueError('Archive file set mismatch')
        for member in members:
            if not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts:
                raise ValueError('Unsafe archive member')
            if member.name == 'manifest.json':
                continue
            with archive.extractfile(member) as f:
                actual = digest(f)
            if member.size != expected[member.name]['bytes'] or actual != expected[member.name]['sha256']:
                raise ValueError('Archive checksum mismatch')
        return manifest


def create_backup(root):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Invalid data root')
    backups = root / 'backups'
    if backups.is_symlink():
        raise ValueError('Symlink backup directory')
    backups.mkdir(mode=0o700, exist_ok=True)
    final = backups / 'recovery-current.tar.gz'
    partial = backups / 'recovery-current.tar.gz.partial'
    if any(p.is_symlink() for p in (final, partial, backups / '.rotation.lock')):
        raise ValueError('Symlink backup target')
    with (backups / '.rotation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A leftover partial is never a verified generation and is safe to replace.
        files = inventory(root)
        if not files:
            raise ValueError('Empty source')
        manifest = {'version': 1, 'created_at': time.time(), 'root': str(root),
                    'commit': os.environ.get('RAILWAY_GIT_COMMIT_SHA'),
                    'consistency': 'PER_DATABASE_ONLINE_BACKUP_NOT_CROSS_DATABASE_ATOMIC', 'files': {}}
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as output, tempfile.TemporaryDirectory(prefix='agent50k-backup-') as tmp:
                with tarfile.open(fileobj=output, mode='w:gz', compresslevel=1) as archive:
                    snapshot = Path(tmp) / 'snapshot'
                    for source in files:
                        method = copy_source(source, snapshot)
                        name = 'data/' + source.relative_to(root).as_posix()
                        with snapshot.open('rb') as f:
                            sha = digest(f)
                        manifest['files'][name] = {'bytes': snapshot.stat().st_size, 'sha256': sha, 'method': method}
                        info = archive.gettarinfo(str(snapshot), arcname=name)
                        info.mode = 0o600
                        with snapshot.open('rb') as f:
                            archive.addfile(info, f)
                        snapshot.unlink()
                    payload = json.dumps(manifest, sort_keys=True).encode()
                    info = tarfile.TarInfo('manifest.json'); info.size = len(payload); info.mode = 0o600
                    archive.addfile(info, io.BytesIO(payload))
                output.flush(); os.fsync(output.fileno())
            verify_archive(partial)
            os.replace(partial, final)
            fd = os.open(backups, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return {'path': str(final), 'bytes': final.stat().st_size, 'files': len(manifest['files']),
                'sqlite_databases': sum(f['method'] == 'sqlite_online_backup' for f in manifest['files'].values()),
                'verified': True, 'retention': 1}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('DATA_DIR', '/data/builder50'))
    args = parser.parse_args()
    print(json.dumps(create_backup(args.root)))
