"""Read full saved ICT evidence lazily; retain only a bounded metadata index.

No history pruning and no modification of source files. Each iterator holds one
decoded review, including its original SSE and exact packet, at a time.
"""
import json
import threading
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path

_INDEX=OrderedDict()
_LOCK=threading.RLock()
INDEX_LIMIT=512


def signature(st):
    return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)


def read(path):
    with path.open() as f:
        import os
        before=signature(os.fstat(f.fileno()))
        value=json.load(f)
        after=signature(os.fstat(f.fileno()))
    if before!=after:raise ValueError('REVIEW_CHANGED_DURING_READ')
    if not isinstance(value,dict):raise ValueError('REVIEW_NOT_OBJECT')
    return value,after


def indexed_time(path):
    key=str(path.resolve());stamp=signature(path.stat())
    with _LOCK:
        cached=_INDEX.get(key)
        if cached and cached[0]==stamp:
            _INDEX.move_to_end(key);return cached[1]
        value,stamp=read(path)
        at=value.get('requested_at') or 0
        if type(at) not in (int,float):raise ValueError('INVALID_REVIEW_TIME')
        _INDEX[key]=(stamp,at);_INDEX.move_to_end(key)
        while len(_INDEX)>INDEX_LIMIT:_INDEX.popitem(last=False)
        return at


class Records(Sequence):
    """Re-iterable ordered view, preserving len/index access without eager payloads."""
    def __init__(self,root):
        entries=[]
        paths=sorted(Path(root).glob('ict_2022_nyam_*/ICT_INDEPENDENT_NYAM_*.json'),
                     key=lambda p:p.stat().st_mtime,reverse=True)
        for path in paths:
            try:entries.append((indexed_time(path),path))
            except (OSError,ValueError):continue
        self.paths=[p for _,p in sorted(entries,key=lambda x:x[0],reverse=True)]

    def __len__(self):return len(self.paths)

    def __getitem__(self,index):
        if isinstance(index,slice):return [read(p)[0] for p in self.paths[index]]
        return read(self.paths[index])[0]

    def __iter__(self):
        for path in self.paths:
            try:value,_=read(path)
            except (OSError,ValueError):continue
            yield value
