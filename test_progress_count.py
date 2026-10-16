"""Regression: the progress counter counts *scanned* objects, not written rows.

The diff-write optimisation made a re-walk of an up-to-date index write almost
nothing.  Counting written rows therefore froze the GUI progress counter on the
volume root ("C:\\ 扫描中 1") and stopped further progress signals, which left
the other volumes stuck on "等待中" in the banner.  A re-walk of an unchanged
tree must still scan - and report - every object.

Run:  python test_progress_count.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["FILESCOUT_HOME"] = os.path.join(
    tempfile.mkdtemp(prefix="fcprog_"), "home")

from filescout.config import Config  # noqa: E402
from filescout.indexer import Index  # noqa: E402

BASE = tempfile.mkdtemp(prefix="fcprog_")
FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" - " + detail) if detail else ""))
    if not ok:
        FAILURES.append(name)


# a tree just over the 2000-object progress gate
ROOT = os.path.join(BASE, "vol") + "\\"
N_DIRS, PER_DIR = 22, 100
for i in range(N_DIRS):
    d = os.path.join(ROOT, "d%02d" % i)
    os.makedirs(d)
    for j in range(PER_DIR):
        with open(os.path.join(d, "f%03d.txt" % j), "w") as fh:
            fh.write("x")
expected = 1 + N_DIRS + N_DIRS * PER_DIR   # volume root + dirs + files

cfg = Config()
idx = Index(cfg.db_path)

n = idx.scan([ROOT])
check("first scan counts every object", n == expected,
      "n=%d expected=%d" % (n, expected))

# unchanged re-walk: diff mode writes almost nothing, but the counter must
# still climb over every scanned object and keep emitting progress signals
counts = []
n2 = idx.scan([ROOT], progress=lambda d, c: counts.append(c))
check("re-walk of unchanged tree counts every object",
      n2 == expected, "n=%d expected=%d" % (n2, expected))
check("re-walk emits repeated progress signals",
      len(counts) >= 2, "signals=%d counts=%s" % (len(counts), counts[:5]))
mono = all(b >= a for a, b in zip(counts, counts[1:]))
check("progress counts are monotonic", mono, str(counts[:5]))
check("last progress signal reaches the object total",
      bool(counts) and expected - 2000 <= counts[-1] <= expected,
      "last=%s expected=%d" % (counts[-1] if counts else None, expected))

idx.close()
shutil.rmtree(BASE, ignore_errors=True)
print("\n%s" % ("ALL TESTS PASSED" if not FAILURES else "FAILED: %s" % FAILURES))
sys.exit(1 if FAILURES else 0)
