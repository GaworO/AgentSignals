"""Daily Railway-local backup rotation, no API calls or trading permissions.

One verified compressed generation, at/after 17:10 America/New_York. Durable
completion and retries survive restarts; missed start time catches up later on
the same NY date. Three attempts maximum, ten minutes apart. A volume lock
serializes multiple app workers. The archive's own manifest reconciles a crash
between successful replacement and writing the scheduler's status.
"""
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import tarfile
import threading
import time
from zoneinfo import ZoneInfo

from backup_retention import create_backup

NY = ZoneInfo('America/New_York')
HOUR, MINUTE = 17, 10
RETRY_SECONDS = 600
MAX_ATTEMPTS = 3
_THREAD = None
_THREAD_LOCK = threading.Lock()


def due_day(now):
    dt = datetime.fromtimestamp(now, NY)
    return dt.date().isoformat() if (dt.hour, dt.minute) >= (HOUR, MINUTE) else None


def archive_completed_today(root, day):
    path = root / 'backups/recovery-current.tar.gz'
    if not path.is_file() or path.is_symlink():
        return False
    # create_backup publishes this name only after full checksum verification.
    with tarfile.open(path, 'r:gz') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
    stamp = manifest.get('created_at')
    return isinstance(stamp, (float, int)) and due_day(stamp) == day


def save_status(path, state):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w') as f:
        json.dump(state, f, sort_keys=True)
        f.flush(); os.fsync(f.fileno())
    os.replace(temporary, path)


class DailyBackups:
    def __init__(self, root, backup=create_backup):
        self.root = Path(root)
        self.path = self.root / 'backup_daily.json'
        self.backup = backup

    def tick(self, now=None):
        now = time.time() if now is None else now
        day = due_day(now)
        if day is None:
            return {'status': 'WAITING', 'schedule': '17:10 America/New_York'}
        with (self.root / '.backup_daily.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'BUSY'}
            state = json.loads(self.path.read_text()) if self.path.exists() else {}
            if state.get('day') != day:
                state = {'day': day, 'attempts': 0}
            artifact = self.root / 'backups/recovery-current.tar.gz'
            if state.get('status') == 'SUCCESS' and artifact.is_file():
                return state
            # Do not retry a successfully published archive after a process crash.
            try:
                complete = archive_completed_today(self.root, day)
            except (OSError, ValueError, KeyError, tarfile.TarError):
                complete = False
            if complete:
                state.update(status='SUCCESS', checked_at=now, recovered_from_archive=True)
                save_status(self.path, state)
                return state
            if state.get('status') == 'SUCCESS':
                state['attempts'] = 0  # missing completed artifact needs replacement
            if state.get('attempts', 0) >= MAX_ATTEMPTS:
                return state
            if now - state.get('last_attempt', 0) < RETRY_SECONDS:
                return state
            state.update(status='RUNNING', attempts=state.get('attempts', 0) + 1, last_attempt=now)
            save_status(self.path, state)
            try:
                result = self.backup(self.root)
                if result.get('verified') is not True:
                    raise ValueError('Backup verification not confirmed')
                state.update(status='SUCCESS', completed_at=time.time(), backup=result)
                state.pop('error', None)
                print('[backup-daily] verified compressed backup replaced; retention=1', flush=True)
            except Exception as exc:
                # Keep the prior archive. Never repair/delete live databases here.
                state.update(status='FAILED', error=type(exc).__name__)
                print('[backup-daily] backup failed; previous copy retained; attempt='+str(state['attempts']), flush=True)
            save_status(self.path, state)
            return state

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                # Storage/error-publication failure must not kill maintenance.
                print('[backup-daily] maintenance unavailable: '+type(exc).__name__+'; retrying', flush=True)
            time.sleep(60)


def start(root):
    global _THREAD
    with _THREAD_LOCK:
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=DailyBackups(root).run, name='daily-backup', daemon=True)
            _THREAD.start()
            print('[backup-daily] scheduled 17:10 America/New_York; one compressed backup', flush=True)
    return _THREAD
