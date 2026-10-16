"""Filesystem index backed by SQLite.

Design notes
------------
* Every indexed object is one row in ``files``.  ``parent`` always stores the
  *containing directory* including the trailing backslash (``C:\\Windows\\``),
  so the full path is simply ``parent || name``.  Volume roots themselves are
  stored as rows with ``parent = ''`` (``C:\\``), which makes them searchable
  like any other folder.
* Each thread gets its own SQLite connection (WAL mode), so the background
  indexer never blocks searches.
* **Robustness against missing files**:
  - the walk is an explicit stack, not recursion, so arbitrarily deep trees
    (nested ``node_modules`` etc.) cannot overflow the Python stack;
  - a directory that cannot be listed or a file whose metadata cannot be read
    is recorded in :attr:`errors` and skipped instead of aborting the whole
    scan (an aborted scan used to roll the entire transaction back, silently
    losing everything indexed so far);
  - roots are normalized (trailing backslash) and de-duplicated, so a folder
    root that is already covered by a volume root is not walked twice (which
    used to duplicate rows);
  - when ``follow_junctions`` is on, visited real paths are remembered so
    junction cycles cannot loop forever.
* **Speed** (the 2026-10 rework):
  - writes are buffered per thread and flushed as one transaction every 64
    changed directories (or 0.4 s), instead of committing once per directory -
    with three volume threads walking in parallel this cuts both commit
    overhead and write-lock contention by orders of magnitude;
  - a re-walked directory only rewrites the rows that actually changed
    (new names inserted, changed rows updated, vanished names deleted), so a
    rescan of an unchanged tree performs almost no writes at all;
  - a directory whose modification time is unchanged since the previous walk
    is not re-scanned at all during periodic rescans.  Entry *names* stay
    exactly correct (any add/remove/rename updates the directory mtime);
    only size/date/attribute columns of files whose *content* changed can lag
    until that directory is next walked - a full metadata refresh is forced
    once per hour per root, and Force Rebuild (Shift+F5) always walks
    everything.
* The original tool uses the NTFS USN journal for change notifications; this
  implementation uses periodic rescans plus "Force Rebuild" instead (see
  README, "与原版的差异").
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from .util import match_exclude

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    parent        TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    is_folder     INTEGER NOT NULL DEFAULT 0,
    size          INTEGER NOT NULL DEFAULT 0,
    date_modified REAL,
    date_created  REAL,
    date_accessed REAL,
    attributes    INTEGER NOT NULL DEFAULT 0,
    extension     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_name   ON files(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_ext    ON files(extension);
CREATE INDEX IF NOT EXISTS idx_files_size   ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_dm     ON files(date_modified);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_DIRECTORY = 0x00000010

MAX_RECORDED_ERRORS = 2000

# write buffering: flush after this many changed directories ...
FLUSH_DIRS = 64
# ... or after this many seconds
FLUSH_SECONDS = 0.4
# a directory unchanged for longer than this is still fully re-walked once per
# period, so stale size/date/attribute metadata cannot accumulate forever
FULL_WALK_INTERVAL = 3600.0

INSERT_SQL = ("INSERT INTO files (parent, name, is_folder, size, date_modified,"
              " date_created, date_accessed, attributes, extension)"
              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
UPDATE_SQL = ("UPDATE files SET is_folder = ?, size = ?, date_modified = ?,"
              " date_created = ?, date_accessed = ?, attributes = ?,"
              " extension = ? WHERE parent = ? AND name = ?")

# Serializes first-connection setup (WAL conversion + schema creation): two
# threads opening the same brand-new database concurrently used to fail with
# "attempt to write a readonly database" while racing on the WAL switch.
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = set()

# One writer at a time.  SQLite's WAL mode allows a single writer, but with
# several volume threads hammering the same file concurrently, the loser
# occasionally gets SQLITE_READONLY (not covered by the busy timeout) - the
# directory walk stays parallel, only the DB writes are serialized.
_WRITE_LOCK = threading.RLock()


def _ext_path(display_path):
    """Convert to an extended-length (\\\\?\\) path for long path support."""
    if display_path.startswith("\\\\?\\"):
        return display_path
    if display_path.startswith("\\\\"):
        return "\\\\?\\UNC" + display_path[1:]
    # Win32 rejects a trailing backslash on \\?\ paths (except the bare
    # 'C:\\' drive-root form), so strip it while keeping roots intact
    stripped = display_path.rstrip("\\")
    if len(stripped) == 2 and stripped[1] == ":":
        stripped = display_path
    return "\\\\?\\" + stripped


def _norm_root(root):
    """Normalize an index root so that ``parent + name`` concatenation works."""
    root = (root or "").strip()
    if not root:
        return ""
    if not root.endswith("\\"):
        root += "\\"
    return root


def _real_key(path):
    """Canonical (case-folded, links resolved) key used for de-duplication."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(path)


