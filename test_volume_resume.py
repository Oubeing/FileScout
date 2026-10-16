"""Tests for per-volume scanning and restart resume (skip_recent)."""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["FILESCOUT_HOME"] = os.path.join(
    tempfile.mkdtemp(prefix="fcvol_"), "home")

from filescout.config import Config
from filescout.indexer import Index

BASE = tempfile.mkdtemp(prefix="fcvol_")
FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" - " + detail) if detail else ""))
    if not ok:
        FAILURES.append(name)


def paths(index):
    return {(r["parent"] + r["name"]).lower()
            for r in index.conn.execute("SELECT parent, name FROM files")}


# two fake "volumes"
vol1 = os.path.join(BASE, "C") + "\\"
vol2 = os.path.join(BASE, "D") + "\\"
for p, f in ((vol1, "a.txt"), (vol2, "b.txt")):
    os.makedirs(os.path.join(p, "sub"))
    open(os.path.join(p, "sub", f), "w").write("x")

cfg = Config()
idx = Index(cfg.db_path)

# 1. scanning vol1 marks it done in meta
idx.scan([vol1])
check("1. vol1 content indexed", os.path.join(vol1, "sub", "a.txt").lower()
      in paths(idx))
check("1b. root_done recorded for vol1",
      idx.get_meta("root_done:" + vol1.lower()) is not None)

# 2. a second scan with skip_recent skips the volume entirely
before = paths(idx)
n = idx.scan([vol1], skip_recent=300)
check("2. skip_recent skips finished volume", n == 0, "n=%d" % n)
check("2b. rows untouched by skipped scan", paths(idx) == before)

# 3. force (skip_recent=None) re-walks it
n = idx.scan([vol1], skip_recent=None)
check("3. forced rescan re-walks volume", n > 0, "n=%d" % n)

# 4. vol2 is independent and gets scanned after vol1
idx.scan([vol2])
have = paths(idx)
check("4. vol2 indexed independently of vol1",
      os.path.join(vol2, "sub", "b.txt").lower() in have
      and os.path.join(vol1, "sub", "a.txt").lower() in have)

# 5. an interrupted scan does NOT mark the volume done
stop = threading.Event()
os.makedirs(os.path.join(vol2, "deep", "x"))
idx.conn.execute("DELETE FROM meta WHERE key = ?", ("root_done:" + vol2.lower(),))
idx.conn.commit()


class StoppingIndex(Index):
    """Stops the walk after the first directory to simulate closing the app."""
    pass


# simulate interruption: stop before the stack is exhausted
orig_scan_dir = Index._scan_dir
calls = {"n": 0}


def stopping_scan_dir(self, d, ef, ex, fj, progress, stop_event, visited,
                      skip_unchanged=False):
    calls["n"] += 1
    result = orig_scan_dir(self, d, ef, ex, fj, progress, stop_event, visited,
                           skip_unchanged=skip_unchanged)
    if calls["n"] >= 2:
        stop_event.set()
    return result


Index._scan_dir = stopping_scan_dir
try:
    idx.scan([vol2], stop=stop)
finally:
    Index._scan_dir = orig_scan_dir
check("5. interrupted volume not marked done",
      idx.get_meta("root_done:" + vol2.lower()) is None)

# 6. after interruption, skip_recent still re-walks the volume
n = idx.scan([vol2], skip_recent=300)
check("6. interrupted volume is re-walked next time", n > 0, "n=%d" % n)
check("6b. volume marked done after full walk",
      idx.get_meta("root_done:" + vol2.lower()) is not None)

idx.close()
shutil.rmtree(BASE, ignore_errors=True)
print("\n%s" % ("ALL TESTS PASSED" if not FAILURES else "FAILED: %s" % FAILURES))
sys.exit(1 if FAILURES else 0)
