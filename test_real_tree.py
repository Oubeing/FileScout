"""Verify index completeness against a real large tree (site-packages)."""
import os, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["FILESCOUT_HOME"] = os.path.join(tempfile.mkdtemp(), "home")

from filescout.indexer import Index

ROOT = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries",
                    "python", "envs", "default", "Lib", "site-packages")
print("root:", ROOT, "exists:", os.path.isdir(ROOT))

idx = Index(os.path.join(os.environ["FILESCOUT_HOME"], "filescout.db"))
t0 = time.time()
n = idx.scan([ROOT])
t1 = time.time()
print("indexed %d objects in %.2fs" % (n, t1 - t0))
print("errors:", len(idx.errors), idx.errors[:3])

rows = idx.conn.execute("SELECT parent, name, is_folder FROM files").fetchall()
indexed = {(r["parent"] + r["name"]).lower().rstrip("\\") for r in rows
           if r["parent"]}  # the root row itself (parent='') is by design

walked = set()
for dirpath, dirnames, filenames in os.walk(ROOT):
    for f in filenames:
        walked.add(os.path.join(dirpath, f).lower())
    for d in dirnames:
        walked.add(os.path.join(dirpath, d).lower())

missing = walked - indexed
extra = indexed - walked
print("os.walk found: %d   index has: %d" % (len(walked), len(indexed)))
print("missing from index:", len(missing), sorted(missing)[:5])
print("phantom in index  :", len(extra), sorted(extra)[:5])

# search for entry_points.txt like the user's screenshot
from filescout.search import SearchOptions, run_search
opts = SearchOptions.from_config(type("C", (), {"search": {
    "match_case": False, "match_whole_word": False, "match_path": False,
    "regex": False, "match_diacritics": False, "duplicates": False,
    "folders_first": False, "sort": "name", "sort_ascending": True},
    "general": {"max_results": 10000, "content_max_size": 8 << 20},
    "roots": lambda self: [ROOT + "\\"]})())
res = run_search(idx, "points.txt", opts)
print("\nsearch 'points.txt' -> %d results (truncated=%s, %.1fms)"
      % (len(res.rows), res.truncated, res.took * 1000))
for r in res.rows[:10]:
    print("   ", r["name"], "|", r["parent"])
idx.close()
sys.exit(1 if (missing or extra) else 0)
