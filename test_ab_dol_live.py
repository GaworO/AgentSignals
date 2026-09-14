from __future__ import annotations

import inspect
import json

import numpy as np
import pandas as pd

import ab_dol_live
import shadow


class Level:
    def __init__(self, id, kind, side, price, born=1, touch=20):
        self.id, self.kind, self.side, self.price = id, kind, side, price
        self.source_start, self.born, self.expires, self.touch = 0, born, 20, touch


class Engine:
    def __init__(self):
        self.n = 12
        self.ms = np.arange(self.n, dtype=np.int64) * 60_000 + 1_700_000_000_000
        self.o = np.full(self.n, 100.0)
        self.h = np.full(self.n, 100.5)
        self.l = np.full(self.n, 99.5)
        self.c = np.full(self.n, 100.0)
        self.f = pd.DataFrame({"ts": pd.to_datetime(self.ms, unit="ms", utc=True)})
        self.levels = [
            Level("source", "session", -1, 99.0, touch=1),
            Level("pdh", "day", 1, 110.0),
            Level("h1eq", "H1_equal", 1, 112.0),
        ]
        self.l[1], self.c[1], self.c[2:] = 98.5, 99.5, 101.0


def signal(engine):
    return {
        "dir": "LONG", "bos_ms": int(engine.ms[10]),
        "entry_ms": int(engine.ms[10]) + 60_000,
        "entry": 101.0, "SL": 96.0, "TP": 111.0,
        "model": "CONT", "cat": "PDH", "date": "2023-11-15",
    }


def test_attachment_is_causal_and_does_not_change_canonical_signal():
    engine = Engine()
    rows = [signal(engine), {**signal(engine), "bos_ms": int(engine.ms[9])}]
    decision_fields = ("dir", "entry", "SL", "TP", "model", "cat", "bos_ms")
    before = [tuple(row[k] for k in decision_fields) for row in rows]

    for row in rows:
        ab_dol_live.attach_metadata(row, "unused.csv", engine=engine)

    assert len(rows) == 2
    assert [tuple(row[k] for k in decision_fields) for row in rows] == before
    meta = rows[0]["_dol"]
    assert meta["metadata_status"] == "ATTACHED"
    assert meta["evaluated_at_ms"] == int(engine.ms[10]) + 60_000
    assert meta["cutoff_bar"] == 10
    assert meta["selected_dol"] is not None
    assert meta["dol_status"] == "OPEN"
    assert meta["direction_aligned_with_dol"] is True
    assert {item["id"] for item in meta["stacked_constituents"]} == {"pdh", "h1eq"}


def test_shadow_persists_dol_without_an_order_path(tmp_path, monkeypatch):
    path = tmp_path / "shadow.json"
    monkeypatch.setattr(shadow, "LOG", str(path))
    monkeypatch.setattr(shadow, "EXCLUDE", set())
    meta = {"selected_dol": "POOL|pdh", "dol_status": "OPEN"}

    assert shadow.record("A/B", "LONG", 100.0, 95.0, 110.0,
                         1_700_000_000_000, metadata=meta)
    row = json.loads(path.read_text())[0]
    assert row["dol"] == meta

    source = inspect.getsource(ab_dol_live)
    assert "requests" not in source
    assert "EXEC_WEBHOOK" not in source
    assert "exec_fx" not in source


def test_agent_attaches_only_at_persistence_after_execution_source_order():
    source = open("agent.py", encoding="utf-8").read()
    assert source.index("_exec_sibling_batch(_book_items, txt)") < source.index(
        "_save_db(_item, _itxt, code)")
    save_start = source.index("def _save_db(")
    save_end = source.index("def _entry_cancel_after_sec", save_start)
    assert "ab_dol_live.attach_metadata" in source[save_start:save_end]

