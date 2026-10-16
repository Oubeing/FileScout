"""Measure real parallel 3-volume scanning (mimics IndexWorker.run)."""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from filescout.config import DEFAULT_EXCLUDE_FOLDERS
from filescout.indexer import Index

DB = os.path.join(tempfile.mkdtemp(prefix="fcpar_"), "filescout.db")
ROOTS = ["C:\\", "D:\\", "E:\\"]
t0 = time.time()
lock = threading.Lock()
results = {}


def scan_volume(root):
    idx = Index(DB)
    try:
        n = idx.scan([root], follow_junctions=True,
                     exclude_folders=DEFAULT_EXCLUDE_FOLDERS)
        with lock:
            results[root] = (n, len(idx.errors), time.time() - t0)
    except Exception as exc:
        with lock:
            results[root] = ("ERROR: %s" % exc, -1, time.time() - t0)
    finally:
        idx.close()


threads = [threading.Thread(target=scan_volume, args=(r,), name="V:%s" % r)
           for r in ROOTS]
for t in threads:
    t.start()
for t in threads:
    t.join()

for root in ROOTS:
    print("%-4s -> %s" % (root, results[root]))

idx = Index(DB)
total, size = idx.stats()
print("total: %d objects in %.1fs" % (total, time.time() - t0))
per_drive = idx.conn.execute(
    "SELECT substr(parent||name,1,2) d, COUNT(*) c FROM files GROUP BY d")
for r in per_drive:
    print("  %s: %d" % (r[0], r[1]))
idx.close()