def _covers(ancestor, descendant):
    """True if *ancestor* (a normalized real path) already covers *descendant*."""
    if ancestor == descendant:
        return True
    if len(ancestor) == 3 and ancestor[1] == ":":  # drive root 'c:\'
        return descendant[:2] == ancestor[:2]
    return descendant.startswith(ancestor + "\\")


class _WriteBuffer:
    """Thread-local buffer of index mutations, flushed as one transaction."""

    __slots__ = ("ops", "dirs", "born")

    def __init__(self):
        self.ops = []
        self.dirs = 0
        self.born = time.monotonic()

    def add(self, op):
        self.ops.append(op)
        self.dirs += 1

    def due(self):
        return self.dirs >= FLUSH_DIRS or (
            self.ops and time.monotonic() - self.born >= FLUSH_SECONDS)


class Index:
    """SQLite backed index; one cached connection per (live) thread.

    Connections are created lazily per thread and released explicitly with
    :meth:`release_thread_conn` when a worker thread finishes (Qt threads are
    not ``threading.Thread`` instances, so they cannot be tracked through
    ``threading.enumerate()``).
    """

    def __init__(self, db_path):
        folder = os.path.dirname(os.path.abspath(db_path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.db_path = db_path
        self._conns = {}
        self._conns_lock = threading.Lock()
        self._local = threading.local()
        # diagnostics from the most recent scan
        self.errors = []        # list of (path, reason)
        self.skipped_links = 0  # junction/symlink dirs not followed
        self.skipped_dirs = 0   # directories skipped (mtime unchanged)
        self.last_timings = {}  # scandir/read/write seconds of the last scan
        self.write_stats = {}   # rows written per kind in the last scan
        # make sure the schema exists
        _ = self.conn

    # -- per-thread scan state --------------------------------------------
    def _tls(self):
        """Thread-local scan state (buffer, counters, timings)."""
        try:
            return self._local.state
        except AttributeError:
            state = self._local.state = {
                "buffer": _WriteBuffer(),
                "count": 0,
                "next_progress": 0,
                "t_scan": 0.0,
                "t_read": 0.0,
                "t_write": 0.0,
                "skipped": 0,
            }
            return state

    # -- connections ------------------------------------------------------
    def _new_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False,
                               timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-65536")  # 64 MiB
        with _SCHEMA_LOCK:
            key = os.path.abspath(self.db_path)
            if key not in _SCHEMA_READY:
                # Only the *first* connection switches the database to WAL and
                # creates the schema: PRAGMA journal_mode needs an exclusive
                # lock and fails with SQLITE_READONLY (not BUSY, so the busy
                # timeout does not help) when another connection is writing.
                # WAL is a persistent database property - later connections
                # inherit it without running the PRAGMA.
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                _SCHEMA_READY.add(key)
            # Force WAL/-shm recovery while we hold the lock: a new
            # connection's very first access recovers the -shm file, and
            # doing that concurrently with another connection's first access
            # fails with SQLITE_READONLY (wal.c's READONLY_RECOVERY).
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _write_with_retry(self, fn):
        """Run a (idempotent) write callable under the global write lock.

        Concurrent volume threads occasionally hit SQLite's transient
        SQLITE_READONLY windows (WAL checkpoint / recovery in progress on
        Windows).  Retrying with backoff clears them.
        """
        delay = 0.05
        for attempt in range(6):
            try:
                with _WRITE_LOCK:
                    return fn()
            except sqlite3.OperationalError as exc:
                if "readonly" not in str(exc).lower() or attempt == 5:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    @property
    def conn(self):
        ident = threading.get_ident()
        with self._conns_lock:
            conn = self._conns.get(ident)
            if conn is None:
                conn = self._new_conn()
                self._conns[ident] = conn
            return conn

    def release_thread_conn(self):
        """Close and forget the connection of the calling thread (if any)."""
        ident = threading.get_ident()
        with self._conns_lock:
            conn = self._conns.pop(ident, None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def close(self):
        with self._conns_lock:
            for conn in self._conns.values():
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()

    # -- basics -----------------------------------------------------------
    def stats(self):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS s FROM files"
        ).fetchone()
        return int(row["n"]), int(row["s"])

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        def write():
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
            self.conn.commit()
        self._write_with_retry(write)

    def clear(self):
        def write():
            self.conn.execute("DELETE FROM files")
            self.conn.execute("DELETE FROM meta")
            self.conn.commit()
        self._write_with_retry(write)

    def set_case_sensitive_like(self, on):
        self.conn.execute("PRAGMA case_sensitive_like=%s" % ("ON" if on else "OFF"))

    # -- maintenance ------------------------------------------------------
    def rebuild(self, roots, **kw):
        with _WRITE_LOCK:
            self.clear()
        return self.scan(roots, **kw)

    def scan(self, roots, exclude_folders=(), exclude_files=(), follow_junctions=False,
             progress=None, stop=None, skip_recent=None, skip_unchanged=False):
        """Walk *roots* and update the index. Returns the number of objects
        seen (scanned) in this pass - not the number of rows written, so a
        re-walk of an up-to-date index still reports the full object count.

        Never raises for ordinary filesystem problems: unreadable directories
        and files are collected in :attr:`errors` so that one bad directory
        cannot cost us the whole index.

        Writes are buffered per thread and flushed as one transaction every
        ``FLUSH_DIRS`` changed directories (or ``FLUSH_SECONDS``), so several
        volume threads can walk concurrently while the database stays
        consistent - the old single long transaction deadlocked against the
        write lock, and one commit per directory dominated the walk.

        *skip_recent* (seconds) skips a root whose previous full walk finished
        less than that many seconds ago - this lets the GUI resume where it
        left off after a restart instead of re-walking C: before ever reaching
        D:/E:.

        *skip_unchanged* re-walks only directories whose modification time
        changed since the previous walk (plus a forced full walk once per
        ``FULL_WALK_INTERVAL`` per root, so stale file metadata is refreshed
        hourly).  Used by periodic rescans; first builds and rebuilds always
        walk everything.
        """
        self.errors = []
        self.skipped_links = 0
        self.skipped_dirs = 0
        state = self._tls()
        state["buffer"] = _WriteBuffer()
        state["count"] = 0
        state["next_progress"] = 0
        state["t_scan"] = 0.0
        state["t_read"] = 0.0
        state["t_write"] = 0.0
        state["skipped"] = 0
        state["roots_done"] = []
        state["fullwalk"] = []
        started = time.time()

        # normalize + de-duplicate roots (a folder root covered by an earlier
        # volume root would otherwise be walked twice and duplicated)
        seen_real = []
        effective = []
        for raw in roots or ():
            root = _norm_root(raw)
            if not root:
                continue
            real = _real_key(root)
            if any(_covers(a, real) for a in seen_real):
                continue
            seen_real = [a for a in seen_real if not _covers(real, a)]
            seen_real.append(real)
            effective.append(root)

        try:
            visited_real = set()
            for root in effective:
                if stop is not None and stop.is_set():
                    break
                if match_exclude(root, exclude_folders):
                    self._delete_subtree(root)
                    continue
                if skip_recent:
                    last = self.get_meta("root_done:" + root.lower())
                    try:
                        if last and (time.time() - float(last)) < float(skip_recent):
                            continue  # recently walked; leave the rows alone
                    except (TypeError, ValueError):
                        pass
                # incremental mode only while the last *full* walk of this
                # root is recent enough; afterwards force one full walk so
                # stale size/date/attribute metadata is refreshed
                incremental = False
                if skip_unchanged:
                    last_full = self.get_meta("fullwalk:" + root.lower())
                    try:
                        incremental = bool(last_full) and (
                            time.time() - float(last_full)) < FULL_WALK_INTERVAL
                    except (TypeError, ValueError):
                        incremental = False
                st = self._stat(root)
                if st is None:
                    # root vanished (e.g. drive removed) -> prune it
                    self._delete_subtree(root)
                    self._note_error(root, "root not accessible")
                    continue
                self._upsert_root(root, st)
                visited_real.add(_real_key(root))
                if progress is not None:
                    # announce the volume immediately so the UI can show
                    # "Indexing D:\ ..." instead of a deep subdirectory
                    try:
                        progress(root, state["count"])
                    except Exception:
                        pass
                # explicit stack instead of recursion: deep trees (nested
                # node_modules, unpacked archives...) must not overflow
                stack = [root]
                while stack:
                    if stop is not None and stop.is_set():
                        break
                    subdirs = self._scan_dir(stack.pop(), exclude_folders,
                                             exclude_files, follow_junctions,
                                             progress, stop, visited_real,
                                             skip_unchanged=incremental)
                    if subdirs:
                        stack.extend(subdirs)
                    if state["buffer"].due():
                        self._flush()
                if not (stop is not None and stop.is_set()):
                    # remember that this volume is fully walked, so a restart
                    # can skip it until the next rescan interval
                    state["roots_done"].append((root.lower(), time.time()))
                    if not incremental:
                        state["fullwalk"].append((root.lower(), time.time()))
        except sqlite3.Error:
            # only database problems are fatal; filesystem errors were
            # already absorbed per-directory above
            raise
        finally:
            # keep whatever was walked so far, even on error/interrupt
            self._flush()
        for key, ts in state["roots_done"]:
            self.set_meta("root_done:" + key, "%.3f" % ts)
        for key, ts in state["fullwalk"]:
            self.set_meta("fullwalk:" + key, "%.3f" % ts)
        self.set_meta("last_scan", "%.3f" % time.time())
        self.set_meta("last_scan_seconds", "%.3f" % (time.time() - started))
        self.set_meta("last_scan_errors", str(len(self.errors)))
        self.skipped_dirs = state["skipped"]
        self.last_timings = {
            "scandir": state["t_scan"],
            "read": state["t_read"],
            "write": state["t_write"],
            "total": time.time() - started,
        }
        return state["count"]

    def _upsert_root(self, root, st):
        """Insert/refresh the volume root row."""
        attrs = int(getattr(st, "st_file_attributes", FILE_ATTRIBUTE_DIRECTORY) or
                    FILE_ATTRIBUTE_DIRECTORY)

        def write():
            conn = self.conn
            conn.execute("DELETE FROM files WHERE parent = '' AND name = ?", (root,))
            conn.execute(
                "INSERT INTO files (parent, name, is_folder, size, date_modified,"
                " date_created, date_accessed, attributes, extension)"
                " VALUES ('', ?, 1, 0, ?, ?, ?, ?, '')",
                (root, st.st_mtime, st.st_ctime, st.st_atime, attrs))
            conn.commit()

        self._write_with_retry(write)
        self._tls()["count"] += 1

    # -- write buffering ---------------------------------------------------
    def _flush(self):
        """Execute buffered mutations as one transaction (per thread)."""
        state = self._tls()
        buffer = state["buffer"]
        if not buffer.ops:
            return
        ops = buffer.ops
        state["buffer"] = _WriteBuffer()
        stats = {"dirs": len(ops), "inserted": 0, "updated": 0,
                 "replaced": 0, "deleted": 0}
        for op in ops:
            if op[0] in ("insert", "replace"):
                key = "replaced" if op[0] == "replace" else "inserted"
                stats[key] += len(op[2])
            elif op[0] == "update":
                stats["updated"] += len(op[2])
            elif op[0] == "delnames":
                stats["deleted"] += len(op[2])
        self.write_stats = stats

        def write():
            conn = self.conn
            for op in ops:
                kind = op[0]
                if kind == "insert":
                    _, d, rows = op
                    if rows:
                        conn.executemany(INSERT_SQL, rows)
                elif kind == "replace":
                    _, d, rows = op
                    conn.execute("DELETE FROM files WHERE parent = ?", (d,))
                    if rows:
                        conn.executemany(INSERT_SQL, rows)
                elif kind == "update":
                    _, d, rows = op
                    conn.executemany(UPDATE_SQL, rows)
                elif kind == "delnames":
                    _, d, names = op
                    for i in range(0, len(names), 400):
                        chunk = names[i:i + 400]
                        conn.execute(
                            "DELETE FROM files WHERE parent = ? AND name IN (%s)"
                            % ",".join("?" * len(chunk)), (d,) + tuple(chunk))
                elif kind == "subtree":
                    self._delete_subtree_sql(conn, op[1])
            conn.commit()

        t0 = time.perf_counter()
        self._write_with_retry(write)
        state["t_write"] += time.perf_counter() - t0

    # -- directory walking ------------------------------------------------
    def _scan_dir(self, d, exclude_folders, exclude_files, follow_junctions,
                  progress, stop, visited_real, skip_unchanged=False):
        """Index one directory; returns the list of subdirectories to walk."""
        if stop is not None and stop.is_set():
            return []
        state = self._tls()
        it = self._scandir(d)
        if it is None:
            if self._stat(d) is None:
                # the directory no longer exists -> prune its subtree
                self._delete_subtree(d)
                self._note_error(d, "directory disappeared during scan")
            else:
                # exists but cannot be listed (permissions, IO error): keep
                # the rows of the previous scan, they are probably still good
                self._note_error(d, "cannot list directory")
            return []
        conn = self.conn
        t0 = time.perf_counter()
        old_rows = {}
        for r in conn.execute(
                "SELECT name, is_folder, size, date_modified, date_created,"
                " date_accessed, attributes, extension FROM files WHERE parent = ?",
                (d,)):
            old_rows[r["name"]] = (r["is_folder"], r["size"], r["date_modified"],
                                   r["date_created"], r["date_accessed"],
                                   r["attributes"], r["extension"])
        state["t_read"] += time.perf_counter() - t0
        seen = set()
        batch = []
        subdirs = []
        t1 = time.perf_counter()
        try:
            with it:
                for entry in it:
                    name = entry.name
                    if name in (".", ".."):
                        continue
                    full = d + name
                    if match_exclude(full, exclude_files) or match_exclude(full, exclude_folders):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                        attrs = int(getattr(st, "st_file_attributes", 0) or 0)
                        size = int(st.st_size)
                        mtime, ctime, atime = st.st_mtime, st.st_ctime, st.st_atime
                    except OSError:
                        # the entry exists but its metadata is unreadable:
                        # index it with unknown metadata rather than lose it
                        self._note_error(full, "cannot read metadata")
                        attrs = 0
                        size = 0
                        mtime = ctime = atime = None
                    try:
                        is_dir = entry.is_dir(follow_symlinks=True)
                    except OSError:
                        is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
                    if is_dir and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                        if not follow_junctions:
                            self.skipped_links += 1
                            recurse = False
                        else:
                            # follow, but never walk the same real path twice
                            real = _real_key(full)
                            if real in visited_real:
                                recurse = False
                            else:
                                visited_real.add(real)
                                recurse = True
                    else:
                        recurse = is_dir
                        if recurse and skip_unchanged and mtime is not None:
                            # nothing added/removed/renamed here since the last
                            # walk (that would have changed the directory's
                            # mtime): keep the indexed rows as they are
                            old = old_rows.get(name)
                            if (old is not None and old[0] == 1
                                    and old[2] == mtime):
                                recurse = False
                                state["skipped"] += 1
                    ext = ""
                    if not is_dir:
                        dot = name.rfind(".")
                        if dot > 0:
                            ext = name[dot + 1:].lower()
                    batch.append((d, name, 1 if is_dir else 0,
                                  0 if is_dir else size,
                                  mtime, ctime, atime, attrs, ext))
                    seen.add(name)
                    if recurse:
                        subdirs.append(full + "\\")
        except OSError as exc:
            # directory changed under us / listing broke half-way: keep the
            # entries collected so far instead of losing the whole scan
            self._note_error(d, "listing interrupted: %s" % exc)
        state["t_scan"] += time.perf_counter() - t1
        # Progress must count *scanned* objects, not written rows: re-walking
        # an up-to-date index writes almost nothing in diff mode, and the old
        # "count += inserted rows" froze the UI counter on the volume root (1).
        state["count"] += len(batch)

        # ---- diff against the indexed rows: write only what changed ------
        gone = old_rows.keys() - seen
        if not gone and not batch and not old_rows:
            return subdirs
        if old_rows:
            # fast path: dict equality is C-speed, so an unchanged directory
            # costs exactly what the old "rewrite whole directory" check cost
            new_map = {b[1]: (b[2], b[3], b[4], b[5], b[6], b[7], b[8])
                       for b in batch}
            if gone or new_map != old_rows:
                buffer = state["buffer"]
                if gone:
                    gone_folders = [n for n in gone if old_rows[n][0]]
                    buffer.add(("delnames", d, list(gone)))
                    for name in gone_folders:
                        buffer.add(("subtree", d + name + "\\"))
                new_rows = [b for b in batch if b[1] not in old_rows]
                diff_rows = []
                for b in batch:
                    old = old_rows.get(b[1])
                    if old is not None and old != (b[2], b[3], b[4], b[5],
                                                   b[6], b[7], b[8]):
                        diff_rows.append(b)
                if new_rows or diff_rows:
                    if (len(new_rows) + len(diff_rows)) * 4 >= len(batch):
                        # most entries changed: a full rewrite is cheaper
                        # than row-by-row updates
                        buffer.add(("replace", d, batch))
                    else:
                        if new_rows:
                            buffer.add(("insert", d, new_rows))
                        if diff_rows:
                            updates = [(b[2], b[3], b[4], b[5], b[6], b[7],
                                        b[8], d, b[1]) for b in diff_rows]
                            buffer.add(("update", d, updates))
        elif batch:
            state["buffer"].add(("insert", d, batch))
        if progress is not None and state["count"] >= state["next_progress"]:
            state["next_progress"] = state["count"] + 2000
            try:
                progress(d, state["count"])
            except Exception:
                pass
        return subdirs

    def _note_error(self, path, reason):
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append((path, reason))

    def _delete_subtree_sql(self, conn, display_dir):
        """Remove a folder (and everything below it) inside a transaction."""
        like = (display_dir.replace("\\", "\\\\").replace("%", "\\%")
                .replace("_", "\\_") + "%")
        conn.execute("DELETE FROM files WHERE parent = ?", (display_dir,))
        conn.execute("DELETE FROM files WHERE parent LIKE ? ESCAPE '\\'", (like,))
        conn.execute("DELETE FROM files WHERE parent = '' AND name = ?",
                     (display_dir,))

    def _delete_subtree(self, display_dir):
        """Remove a folder (and everything below it) from the index."""

        def write():
            self._delete_subtree_sql(self.conn, display_dir)
            self.conn.commit()

        self._write_with_retry(write)

    # -- low level io -----------------------------------------------------
    def _scandir(self, display_dir):
        # try the extended-length path first: the plain form fails once the
        # absolute path exceeds MAX_PATH (260 chars), which is common in
        # site-packages / node_modules trees
        for candidate in (_ext_path(display_dir), display_dir):
            try:
                return os.scandir(candidate)
            except OSError:
                continue
        return None

    def _stat(self, display_dir):
        for candidate in (_ext_path(display_dir), display_dir):
            try:
                return os.stat(candidate)
            except OSError:
                continue
        return None
