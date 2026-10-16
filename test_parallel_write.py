"""Stress: 3 volume threads writing to one DB concurrently (IndexWorker shape)."""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from filescout.config import DEFAULT_EXCLUDE_FOLDERS
from filescout.indexer import Index

BASE = tempfile.mkdtemp(prefix="evstress_")
DB = os.path.join(BASE, "stress.db")
VOLUMES = []
for v in ("C", "D", "E"):
    root = os.path.join(BASE, v) + "\\"
    os.makedirs(os.path.join(root, "sub1", "sub2"))
    for i in range(200):
        with open(os.path.join(root, "sub1", "sub2", "f%03d.dat" % i), "w") as fh:
            fh.write("x" * 50)
    with open(os.path.join(root, "top.txt"), "w") as fh:
        fh.write("top")
    VOLUMES.append(root)

errors = []
results = {}
lock = threading.Lock()


def scan_volume(root):
    idx = None
    try:
        idx = Index(DB)
        n = idx.scan([root], follow_junctions=True,
                     exclude_folders=DEFAULT_EXCLUDE_FOLDERS)
        with lock:
            results[root] = (n, len(idx.errors))
    except Exception as exc:
        import traceback
        with lock:
            errors.append((root[-3:], repr(exc), traceback.format_exc()))
    finally:
        try:
            if idx is not None:
                idx.close()
        except Exception:
            pass


for round_no in range(3):
    errors.clear()
    results.clear()
    # warm up WAL/-shm from this thread before starting volume threads
    Index(DB).set_meta("warmup", "1")
    threads = [threading.Thread(target=scan_volume, args=(v,))
               for v in VOLUMES]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("round %d: %.2fs results=%s" % (round_no, time.time() - t0,
                                          {k[-3:]: v for k, v in results.items()}))
    for root, msg, tb in errors:
        print("ERROR in %s: %s" % (root, msg))
        print(tb)

# verify all rows present and consistent
idx = Index(DB)
total, _ = idx.stats()
rows = idx.conn.execute("SELECT COUNT(*) FROM files WHERE parent = ''").fetchone()[0]
per_vol = idx.conn.execute(
    "SELECT substr(parent||name,1,2) d, COUNT(*) c FROM files GROUP BY d").fetchall()
print("total=%d roots=%d per_volume=%s" % (total, rows, per_vol))
idx.close()
shutil.rmtree(BASE, ignore_errors=True)
sys.exit(1 if errors or rows != 3 else 0)
