"""Before/after benchmark: old everything/ indexer vs new filescout/ indexer.

Runs on a *copy* of node_modules (12.4k files / 907 dirs) so the live runtime
tree is never touched.
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OLD = r"E:\zone2\everything-py"
SRC = (r"C:\Users\Admin\.workbuddy\binaries\node\versions"
       r"\22.22.2-6\lib\node_modules")

WORK = tempfile.mkdtemp(prefix="fcbench_")
TREE = os.path.join(WORK, "node_modules")
print("copying benchmark tree ...")
shutil.copytree(SRC, TREE)
print("tree ready: %s" % TREE)


def count_tree(root):
    n = d = 0
    for _dp, dn, fn in os.walk(root):
        d += 1
        n += len(fn)
    return d, n


# --- old implementation -------------------------------------------------
os.environ["EVERYTHING_PY_HOME"] = os.path.join(WORK, "old_home")
sys.path.insert(0, OLD)
from everything.indexer import Index as OldIndex  # noqa: E402

old_db = os.path.join(os.environ["EVERYTHING_PY_HOME"], "bench.db")
t0 = time.perf_counter()
oi = OldIndex(old_db)
oi.scan([TREE])
old_first = time.perf_counter() - t0
old_n = oi.stats()[0]
t0 = time.perf_counter()
oi.scan([TREE])
old_rescan = time.perf_counter() - t0
oi.close()

# --- new implementation -------------------------------------------------
for mod in ("everything.indexer", "everything.util", "everything"):
    sys.modules.pop(mod, None)
sys.path.remove(OLD)
os.environ["FILESCOUT_HOME"] = os.path.join(WORK, "new_home")
os.environ.pop("EVERYTHING_PY_HOME")
from filescout.indexer import Index as NewIndex  # noqa: E402

new_db = os.path.join(os.environ["FILESCOUT_HOME"], "bench.db")


def timed(label, fn):
    t0 = time.perf_counter()
    result = fn()
    print("%-28s %6.2fs" % (label, time.perf_counter() - t0))
    return result


ni = NewIndex(new_db)
timed("new: first scan", lambda: ni.scan([TREE]))
new_n = ni.stats()[0]
print("  timings=%s writes=%s" % (ni.last_timings, ni.write_stats))
timed("new: rescan (full walk)", lambda: ni.scan([TREE]))
print("  timings=%s writes=%s" % (ni.last_timings, ni.write_stats))
timed("new: rescan (skip mode)", lambda: ni.scan([TREE], skip_unchanged=True))
print("  timings=%s writes=%s skipped_dirs=%d"
      % (ni.last_timings, ni.write_stats, ni.skipped_dirs))
timed("new: rescan (skip mode #2)",
      lambda: ni.scan([TREE], skip_unchanged=True))

# touch 10 files inside the COPY, then rescan in incremental mode
touched = []
for dp, dn, fn in os.walk(TREE):
    for f in fn[:1]:
        p = os.path.join(dp, f)
        with open(p, "a") as fh:
            fh.write("x")
        touched.append(p)
    if len(touched) >= 10:
        break
timed("new: rescan (10 files touched)",
      lambda: ni.scan([TREE], skip_unchanged=True))
print("  timings=%s writes=%s skipped_dirs=%d"
      % (ni.last_timings, ni.write_stats, ni.skipped_dirs))
# restore the touched files (they are copies, but keep the bench idempotent)
for p in touched:
    with open(p, "r+b") as fh:
        fh.truncate(os.path.getsize(p) - 1)
timed("new: rescan after restore",
      lambda: ni.scan([TREE], skip_unchanged=True))
ni.close()

print()
print("objects: old=%d new=%d (%s)" % (old_n, new_n, count_tree(TREE)))
print("first scan     old=%6.2fs  new=%6.2fs  (%.1fx)"
      % (old_first, old_first and 0 or 0, 0, 0) if False else
      "first scan     old=%6.2fs" % old_first)
print("rescan (walk)  old=%6.2fs" % old_rescan)

for _key in ("FILESCOUT_HOME", "EVERYTHING_PY_HOME"):
    _home = os.environ.get(_key)
    if _home and os.path.isdir(_home):
        shutil.rmtree(_home, ignore_errors=True)
if os.path.isdir(WORK):
    shutil.rmtree(WORK, ignore_errors=True)
print("cleaned up %s" % WORK)
