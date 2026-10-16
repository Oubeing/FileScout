"""Configuration, bookmarks, filters and recent-search history (JSON file)."""
from __future__ import annotations

import json
import os
import re
import sys

APP_DIR_NAME = "FileScout"
CONFIG_VERSION = 3

# pre-rename names, migrated automatically on first start
_LEGACY_APP_DIR_NAME = "EverythingPy"
_LEGACY_DB_NAME = "everything.db"

DB_NAME = "filescout.db"

DEFAULT_FILTERS = [
    {"name": "All", "query": "", "sort": "name"},
    {"name": "Audio", "query": "audio:", "sort": "name"},
    {"name": "Compressed", "query": "zip:", "sort": "name"},
    {"name": "Doc", "query": "doc:", "sort": "name"},
    {"name": "Exe", "query": "exe:", "sort": "name"},
    {"name": "Folder", "query": "folder:", "sort": "name"},
    {"name": "Pic", "query": "pic:", "sort": "name"},
    {"name": "Video", "query": "video:", "sort": "name"},
]

# Folders skipped by default (visible/editable in Tools -> Options -> Exclude).
# A directory-walk indexer cannot afford Windows.old (hundreds of thousands of
# hard links under WinSxS) and the two ACL-locked system folders; an MFT based
# scanner does not need these exclusions.  The WinSxS metadata subdirectories
# are component-store bookkeeping (measured: they dominate the first full C:
# walk) and hold nothing a user ever searches for.
DEFAULT_EXCLUDE_FOLDERS = [
    "*\\Windows.old",
    "*\\$Recycle.Bin",
    "*\\System Volume Information",
    "*\\WinSxS\\Manifests",
    "*\\WinSxS\\Catalogs",
    "*\\WinSxS\\Backup",
    "*\\WinSxS\\FileMaps",
    "*\\WinSxS\\Temp",
]


