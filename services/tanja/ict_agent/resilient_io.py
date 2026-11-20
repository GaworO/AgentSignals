"""Retry interrupted local reads without weakening source or JSON validation."""
import contextlib
import errno
import gzip
import json
import os
from pathlib import Path
import signal
import stat
import sys
import threading
import time


class ReadDeadlineExceeded(TimeoutError):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular_bytes(path):
    # Observed stalled descriptors were already at st_size. Avoid read-to-EOF's
    # extra syscall, but reject short reads, in-place edits and path replacement.
    with path.open('rb', buffering=0) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, 'Expected a regular local file', str(path))
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(stream.fileno(), min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError(errno.EAGAIN, 'File shortened during read', str(path))
            chunks.append(chunk)
            remaining -= len(chunk)
        if (_identity(os.fstat(stream.fileno())) != _identity(before)
                or _identity(path.stat()) != _identity(before)):
            raise OSError(errno.EAGAIN, 'File changed during read', str(path))
        return b''.join(chunks)


@contextlib.contextmanager
def _deadline(seconds):
    # CLI queue processes have no alarm of their own. Never replace a caller's
    # timer/handler or install signals in a server's background thread.
    enabled = (seconds > 0 and hasattr(signal, 'setitimer')
               and threading.current_thread() is threading.main_thread()
               and signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
               and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
    if not enabled:
        yield
        return

    def expired(signum, frame):
        raise ReadDeadlineExceeded(errno.ETIMEDOUT, 'Local file read timed out')

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def read_bytes(path, *, attempts=3, timeout_seconds=10.0):
    """Reopen only a failed file; never return partial bytes or cached content."""
    path = Path(path)
    if attempts < 1:
        raise ValueError('At least one read attempt is required')
    for attempt in range(1, attempts + 1):
        try:
            with _deadline(timeout_seconds):
                return _read_regular_bytes(path)
        except OSError as exc:
            if exc.errno not in (errno.ECANCELED, errno.EINTR, errno.ETIMEDOUT, errno.EAGAIN):
                raise
            if attempt == attempts:
                raise OSError(exc.errno,
                              'ICT_FILE_READ_FAILED after %d attempts: %s' % (attempts, exc),
                              str(path)) from exc
            print('ICT_FILE_READ_RETRY %d/%d errno=%s path=%s' %
                  (attempt, attempts, exc.errno, path), file=sys.stderr, flush=True)
            time.sleep(0.25 * attempt)


def read_json(path, **kwargs):
    # Decoding, syntax errors and all semantic checks stay outside the retry.
    return json.loads(read_bytes(path, **kwargs).decode('utf-8'))


def read_gzip_json(path, **kwargs):
    return json.loads(gzip.decompress(read_bytes(path, **kwargs)).decode('utf-8'))
