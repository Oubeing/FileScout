"""Regression tests for index completeness (the 'missing files' bug).

Verifies:
  1. deep trees whose paths exceed MAX_PATH (260) are fully indexed
  2. roots without a trailing backslash no longer break path joining
  3. one unreadable directory does not abort the whole scan
  4. junction/symlink cycles cannot loop when follow_junctions=True
  5. overlapping roots (volume + subfolder) are not indexed twice
  6. deletions (including deep ones) are detected on rescan
  7. index vs os.walk completeness on a real directory tree
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("FILESCOUT_HOME",
                      os.path.join(tempfile.mkdtemp(prefix="fctest_"), "home"))

from filescout.config import Config
from filescout.indexer import Index

BASE = tempfile.mkdtemp(prefix="evfix_")
FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" - " + detail) if detail else ""))
    if not ok:
        FAILURES.append(name)


def indexed_paths(index):
    rows = index.conn.execute("SELECT parent, name FROM files").fetchall()
    out = set()
    for r in rows:
        out.add((r["parent"] + r["name"]).lower())
    return out


def walk_files(root):
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            out.add(os.path.join(dirpath, f).lower().rstrip("\\"))
        for d in dirnames:
            out.add(os.path.join(dirpath, d).lower().rstrip("\\"))
    return out


# ---------------------------------------------------------------- test 1+2
# deep tree over MAX_PATH + root given WITHOUT trailing backslash
deep = os.path.join(BASE, "t1")
node = deep
for i in range(28):
    node = os.path.join(node, "lvl%02d" % i)
os.makedirs("\\\\?\\" + node.rstrip("\\"))
deepfile = os.path.join(node, "buried.txt")
with open("\\\\?\\" + deepfile.rstrip("\\"), "w") as fh:
    fh.write("x")

cfg = Config()
idx = Index(cfg.db_path)
n = idx.scan([deep])  # no trailing backslash on purpose
have = indexed_paths(idx)
check("1. deep (>260 char) file indexed", deepfile.lower() in have,
      "len=%d, indexed=%d" % (len(deepfile), n))
check("2. root without trailing backslash joins paths",
      (deep.lower() + "\\lvl00").lower() in have)
# path joining is correct iff every non-root row's parent ends with '\'
bad_parents = [r["parent"] for r in idx.conn.execute(
    "SELECT DISTINCT parent FROM files").fetchall()
    if r["parent"] and not r["parent"].endswith("\\")]
check("2b. no path mangling in index", not bad_parents,
      str(bad_parents[:3]))

# ---------------------------------------------------------------- test 3
# one unreadable directory must not abort the scan
t3 = os.path.join(BASE, "t3")
os.makedirs(os.path.join(t3, "good", "nested"))
os.makedirs(os.path.join(t3, "bad"))
open(os.path.join(t3, "good", "nested", "keep.txt"), "w").write("k")
open(os.path.join(t3, "bad", "hidden.txt"), "w").write("h")
open(os.path.join(t3, "top.txt"), "w").write("t")

idx2 = Index(os.path.join(os.path.dirname(cfg.db_path), "t3.db"))
orig_scandir = os.scandir
real_path = os.path.realpath


def fake_scandir(path):
    key = real_path(path).lower().replace("\\\\?\\", "")
    if key.startswith(real_path(os.path.join(t3, "bad")).lower()):
        raise PermissionError(13, "Access is denied")
    return orig_scandir(path)


os.scandir = fake_scandir
try:
    n = idx2.scan([t3])
finally:
    os.scandir = orig_scandir
have = indexed_paths(idx2)
check("3. readable files survive an unreadable sibling dir",
      os.path.join(t3, "good", "nested", "keep.txt").lower() in have
      and os.path.join(t3, "top.txt").lower() in have)
check("3b. error recorded for the unreadable dir",
      any("bad" in p.lower() for p, _r in idx2.errors), str(idx2.errors[:2]))
check("3c. scan did not raise / rollback", n >= 3, "n=%d" % n)

# ---------------------------------------------------------------- test 5
# overlapping roots must not duplicate rows
t5 = os.path.join(BASE, "t5")
os.makedirs(os.path.join(t5, "sub"))
open(os.path.join(t5, "sub", "a.txt"), "w").write("a")
idx5 = Index(os.path.join(os.path.dirname(cfg.db_path), "t5.db"))
idx5.scan([t5 + "\\", os.path.join(t5, "sub") + "\\"])
rows = idx5.conn.execute(
    "SELECT parent, name, COUNT(*) c FROM files GROUP BY parent, name "
    "HAVING c > 1").fetchall()
check("5. overlapping roots do not duplicate rows", not rows,
      str([(r['parent'], r['name']) for r in rows]))
have = indexed_paths(idx5)
check("5b. folder root row present once",
      sum(1 for p in have if p.endswith("t5\\sub")) == 1)

# ---------------------------------------------------------------- test 6
# deletions are detected, including deep ones
idx.scan([deep])
before = deepfile.lower() in indexed_paths(idx)
os.remove("\\\\?\\" + deepfile.rstrip("\\"))
idx.scan([deep])
after = deepfile.lower() in indexed_paths(idx)
check("6. deleted deep file disappears on rescan", before and not after)

# ---------------------------------------------------------------- test 7
# completeness vs os.walk on a real tree (the project directory itself)
t7 = HERE
idx7 = Index(os.path.join(os.path.dirname(cfg.db_path), "t7.db"))
idx7.scan([t7])
have = indexed_paths(idx7)
expect = walk_files(t7)
missing = {p for p in expect if p not in have}
check("7. index matches os.walk on project tree",
      not missing, "missing: %s" % sorted(missing)[:5])
extra = {p for p in have - expect if not p.endswith("\\")}
check("7b. no phantom files", not extra, "extra: %s" % sorted(extra)[:5])

# ---------------------------------------------------------------- test 4
# junction cycle protection (needs symlink privileges; skip gracefully)
t4 = os.path.join(BASE, "t4")
os.makedirs(os.path.join(t4, "a"))
open(os.path.join(t4, "a", "f.txt"), "w").write("f")
link = os.path.join(t4, "b")
made = False
try:
    os.symlink(os.path.join(t4, "a"), link, target_is_directory=True)
    made = True
except OSError:
    pass
if made:
    # also a two-node cycle: a/loop -> t4 (parent)
    try:
        os.symlink(t4, os.path.join(t4, "a", "loop"), target_is_directory=True)
        cyclic = True
    except OSError:
        cyclic = False
    idx4 = Index(os.path.join(os.path.dirname(cfg.db_path), "t4.db"))
    try:
        idx4.scan([t4], follow_junctions=True)
        have = indexed_paths(idx4)
        check("4. junction cycle did not hang/duplicate",
              any("f.txt" in p for p in have))
    except RecursionError:
        check("4. junction cycle did not hang/duplicate", False, "RecursionError")
else:
    print("SKIP 4. junction cycle test (no symlink privilege)")

for i in (idx, idx2, idx5, idx7) + ((idx4,) if made else ()):
    i.close()
shutil.rmtree(BASE, ignore_errors=True)

print("\n%s" % ("ALL TESTS PASSED" if not FAILURES else "FAILED: %s" % FAILURES))
sys.exit(1 if FAILURES else 0)