def app_dir():
    """Base directory for config + database.

    Override with the FILESCOUT_HOME environment variable (portable mode).
    A pre-rename install directory is moved to the new name once, so an
    existing index survives the upgrade.
    """
    env = os.environ.get("FILESCOUT_HOME") or os.environ.get("EVERYTHING_PY_HOME")
    if env:
        return os.path.abspath(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    new_dir = os.path.join(base, APP_DIR_NAME)
    old_dir = os.path.join(base, _LEGACY_APP_DIR_NAME)
    if os.path.isdir(old_dir) and not os.path.exists(new_dir):
        try:
            os.replace(old_dir, new_dir)  # same volume -> atomic rename
        except OSError:
            # e.g. the old directory is locked by a still-running instance
            return old_dir
    return new_dir


def default_config():
    return {
        "version": CONFIG_VERSION,
        "general": {
            "max_results": 10000,
            "search_history_max": 10,
            "content_max_size": 8 * 1024 * 1024,
            "always_on_top": False,
            "confirm_delete": True,
        },
        "search": {
            "match_case": False,
            "match_whole_word": False,
            "match_path": False,
            "regex": False,
            "match_diacritics": False,
            "duplicates": False,
            "folders_first": False,
            "sort": "name",
            "sort_ascending": True,
        },
        "indexes": {
            "volumes": [],
            "folders": [],
            # following junction points matters for a directory-walk indexer:
            # without it, anything reachable only through a junction (e.g.
            # 'Application Data', 'Documents and Settings') is invisible
            "follow_junctions": True,
            "exclude_folders": list(DEFAULT_EXCLUDE_FOLDERS),
            "exclude_files": [],
            "rescan_interval": 300,
            # a directory whose modification time is unchanged is not re-walked
            # during periodic rescans (its entry names stay correct); file
            # content changes inside such a directory are picked up by the next
            # walk of that directory or by Force Rebuild (Shift+F5)
            "skip_unchanged_dirs": True,
        },
        "bookmarks": [],
        "filters": [dict(f) for f in DEFAULT_FILTERS],
        "history": [],
        "window": {
            "geometry": "1020x680",
            "columns": {"name": 260, "path": 400, "size": 90,
                        "extension": 90, "date_modified": 150},
        },
    }


def _deep_update(dst, src):
    for key, val in src.items():
        if isinstance(val, dict) and isinstance(dst.get(key), dict):
            _deep_update(dst[key], val)
        else:
            dst[key] = val


class Config:
    def __init__(self, path=None):
        self.path = path or os.path.join(app_dir(), "config.json")
        self.data = default_config()
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self):
        stored_version = 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                stored_version = int(stored.get("version", 0) or 0)
                _deep_update(self.data, stored)
        except (OSError, ValueError):
            pass
        if stored_version < CONFIG_VERSION:
            self._migrate(stored_version)
        if not self.data["indexes"]["volumes"] and not self.data["indexes"]["folders"]:
            self.data["indexes"]["volumes"] = _detect_volumes()

    def _migrate(self, from_version):
        """Upgrade older configs so existing users get the completeness fixes."""
        if from_version < 2:
            # v1 -> v2: a directory-walk indexer must follow junctions or
            # anything reachable only through one is invisible; and the
            # result cap of 1000 silently truncated large searches
            self.data["indexes"]["follow_junctions"] = True
            try:
                if int(self.data["general"].get("max_results") or 0) < 10000:
                    self.data["general"]["max_results"] = 10000
            except (TypeError, ValueError):
                self.data["general"]["max_results"] = 10000
            if not self.data["indexes"].get("exclude_folders"):
                self.data["indexes"]["exclude_folders"] = list(
                    DEFAULT_EXCLUDE_FOLDERS)
            self.data["version"] = CONFIG_VERSION
            try:
                self.save()
            except OSError:
                pass
        if from_version < 3:
            # v2 -> v3: add the WinSxS metadata exclusions (they dominate the
            # first full C: walk) for configs that already migrated to v2
            existing = {p.lower() for p in
                        self.data["indexes"].get("exclude_folders", [])}
            added = False
            for pattern in DEFAULT_EXCLUDE_FOLDERS:
                if pattern.lower() not in existing:
                    self.data["indexes"]["exclude_folders"].append(pattern)
                    added = True
            self.data["version"] = CONFIG_VERSION
            if added:
                try:
                    self.save()
                except OSError:
                    pass

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # -- paths ------------------------------------------------------------
    @property
    def db_path(self):
        folder = os.path.dirname(self.path)
        new_path = os.path.join(folder, DB_NAME)
        legacy = os.path.join(folder, _LEGACY_DB_NAME)
        if not os.path.exists(new_path) and os.path.exists(legacy):
            # one-time rename of a pre-rename database (keeps the index)
            try:
                os.replace(legacy, new_path)
            except OSError:
                return legacy
        if not os.path.exists(legacy):
            # the rename already happened: drop the old -wal/-shm sidecars so
            # they do not linger forever (nothing can use them once the main
            # database file of that name is gone)
            for suffix in ("-wal", "-shm"):
                sidecar = legacy + suffix
                if os.path.exists(sidecar):
                    try:
                        os.remove(sidecar)
                    except OSError:
                        pass
        return new_path

    # -- sections ---------------------------------------------------------
    @property
    def general(self):
        return self.data["general"]

    @property
    def search(self):
        return self.data["search"]

    @property
    def indexes(self):
        return self.data["indexes"]

    @property
    def bookmarks(self):
        return self.data["bookmarks"]

    @property
    def filters(self):
        return self.data["filters"]

    @property
    def history(self):
        return self.data["history"]

    # -- index roots ------------------------------------------------------
    def roots(self):
        """Normalized list of index roots, each ending with a backslash."""
        env = os.environ.get("FILESCOUT_ROOTS") or os.environ.get(
            "EVERYTHING_PY_ROOTS")
        if env:
            raw = [p for p in re.split(r"[;,]", env) if p.strip()]
        else:
            raw = list(self.indexes["volumes"]) + list(self.indexes["folders"])
        out = []
        for item in raw:
            item = item.strip()
            if not item:
                continue
            if not item.endswith("\\"):
                item += "\\"
            if item not in out:
                out.append(item)
        return out

    # -- history ----------------------------------------------------------
    def add_history(self, query, limit=None):
        query = (query or "").strip()
        if not query:
            return
        limit = limit if limit is not None else self.general["search_history_max"]
        hist = [h for h in self.history if h != query]
        hist.insert(0, query)
        del hist[limit:]
        self.data["history"] = hist

    def clear_history(self):
        self.data["history"] = []

    # -- bookmarks --------------------------------------------------------
    def add_bookmark(self, name, query, sort=None, match_case=None):
        name = (name or "").strip()
        if not name:
            return
        self.data["bookmarks"] = [b for b in self.bookmarks if b.get("name") != name]
        entry = {"name": name, "query": query or ""}
        if sort:
            entry["sort"] = sort
        if match_case is not None:
            entry["match_case"] = bool(match_case)
        self.bookmarks.insert(0, entry)

    def remove_bookmark(self, name):
        self.data["bookmarks"] = [b for b in self.bookmarks if b.get("name") != name]

    # -- filters ----------------------------------------------------------
    def filter_by_name(self, name):
        for f in self.filters:
            if f.get("name", "").lower() == (name or "").lower():
                return f
        return None


def _detect_volumes():
    try:
        from .util import fixed_drives
        return fixed_drives()
    except Exception:
        return []
