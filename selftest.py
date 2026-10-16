"""Development smoke test for the index + search engine (not a deliverable)."""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filescout.config import Config
from filescout.indexer import Index
from filescout.search import SearchOptions, run_search

BASE = tempfile.mkdtemp(prefix="fctest_")
HOME = os.path.join(BASE, "home")
os.environ["FILESCOUT_HOME"] = HOME

# ---- build a test tree -----------------------------------------------------
TREE = {
    "docs": {
        "readme.txt": "hello world\n",
        "notes.md": "# notes\nsome TODO items\n",
        "report.pdf": "PDF-ish binary \x00\x01",
        "archive.zip": "PK\x03\x04 fake zip",
        "deep": {
            "deeper": {
                "buried.txt": "deep TODO content here\n",
            },
        },
    },
    "pics": {
        "cat.JPG": "\xff\xd8\xff fake jpeg",
        "dog.png": "\x89PNG fake",
        "logo.svg": "<svg/>",
    },
    "code": {
        "main.py": "print('hi')\n",
        "utils.py": "def f(): pass\n",
        "app.js": "console.log(1)\n",
    },
    "empty_dir": {},
    "todo.txt": "top level TODO\n",
    "TODO-list.txt": "another todo\n",
}
NOW = time.time()


def build_tree(base, tree):
    for name, val in tree.items():
        path = os.path.join(base, name)
        if isinstance(val, dict):
            os.makedirs(path, exist_ok=True)
            build_tree(path, val)
        else:
            with open(path, "wb") as fh:
                fh.write(val.encode("latin-1") if isinstance(val, str) else val)
            os.utime(path, (NOW - 3600 * 24 * 3, NOW - 3600 * 24 * 3))  # 3 days ago


ROOT = os.path.join(BASE, "root")
os.makedirs(ROOT)
build_tree(ROOT, TREE)

cfg = Config()
cfg.data["indexes"]["volumes"] = []
cfg.data["indexes"]["folders"] = [ROOT]
idx = Index(cfg.db_path)
n = idx.scan(cfg.roots())
print("indexed objects:", n, "| stats:", idx.stats())

fails = []


def check(query, expect_names=None, opts_kwargs=None, note=""):
    opts = SearchOptions.from_config(cfg, **(opts_kwargs or {}))
    res = run_search(idx, query, opts)
    names = sorted(r["name"] for r in res.rows)
    ok = True
    if expect_names is not None:
        ok = names == sorted(expect_names)
    status = "OK " if ok else "FAIL"
    if not ok:
        fails.append((query, names, expect_names))
    print("%s q=%-42r n=%-3d %s %s" % (status, query, len(names), names[:8], note))


# plain / wildcard
check("todo", ["todo.txt", "TODO-list.txt"])
check("TODO", ["todo.txt", "TODO-list.txt"])
check("*.py", ["main.py", "utils.py"])
check("*.PY", ["main.py", "utils.py"])
check("m?in.py", ["main.py"])
check("readme", ["readme.txt"])
check("readme.txt", ["readme.txt"])
check("e*e", [], note="anchored wildcard: starts+ends with e")
check("*e*", ["readme.txt", "notes.md", "report.pdf", "buried.txt", "empty_dir",
              "deeper", "deep", "code", "archive.zip", ROOT + "\\"],
      note="contains e")
# boolean
check("todo !list", ["todo.txt"])
check("todo | cat", ["todo.txt", "TODO-list.txt", "cat.JPG"])
check("(todo | cat) !list", ["todo.txt", "cat.JPG"])
check("todo or cat", ["todo.txt", "TODO-list.txt", "cat.JPG"])
# macros
check("folder:", ["docs", "pics", "code", "empty_dir", "deep", "deeper", ROOT + "\\"],
      note="folders only (incl. index root)")
check("file:", note="files only")
check("ext:py;js", ["main.py", "utils.py", "app.js"])
check("ext:<>py;js;md;txt", ["report.pdf", "archive.zip", "cat.JPG", "dog.png",
                             "logo.svg", "docs", "pics", "code", "empty_dir",
                             "deep", "deeper", ROOT + "\\"],
      note="ext not in list (folders have no extension)")
check("pic:", ["cat.JPG", "dog.png", "logo.svg"])
check("zip:", ["archive.zip"])
check("doc:txt", ["readme.txt", "buried.txt", "todo.txt", "TODO-list.txt"])
check("size:>10", note="size>10 bytes")
check("size:<10", note="size<10 bytes")
check("dm:past7days", note="modified in last 7 days")
check("dm:>2000-01-01", note="after 2000")
check("dm:<2000-01-01", note="nothing before 2000")
check("attrib:D", note="directories")
check("depth:1", ["docs", "pics", "code", "empty_dir", "todo.txt", "TODO-list.txt"])
check("depth:4", ["buried.txt"])
check("dupe:", [], note="no duplicate names")
check("regex:^main\\.py$", ["main.py"])
check("regex:.*\\.js$", ["app.js"])
check("path:docs", ["readme.txt", "notes.md", "report.pdf", "archive.zip",
                    "buried.txt", "deep", "deeper", "docs"], note="match path")
check("wfn:todo.txt", ["todo.txt"])
check("stem:todo", ["todo.txt", "TODO-list.txt"], note="stem = name without ext")
check("sort:size-descending", note="sort smoke")
# options
check("todo", ["todo.txt"], {"match_case": True}, "match case (TODO-list excluded)")
check("todo", ["todo.txt", "TODO-list.txt"], {"match_whole_word": True}, "whole word")
check("todo", ["todo.txt", "TODO-list.txt"], {"match_path": True}, "match path")
check("cat", ["cat.JPG"], {"match_whole_word": True}, "whole word anchored")
# content search
check("content:TODO", ["notes.md", "buried.txt", "todo.txt", "TODO-list.txt"], note="content")
check("txt content:hello", ["readme.txt"], note="name+content")

# sort verification
opts = SearchOptions.from_config(cfg)
res = run_search(idx, "sort:size-descending", opts)
sizes = [r["size"] for r in res.rows]
print("sort size desc:", sizes[:6], "OK" if sizes == sorted(sizes, reverse=True) else "FAIL")
if sizes != sorted(sizes, reverse=True):
    fails.append(("sort:size-descending", sizes, None))

# incremental rescan: add a file, rescan, find it
with open(os.path.join(ROOT, "docs", "added.md"), "w") as fh:
    fh.write("new file\n")
idx.scan(cfg.roots())
check("added.md", ["added.md"], note="after rescan")
os.remove(os.path.join(ROOT, "docs", "added.md"))
idx.scan(cfg.roots())
check("added.md", [], note="after delete + rescan")

# rebuild
idx.rebuild(cfg.roots())
check("readme", ["readme.txt"], note="after rebuild")

print()
if fails:
    print("FAILURES:")
    for f in fails:
        print("  ", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
idx.close()
shutil.rmtree(BASE, ignore_errors=True)
