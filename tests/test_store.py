import json
import os
import subprocess
import sys
import threading

import pytest

from store import AlreadyRunning, InstanceLock, Store, write_atomic


def test_write_atomic_never_leaves_half_a_file(tmp_path):
    p = tmp_path / "x.json"
    write_atomic(p, "old")

    class Boom(str):
        pass
    with pytest.raises(TypeError):
        write_atomic(p, 123)                       # fails while writing
    assert p.read_text() == "old" and [f.name for f in tmp_path.iterdir()] == ["x.json"]


def test_concurrent_writers(store):
    def add(k):
        for i in range(20):
            store.add_items([{"text": f"{k}-{i}"}])
    ts = [threading.Thread(target=add, args=(k,)) for k in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(store.items()) == 80


def test_items_limit_and_kv(store):
    assert store.add_items([{"text": str(i)} for i in range(5)], limit=3) == (3, 3)
    assert store.add_items([{"text": "x"}], limit=3) == (0, 3)
    store.set("k", "1"); store.set("k", "2")
    assert store.get("k") == "2" and store.get("none", "d") == "d"


def test_legacy_import(tmp_path):
    out = tmp_path / "transcripts"; (out / "agendas").mkdir(parents=True)
    (out / "agenda.json").write_text(json.dumps({"items": [{"text": "a", "from": "А"}, {"text": "b", "carried": 2}],
                                                 "pending": {"1": "del"}}))
    (out / "agendas" / "1.json").write_text(json.dumps({"created": 5.0, "points": [{"n": 1, "topic": "x"}], "used_by": "m"}))
    (out / ".telegram_offset").write_text("42")
    s = Store(tmp_path / "db")
    assert len(s.import_legacy(out)) == 3 and s.import_legacy(out) == []        # once
    assert [(x["text"], x["carried"]) for x in s.items()] == [("a", 0), ("b", 2)] and s.pending("1") == "del"
    assert s.agendas()[0]["state"] == "used" and s.get("telegram.offset") == "42"
    assert (out / "agenda.json.imported").exists()


def test_single_instance(tmp_path):
    lock = InstanceLock(tmp_path / "l").acquire()
    with pytest.raises(AlreadyRunning, match=f"pid {os.getpid()}"):
        InstanceLock(tmp_path / "l").acquire()
    code = f"import sys; sys.path.insert(0, {str(__import__('store').__file__.rsplit('/', 1)[0])!r}); " \
           f"from store import InstanceLock; InstanceLock({str(tmp_path / 'l')!r}).acquire()"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode != 0   # another process
    lock.release()
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0
