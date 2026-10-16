"""PyQt5 desktop GUI - the main FileScout window.

Layout: menu bar, search box with history and match option toggles, filter
selector, sortable result list (Name / Path / Size / Extension /
Date Modified ...), and a status bar showing the object count, index state
and total size.

The indexer and the search engine run in QThread workers and stream results
back through Qt signals, so the UI stays responsive while typing.
"""
from __future__ import annotations

import base64
import copy
import os
import threading
import time

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QAction, QActionGroup,
                             QApplication, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                             QProgressBar, QPushButton, QSizePolicy, QShortcut,
                             QSpinBox, QTabWidget, QToolButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_TITLE, __version__
from .config import Config
from .indexer import Index
from .search import (ATTRIB_MAP, EXT_MACROS, SORT_LABELS, SearchOptions,
                     combine_query, parse_sort_term, run_search)
from .util import (fixed_drives, format_size, format_ts, open_file, open_folder,
                   open_path, recycle, show_properties)

# columns: key, label, default width, default visible
COLUMNS = [
    ("path", "Path", 400, True),
    ("size", "Size", 90, True),
    ("extension", "Extension", 90, True),
    ("date_modified", "Date Modified", 150, True),
    ("date_created", "Date Created", 150, False),
    ("date_accessed", "Date Accessed", 150, False),
    ("attributes", "Attributes", 90, False),
]

# result column key -> search engine sort key
COLUMN_TO_SORT = {
    "name": "name",
    "path": "path",
    "size": "size",
    "extension": "extension",
    "date_modified": "dm",
    "date_created": "dc",
    "date_accessed": "da",
    "attributes": "attrib",
}

TOGGLES = [
    ("match_case", "Match Case"),
    ("match_whole_word", "Whole Word"),
    ("match_path", "Match Path"),
    ("regex", "Regex"),
    ("match_diacritics", "Diacritics"),
    ("duplicates", "Duplicates"),
]

SORT_ORDER = ["name", "path", "size", "extension", "dm", "dc", "da", "attrib"]

_ICON_SIZE = 16


# ---------------------------------------------------------------------------
# icons (drawn with QPainter - no external assets needed)
# ---------------------------------------------------------------------------

def _folder_icon():
    pix = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.fillRect(2, 3, 7, 3, QColor("#e8b93d"))      # tab
    p.fillRect(1, 6, 14, 8, QColor("#f2c94c"))     # body
    p.fillRect(1, 14, 14, 1, QColor("#c99a2e"))    # bottom edge
    p.fillRect(14, 6, 1, 8, QColor("#c99a2e"))     # right edge
    p.end()
    return QIcon(pix)


def _file_icon(fill="#ffffff"):
    pix = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.fillRect(3, 2, 9, 13, QColor(fill))          # page
    p.fillRect(11, 2, 1, 13, QColor("#9aa3ad"))    # right edge
    p.fillRect(3, 14, 9, 1, QColor("#9aa3ad"))     # bottom edge
    p.fillRect(10, 2, 3, 3, QColor("#c3c9d1"))     # folded corner
    p.end()
    return QIcon(pix)


_ICON_FILLS = {
    "doc": "#dbeafe", "pic": "#dcfce7", "audio": "#f3e8ff",
    "video": "#fce7f3", "zip": "#ffedd5", "exe": "#e5e7eb",
}


def _icon_category(row):
    ext = row["extension"]
    if not ext:
        return None
    for macro, exts in EXT_MACROS.items():
        if ext in exts:
            return macro
    return None


def format_attributes(attrs):
    if not attrs:
        return ""
    out = []
    for letter, bit in ATTRIB_MAP.items():
        if attrs & bit:
            out.append(letter)
    return "".join(sorted(out))


# ---------------------------------------------------------------------------
# background workers
# ---------------------------------------------------------------------------

class IndexWorker(QThread):
    """Keeps the index up to date: initial scan, periodic rescans, rebuilds."""

    started = pyqtSignal(str, bool, bool)  # label, first_build, user_requested
    progress = pyqtSignal(str, int)    # current volume/directory, object count
    root_done = pyqtSignal(str, int, int)  # volume root, objects, error count
    done = pyqtSignal(int, int, list)      # total, error count, error samples
    failed = pyqtSignal(str)

    def __init__(self, index, cfg):
        super().__init__(objectName="IndexWorker")
        self.index = index
        self.cfg = cfg
        self.stop_event = _make_event()
        self.rescan_requested = _make_event()
        self.rebuild_requested = _make_event()
        self.user_requested = False
        self._incremental = False

    def stop(self):
        self.stop_event.set()
        self.rescan_requested.set()
        self.rebuild_requested.set()

    def request_rescan(self):
        self.user_requested = True
        self.rescan_requested.set()

    def request_rebuild(self):
        self.user_requested = True
        self.rebuild_requested.set()

    def _scan_kwargs(self):
        ix = self.cfg.indexes
        return {
            "exclude_folders": ix["exclude_folders"],
            "exclude_files": ix["exclude_files"],
            "follow_junctions": ix["follow_junctions"],
            "progress": lambda d, n: self.progress.emit(d, n),
            "stop": self.stop_event,
            # periodic rescans only re-walk directories whose mtime changed;
            # first builds and rebuilds always walk everything
            "skip_unchanged": self._incremental,
        }

    def _needs_full_build(self):
        """True when some root has never been fully walked.

        Only then is the index really incomplete and the yellow banner
        justified - a periodic rescan of an already complete index must stay
        quiet (the old code re-showed "results incomplete" every 5 minutes).
        """
        roots = self.cfg.roots()
        if not roots:
            return False
        try:
            count, _size = self.index.stats()
            if count <= 0:
                return True
            for root in roots:
                if not self.index.get_meta("root_done:" + root.lower()):
                    return True
            return False
        except Exception:
            return True  # unknown state: show the banner, better safe

    def run(self):
        interval = max(10, int(self.cfg.indexes.get("rescan_interval", 300)))
        try:
            while not self.stop_event.is_set():
                rebuild = self.rebuild_requested.is_set()
                if rebuild:
                    self.rebuild_requested.clear()
                label = "Force rebuild" if rebuild else "Indexing"
                self._incremental = bool(
                    self.cfg.indexes.get("skip_unchanged_dirs", True)) and not rebuild
                first_build = True if rebuild else self._needs_full_build()
                user_requested = self.user_requested
                self.user_requested = False
                self.started.emit(label, first_build, user_requested)
                try:
                    roots = self.cfg.roots()
                    if rebuild:
                        # wipe once, then walk every volume from scratch
                        self.index.clear()
                    # Warm up the WAL/-shm state with a tiny write from this
                    # thread before the volume threads start: the very first
                    # write to a fresh database initializes the shared-memory
                    # file, and racing that initialization makes concurrent
                    # connections fail with SQLITE_READONLY.
                    try:
                        self.index.set_meta("warmup", "1")
                    except Exception:
                        pass
                    # Scan every volume *in parallel*: a directory walk is IO
                    # bound, so scanning C: must not keep D:/E: waiting for
                    # minutes.  Each volume gets its own Index instance (and
                    # therefore its own SQLite connection); WAL mode plus the
                    # 30s busy timeout absorbs the write contention.
                    errors = []
                    samples = []
                    threads = []
                    for root in roots:
                        volume_index = Index(self.index.db_path)
                        thread = threading.Thread(
                            target=self._scan_volume,
                            args=(root, volume_index,
                                  None if rebuild else interval,
                                  errors, samples),
                            name="IndexVolume:%s" % root)
                        thread.start()
                        threads.append((thread, volume_index))
                    for thread, volume_index in threads:
                        thread.join()
                        volume_index.close()
                    self.done.emit(0, len(errors), samples[:5])
                except Exception as exc:  # surface in the status bar
                    self.failed.emit(str(exc))
                deadline = time.time() + interval
                while not self.stop_event.is_set():
                    if self.rescan_requested.is_set():
                        self.rescan_requested.clear()
                        break
                    if self.rebuild_requested.is_set():
                        break
                    if time.time() >= deadline:
                        break
                    time.sleep(0.2)
        finally:
            self.index.release_thread_conn()

    def _scan_volume(self, root, index, skip_recent, errors, samples):
        try:
            n = index.scan([root], skip_recent=skip_recent,
                           **self._scan_kwargs())
            errors.extend(index.errors)
            samples.extend(index.errors)
            self.root_done.emit(root, n, len(index.errors))
        except Exception as exc:  # surface in the status bar
            self.failed.emit("%s: %s" % (root, exc))


class SearchWorker(QThread):
    """Runs one search; results stream back through Qt signals."""

    batch = pyqtSignal(int, list)              # qid, rows
    finished = pyqtSignal(int, bool, int, float)  # qid, truncated, total size, seconds
    failed = pyqtSignal(int, str)

    def __init__(self, index, query, opts, qid):
        super().__init__(objectName="SearchWorker")
        self.index = index
        self.query = query
        self.opts = opts
        self.qid = qid
        self.stop_event = _make_event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        def on_batch(rows):
            if not self.stop_event.is_set():
                self.batch.emit(self.qid, rows)

        try:
            res = run_search(self.index, self.query, self.opts,
                             on_batch=on_batch, stop=self.stop_event)
            self.finished.emit(self.qid, res.truncated, res.total_size, res.took)
        except Exception as exc:
            self.failed.emit(self.qid, str(exc))
        finally:
            self.index.release_thread_conn()


def _make_event():
    """A threading.Event that can be created from any thread."""
    return threading.Event()


# ---------------------------------------------------------------------------
# search entry with history support
# ---------------------------------------------------------------------------

class SearchEdit(QLineEdit):
    """QLineEdit that opens the history popup on Down and clears on Escape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._window = parent

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Down:
            if self._window is not None:
                self._window.show_history()
            return
        if key == Qt.Key_Escape:
            if self._window is not None:
                self._window.clear_search()
            return
        super().keyPressEvent(event)


class HistoryPopup(QListWidget):
    """Frameless popup listing recent searches."""

    def __init__(self, parent, on_choose):
        super().__init__(parent)
        self._on_choose = on_choose
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.itemActivated.connect(self._choose)
        self.itemDoubleClicked.connect(self._choose)

    def fill(self, entries, width):
        self.clear()
        for text in entries:
            QListWidgetItem(text, self)
        self.setCurrentRow(0)
        self.setMinimumWidth(max(width, 220))
        self.setMaximumHeight(min(10, len(entries)) * 22 + 4)

    def _choose(self, item=None):
        item = item or self.currentItem()
        if item is not None:
            self._on_choose(item.text())
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._choose()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        self.close()
        super().focusOutEvent(event)


# ---------------------------------------------------------------------------
# options dialog
# ---------------------------------------------------------------------------

class OptionsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.cfg = parent.cfg
        self.draft = copy.deepcopy(self.cfg.data)
        self.setWindowTitle("Options")
        self.setModal(False)
        self.setMinimumWidth(560)

        tabs = QTabWidget(self)
        self._tab_general(tabs)
        self._tab_search(tabs)
        self._tab_indexes(tabs)
        self._tab_exclude(tabs)
        self._tab_filters(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Apply
                                   | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).clicked.connect(self._ok)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- tabs -------------------------------------------------------------
    def _tab_general(self, tabs):
        tab = QWidget()
        form = QVBoxLayout(tab)
        g = self.draft["general"]
        grid = QHBoxLayout()
        self.spin_max_results = QSpinBox()
        self.spin_max_results.setRange(100, 1000000)
        self.spin_max_results.setValue(g["max_results"])
        self.spin_history_max = QSpinBox()
        self.spin_history_max.setRange(0, 100)
        self.spin_history_max.setValue(g["search_history_max"])
        grid.addWidget(QLabel("Maximum results:"))
        grid.addWidget(self.spin_max_results)
        grid.addStretch(1)
        grid.addWidget(QLabel("Recent searches kept:"))
        grid.addWidget(self.spin_history_max)
        grid.addStretch(1)
        form.addLayout(grid)
        self.chk_always_on_top = QCheckBox("Always on top")
        self.chk_always_on_top.setChecked(g["always_on_top"])
        form.addWidget(self.chk_always_on_top)
        self.chk_confirm_delete = QCheckBox("Confirm before deleting to the recycle bin")
        self.chk_confirm_delete.setChecked(g["confirm_delete"])
        form.addWidget(self.chk_confirm_delete)
        form.addStretch(1)
        tabs.addTab(tab, "General")

    def _tab_search(self, tabs):
        tab = QWidget()
        form = QVBoxLayout(tab)
        s = self.draft["search"]
        self.search_checks = {}
        grid = QHBoxLayout()
        for i, (key, label) in enumerate(TOGGLES):
            chk = QCheckBox(label)
            chk.setChecked(bool(s[key]))
            self.search_checks[key] = chk
            grid.addWidget(chk)
            if i % 3 == 2:
                form.addLayout(grid)
                grid = QHBoxLayout()
        if grid.count():
            form.addLayout(grid)
        self.chk_folders_first = QCheckBox("Sort folders first")
        self.chk_folders_first.setChecked(bool(s["folders_first"]))
        form.addWidget(self.chk_folders_first)
        row = QHBoxLayout()
        row.addWidget(QLabel("Default sort:"))
        self.combo_default_sort = QComboBox()
        self.combo_default_sort.addItems([SORT_LABELS[k] for k in SORT_ORDER])
        label_to_key = {v: k for k, v in SORT_LABELS.items()}
        self.combo_default_sort.setCurrentText(SORT_LABELS.get(s["sort"], "Name"))
        self._label_to_key = label_to_key
        row.addWidget(self.combo_default_sort)
        row.addStretch(1)
        form.addLayout(row)
        form.addStretch(1)
        tabs.addTab(tab, "Search")

    def _tab_indexes(self, tabs):
        tab = QWidget()
        form = QVBoxLayout(tab)
        ix = self.draft["indexes"]

        form.addWidget(QLabel("Volumes:"))
        vol_frame = QHBoxLayout()
        self.vol_checks = {}
        drives = sorted(set(fixed_drives()) | set(ix["volumes"]))
        for drive in drives:
            chk = QCheckBox(drive)
            chk.setChecked(drive in ix["volumes"])
            self.vol_checks[drive] = chk
            vol_frame.addWidget(chk)
        vol_frame.addStretch(1)
        form.addLayout(vol_frame)

        form.addWidget(QLabel("Folders:"))
        folder_frame = QHBoxLayout()
        self.folder_list = QListWidget()
        self.folder_list.setMaximumHeight(100)
        for folder in ix["folders"]:
            QListWidgetItem(folder, self.folder_list)
        folder_frame.addWidget(self.folder_list)
        btn_col = QVBoxLayout()
        add_btn = QPushButton("Add...")
        add_btn.clicked.connect(self._add_folder)
        btn_col.addWidget(add_btn)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda: self._remove_from(self.folder_list))
        btn_col.addWidget(remove_btn)
        btn_col.addStretch(1)
        folder_frame.addLayout(btn_col)
        form.addLayout(folder_frame)

        self.chk_follow_junctions = QCheckBox("Follow junction points / symlinks")
        self.chk_follow_junctions.setChecked(bool(ix["follow_junctions"]))
        form.addWidget(self.chk_follow_junctions)

        row = QHBoxLayout()
        row.addWidget(QLabel("Rescan interval (seconds):"))
        self.spin_rescan = QSpinBox()
        self.spin_rescan.setRange(10, 86400)
        self.spin_rescan.setValue(ix["rescan_interval"])
        row.addWidget(self.spin_rescan)
        row.addStretch(1)
        form.addLayout(row)
        form.addStretch(1)
        tabs.addTab(tab, "Indexes")

    def _tab_exclude(self, tabs):
        tab = QWidget()
        form = QVBoxLayout(tab)
        ix = self.draft["indexes"]
        self.exc_folders = self._exclude_list(form, "Exclude folders:", ix["exclude_folders"])
        self.exc_files = self._exclude_list(form, "Exclude files:", ix["exclude_files"])
        form.addStretch(1)
        tabs.addTab(tab, "Exclude")

    def _exclude_list(self, form, label, values):
        form.addWidget(QLabel(label))
        frame = QHBoxLayout()
        listbox = QListWidget()
        listbox.setMaximumHeight(100)
        for value in values:
            QListWidgetItem(value, listbox)
        frame.addWidget(listbox)
        side = QVBoxLayout()
        entry = QLineEdit()
        entry.setMinimumWidth(240)
        side.addWidget(entry)

        def add_pattern():
            text = entry.text().strip()
            if text:
                QListWidgetItem(text, listbox)
                entry.clear()

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(add_pattern)
        side.addWidget(add_btn)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(lambda: self._remove_from(listbox))
        side.addWidget(remove_btn)
        side.addStretch(1)
        frame.addLayout(side)
        form.addLayout(frame)
        return listbox

    def _tab_filters(self, tabs):
        tab = QWidget()
        form = QVBoxLayout(tab)
        form.addWidget(QLabel("Filters:"))
        frame = QHBoxLayout()
        self.filter_list = QListWidget()
        self.filter_list.setMaximumHeight(180)
        frame.addWidget(self.filter_list)
        form.addLayout(frame)
        editor = QHBoxLayout()
        editor.addWidget(QLabel("Name:"))
        self.edit_filter_name = QLineEdit()
        self.edit_filter_name.setMaximumWidth(140)
        editor.addWidget(self.edit_filter_name)
        editor.addWidget(QLabel("Search:"))
        self.edit_filter_query = QLineEdit()
        editor.addWidget(self.edit_filter_query)
        add_btn = QPushButton("Add / Update")
        add_btn.clicked.connect(self._save_filter)
        editor.addWidget(add_btn)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_filter)
        editor.addWidget(remove_btn)
        form.addLayout(editor)
        self.filter_list.itemSelectionChanged.connect(self._load_filter)
        self._refresh_filters()
        tabs.addTab(tab, "Filters")

    # -- list helpers -------------------------------------------------------
    def _remove_from(self, listbox):
        for item in listbox.selectedItems():
            row = listbox.row(item)
            listbox.takeItem(row)

    def _add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select folder to index")
        if path:
            QListWidgetItem(path, self.folder_list)

    # -- filters ------------------------------------------------------------
    def _save_filter(self):
        name = self.edit_filter_name.text().strip()
        if not name:
            return
        query = self.edit_filter_query.text().strip()
        filters = [f for f in self.draft["filters"] if f["name"] != name]
        filters.append({"name": name, "query": query, "sort": "name"})
        filters.sort(key=lambda f: f["name"].lower())
        self.draft["filters"] = filters
        self._refresh_filters()

    def _remove_filter(self):
        item = self.filter_list.currentItem()
        if item is None:
            return
        row = self.filter_list.row(item)
        name = self.draft["filters"][row]["name"]
        self.draft["filters"] = [f for f in self.draft["filters"] if f["name"] != name]
        self._refresh_filters()

    def _refresh_filters(self):
        self.filter_list.clear()
        for flt in self.draft["filters"]:
            QListWidgetItem("%s  ->  %s" % (flt["name"], flt.get("query", "")),
                            self.filter_list)

    def _load_filter(self):
        item = self.filter_list.currentItem()
        if item is None:
            return
        flt = self.draft["filters"][self.filter_list.row(item)]
        self.edit_filter_name.setText(flt["name"])
        self.edit_filter_query.setText(flt.get("query", ""))

    # -- apply ---------------------------------------------------------------
    def _collect(self):
        d = self.draft
        d["general"]["max_results"] = max(1, self.spin_max_results.value())
        d["general"]["search_history_max"] = max(0, self.spin_history_max.value())
        d["general"]["always_on_top"] = self.chk_always_on_top.isChecked()
        d["general"]["confirm_delete"] = self.chk_confirm_delete.isChecked()
        for key, chk in self.search_checks.items():
            d["search"][key] = chk.isChecked()
        d["search"]["folders_first"] = self.chk_folders_first.isChecked()
        d["search"]["sort"] = self._label_to_key.get(
            self.combo_default_sort.currentText(), "name")
        d["indexes"]["volumes"] = [drive for drive, chk in self.vol_checks.items()
                                   if chk.isChecked()]
        d["indexes"]["folders"] = [self.folder_list.item(i).text()
                                   for i in range(self.folder_list.count())]
        d["indexes"]["exclude_folders"] = [self.exc_folders.item(i).text()
                                           for i in range(self.exc_folders.count())]
        d["indexes"]["exclude_files"] = [self.exc_files.item(i).text()
                                         for i in range(self.exc_files.count())]
        d["indexes"]["follow_junctions"] = self.chk_follow_junctions.isChecked()
        d["indexes"]["rescan_interval"] = max(10, self.spin_rescan.value())

    def _apply(self):
        old_roots = self.cfg.roots()
        self._collect()
        self.cfg.data = self.draft
        self.cfg.save()
        if old_roots != self.cfg.roots():
            self.parent.on_roots_changed()
        self.parent.reload_from_config()

    def _ok(self):
        self._apply()
        self.close()


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, cfg, index, initial_query="", initial_opts=None):
        super().__init__()
        self.cfg = cfg
        self.index = index
        self.initial_opts = initial_opts
        self.search_qid = 0
        self.current_qid = 0
        self._root_status = {}
        self._last_banner_update = 0.0
        self._index_first_build = True
        self._index_user_requested = False
        self.search_worker = None
        self._sort_key = cfg.search["sort"]
        self._sort_asc = bool(cfg.search["sort_ascending"])
        self._icon_cache = {}
        self._options_dialog = None

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(640, 360)
        self._restore_geometry()

        self._build_menus()
        self._build_central()
        self.search_edit.setText(initial_query or "")

        self.index_worker = IndexWorker(self.index, self.cfg)
        self.index_worker.started.connect(self._on_index_started)
        self.index_worker.progress.connect(self._on_index_progress)
        self.index_worker.root_done.connect(self._on_index_root_done)
        self.index_worker.done.connect(self._on_index_done)
        self.index_worker.failed.connect(self._on_index_failed)
        self.index_worker.start()

        QTimer.singleShot(300, self._run_search)

    # -- construction -----------------------------------------------------
    def _restore_geometry(self):
        blob = self.cfg.data["window"].get("geometry_b64")
        restored = False
        if blob:
            try:
                self.restoreGeometry(base64.b64decode(blob))
                restored = True
            except Exception:
                restored = False
        if not restored:
            try:
                w, h = self.cfg.data["window"].get("geometry", "1020x680").split("x")
                self.resize(int(w), int(h))
            except ValueError:
                self.resize(1020, 680)
        self._apply_always_on_top(bool(self.cfg.general["always_on_top"]))

    def _apply_always_on_top(self, on):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()

    def _build_central(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 4)

        # search bar
        bar = QHBoxLayout()
        self.search_edit = SearchEdit(self)
        self.search_edit.setFont(self._search_font())
        self.search_edit.textChanged.connect(self._on_search_change)
        self.search_edit.returnPressed.connect(self._on_search_return)
        bar.addWidget(self.search_edit, 1)
        self.history_button = QToolButton()
        self.history_button.setText("▼")
        self.history_button.setAutoRaise(True)
        self.history_button.clicked.connect(self.show_history)
        bar.addWidget(self.history_button)
        bar.addSpacing(10)
        bar.addWidget(QLabel("Filter:"))
        self.filter_box = QComboBox()
        self.filter_box.addItems([f["name"] for f in self.cfg.filters])
        self.filter_box.setCurrentIndex(0)
        self.filter_box.setMinimumContentsLength(10)
        self.filter_box.activated.connect(self._on_filter_selected)
        bar.addWidget(self.filter_box)
        layout.addLayout(bar)

        # match option toggles
        toggles = QHBoxLayout()
        self._toggle_checks = {}
        opts = self.initial_opts
        for key, label in TOGGLES:
            default = bool(self.cfg.search[key])
            if opts is not None:
                default = bool(getattr(opts, key, default))
            chk = QCheckBox(label)
            chk.setChecked(default)
            chk.stateChanged.connect(self._on_toggle)
            self._toggle_checks[key] = chk
            toggles.addWidget(chk)
        self.chk_folders_first = QCheckBox("Folders first")
        self.chk_folders_first.setChecked(bool(self.cfg.search["folders_first"]))
        self.chk_folders_first.stateChanged.connect(self._on_toggle)
        toggles.addWidget(self.chk_folders_first)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        # index-building banner (visible until the first full scan completes)
        self.index_banner = QLabel()
        self.index_banner.setStyleSheet(
            "background:#fff3cd; color:#7a5b00; padding:4px 8px;"
            "border:1px solid #e6c56a; border-radius:3px;")
        self.index_banner.setWordWrap(False)
        self.index_banner.hide()
        layout.addWidget(self.index_banner)

        # result list
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_right_click)
        self.tree.itemDoubleClicked.connect(lambda _item: self.open_selected())
        header = self.tree.header()
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._on_header_clicked)
        self.tree.headerItem().setText(0, "Name")
        widths = self.cfg.data["window"]["columns"]
        self.tree.setColumnWidth(0, int(widths.get("name", 260)))
        for i, (key, label, width, _visible) in enumerate(COLUMNS, start=1):
            self.tree.headerItem().setText(i, label)
            self.tree.setColumnWidth(i, int(widths.get(key, width)))
        self._apply_column_visibility()
        layout.addWidget(self.tree, 1)

        # status bar
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(140)
        self.progress.setRange(0, 0)  # indeterminate
        self.progress.hide()
        self.objects_label = QLabel("0 objects")
        self.objects_label.setMinimumWidth(90)
        self.status_label = QLabel("Starting...")
        self.size_label = QLabel("")
        self.size_label.setMinimumWidth(90)
        self.size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sb = self.statusBar()
        sb.addWidget(self.progress)
        sb.addWidget(self.objects_label)
        sb.addPermanentWidget(self.size_label)
        sb.addWidget(self.status_label, 1)

    def _search_font(self):
        font = self.font()
        font.setPointSize(font.pointSize() + 1)
        return font

    def _build_menus(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        file_menu.addAction("Open", self.open_selected, "Return")
        file_menu.addAction("Open Path", self.open_path_selected, "Ctrl+Return")
        file_menu.addAction("Explore", self.explore_selected)
        file_menu.addSeparator()
        file_menu.addAction("Copy Full Name", self.copy_full_names)
        file_menu.addAction("Copy Name", self.copy_names)
        file_menu.addAction("Copy Path", self.copy_paths)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close, "Alt+F4")

        edit_menu = menubar.addMenu("Edit")
        edit_menu.addAction("Select All", self.select_all, "Ctrl+A")
        edit_menu.addAction("Copy", self.copy_full_names, "Ctrl+C")
        edit_menu.addSeparator()
        edit_menu.addAction("Delete", self.delete_selected, "Del")
        edit_menu.addAction("Properties", self.properties_selected)

        view_menu = menubar.addMenu("View")
        sort_menu = view_menu.addMenu("Sort By")
        self.sort_group = QActionGroup(self)
        self.sort_group.setExclusive(True)
        self._sort_actions = {}
        for key in SORT_ORDER:
            act = QAction(SORT_LABELS[key], self)
            act.setCheckable(True)
            act.setChecked(key == self._sort_key)
            act.triggered.connect(lambda _checked, k=key: self._set_sort_key(k))
            self.sort_group.addAction(act)
            sort_menu.addAction(act)
            self._sort_actions[key] = act
        sort_menu.addSeparator()
        sort_menu.addAction("Ascending", lambda: self._set_sort_direction(True))
        sort_menu.addAction("Descending", lambda: self._set_sort_direction(False))
        self.chk_folders_first_menu = view_menu.addAction("Sort folders first")
        self.chk_folders_first_menu.setCheckable(True)
        self.chk_folders_first_menu.setChecked(
            bool(self.cfg.search["folders_first"]))
        self.chk_folders_first_menu.triggered.connect(self._on_toggle)
        columns_menu = view_menu.addMenu("Columns")
        self._column_actions = {}
        for i, (key, label, _width, visible) in enumerate(COLUMNS):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(visible)
            act.triggered.connect(lambda _checked, k=key: self._apply_column_visibility())
            columns_menu.addAction(act)
            self._column_actions[key] = act
        self.chk_statusbar = view_menu.addAction("Status Bar")
        self.chk_statusbar.setCheckable(True)
        self.chk_statusbar.setChecked(True)
        self.chk_statusbar.triggered.connect(self._toggle_statusbar)
        view_menu.addSeparator()
        view_menu.addAction("Refresh", self.rescan, "F5")
        view_menu.addAction("Force Rebuild", self.rebuild_index, "Shift+F5")

        search_menu = menubar.addMenu("Search")
        self._menu_toggle_actions = {}
        for key, label in TOGGLES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(bool(self.cfg.search[key]))
            act.triggered.connect(self._on_toggle)
            search_menu.addAction(act)
            self._menu_toggle_actions[key] = act
        search_menu.addSeparator()
        search_menu.addAction("Clear", self.clear_search, "Esc")
        search_menu.addAction("Search History", self.show_history)
        search_menu.addAction("Clear Search History", self._clear_history)

        self.bookmarks_menu = menubar.addMenu("Bookmarks")
        self.bookmarks_menu.addAction("Add Bookmark...", self.add_bookmark)
        self.bookmarks_menu.addAction("Organize Bookmarks...", self.organize_bookmarks)
        self.bookmarks_menu.addSeparator()
        self._refresh_bookmark_menu()

        self.filters_menu = menubar.addMenu("Filters")
        self.filters_menu.aboutToShow.connect(self._refresh_filters_menu)
        self.filters_menu.addSeparator()
        self.filters_menu.addAction("Edit Filters...", self.show_options)

        tools_menu = menubar.addMenu("Tools")
        tools_menu.addAction("Options...", self.show_options)

        help_menu = menubar.addMenu("Help")
        help_menu.addAction("About", self._show_about)

        # keyboard shortcuts
        self._add_shortcut("Ctrl+E", lambda: self.search_edit.setFocus())
        self._add_shortcut("Ctrl+A", self.select_all)
        self._add_shortcut("Ctrl+C", self.copy_full_names)
        self._add_shortcut("F5", self.rescan)
        self._add_shortcut("Shift+F5", self.rebuild_index)

    def _add_shortcut(self, keys, slot):
        QShortcut(QKeySequence(keys), self, slot)

    # -- search -----------------------------------------------------------
    def _on_search_change(self):
        self._start_debounce()

    def _start_debounce(self):
        self._debounce_timer = getattr(self, "_debounce_timer", None)
        if self._debounce_timer is None:
            self._debounce_timer = QTimer(self)
            self._debounce_timer.setSingleShot(True)
            self._debounce_timer.timeout.connect(self._run_search)
        self._debounce_timer.start(150)

    def _on_search_return(self):
        if getattr(self, "_debounce_timer", None) is not None:
            self._debounce_timer.stop()
        self.cfg.add_history(self.search_edit.text())
        self.cfg.save()
        self._run_search()

    def clear_search(self):
        self.search_edit.clear()
        self.search_edit.setFocus()
        self._run_search()

    def current_options(self):
        opts = SearchOptions.from_config(self.cfg)
        for key, _label in TOGGLES:
            setattr(opts, key, self._toggle_checks[key].isChecked())
        opts.folders_first = self.chk_folders_first.isChecked()
        opts.sort = self._sort_key
        opts.sort_ascending = self._sort_asc
        return opts

    def _run_search(self):
        if self.search_worker is not None:
            self.search_worker.stop()
        self.search_qid += 1
        self.current_qid = self.search_qid
        self.tree.clear()
        query = self.search_edit.text()
        self.search_worker = SearchWorker(self.index, query, self.current_options(),
                                          self.search_qid)
        self.search_worker.batch.connect(self._on_search_batch)
        self.search_worker.finished.connect(self._on_search_finished)
        self.search_worker.failed.connect(self._on_search_failed)
        self.search_worker.start()
        self.status_label.setText("Searching...")
        self.progress.setRange(0, 0)
        self.progress.show()

    def _on_filter_selected(self, _index=0):
        self.apply_filter(self.filter_box.currentText())

    def apply_filter(self, name):
        flt = self.cfg.filter_by_name(name)
        if flt is None:
            return
        self.filter_box.setCurrentText(flt["name"])
        self.search_edit.setText(
            combine_query(flt.get("query", ""), self.search_edit.text()))
        self._run_search()

    def _on_toggle(self, _checked=None):
        for key, _label in TOGGLES:
            self.cfg.search[key] = self._toggle_checks[key].isChecked()
            act = self._menu_toggle_actions.get(key)
            if act is not None:
                act.setChecked(self._toggle_checks[key].isChecked())
        self.cfg.search["folders_first"] = self.chk_folders_first.isChecked()
        self.chk_folders_first_menu.setChecked(
            self.chk_folders_first.isChecked())
        self.cfg.save()
        self._run_search()

    # -- sorting ----------------------------------------------------------
    def _on_header_clicked(self, section):
        if section == 0:
            key = "name"
        else:
            key = COLUMNS[section - 1][0]
        sort_key = COLUMN_TO_SORT.get(key, "name")
        if sort_key == self._sort_key:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_key = sort_key
            self._sort_asc = True
        self._sync_sort_ui()
        self.cfg.search["sort"] = self._sort_key
        self.cfg.search["sort_ascending"] = self._sort_asc
        self.cfg.save()
        self._run_search()

    def _set_sort_key(self, key):
        if key == self._sort_key:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_key = key
            self._sort_asc = True
        self._sync_sort_ui()
        self.cfg.search["sort"] = self._sort_key
        self.cfg.search["sort_ascending"] = self._sort_asc
        self.cfg.save()
        self._run_search()

    def _set_sort_direction(self, ascending):
        self._sort_asc = bool(ascending)
        self._sync_sort_ui()
        self.cfg.search["sort_ascending"] = self._sort_asc
        self.cfg.save()
        self._run_search()

    def _sync_sort_ui(self):
        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        if self._sort_key == "name":
            visual = 0
        else:
            column = _sort_key_to_column(self._sort_key)
            visual = [k for k, _l, _w, _v in COLUMNS].index(column) + 1
        self.tree.header().setSortIndicator(visual, order)
        for key, act in self._sort_actions.items():
            act.setChecked(key == self._sort_key)

    # -- history / bookmarks ----------------------------------------------
    def show_history(self):
        history = [h for h in self.cfg.history if h.strip()]
        if not history:
            return
        popup = HistoryPopup(self, self._use_history_entry)
        popup.fill(history, self.search_edit.width())
        point = self.search_edit.mapToGlobal(self.search_edit.rect().bottomLeft())
        popup.move(point)
        popup.show()
        popup.setFocus()
        self._history_popup = popup

    def _use_history_entry(self, text):
        self.search_edit.setText(text)
        self._run_search()

    def _clear_history(self):
        self.cfg.clear_history()
        self.cfg.save()

    def _refresh_bookmark_menu(self):
        # keep "Add Bookmark...", "Organize Bookmarks..." and the separator
        for act in self.bookmarks_menu.actions()[3:]:
            self.bookmarks_menu.removeAction(act)
        for bookmark in self.cfg.bookmarks:
            act = QAction(bookmark["name"], self)
            act.triggered.connect(
                lambda _checked, b=bookmark: self._use_bookmark(b))
            self.bookmarks_menu.addAction(act)

    def _use_bookmark(self, bookmark):
        self.search_edit.setText(bookmark.get("query", ""))
        if bookmark.get("match_case") is not None:
            self._toggle_checks["match_case"].setChecked(
                bool(bookmark["match_case"]))
        if bookmark.get("sort"):
            key, asc = parse_sort_term(bookmark["sort"])
            self._sort_key, self._sort_asc = key, asc
            self._sync_sort_ui()
        self._run_search()

    def add_bookmark(self):
        name, ok = QInputDialog.getText(self, "Add Bookmark", "Bookmark name:")
        if not ok or not name.strip():
            return
        self.cfg.add_bookmark(name.strip(), self.search_edit.text(),
                              sort=self._sort_key,
                              match_case=self._toggle_checks["match_case"].isChecked())
        self.cfg.save()
        self._refresh_bookmark_menu()

    def organize_bookmarks(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Organize Bookmarks")
        dialog.resize(460, 320)
        layout = QVBoxLayout(dialog)
        listbox = QListWidget()
        for bookmark in self.cfg.bookmarks:
            QListWidgetItem("%s  ->  %s" % (bookmark["name"],
                                            bookmark.get("query", "")), listbox)
        layout.addWidget(listbox)

        def remove():
            for item in listbox.selectedItems():
                row = listbox.row(item)
                self.cfg.remove_bookmark(self.cfg.bookmarks[row]["name"])
                listbox.takeItem(row)
            self.cfg.save()
            self._refresh_bookmark_menu()

        button = QPushButton("Remove")
        button.clicked.connect(remove)
        layout.addWidget(button)
        dialog.show()
        self._organize_dialog = dialog

    def _refresh_filters_menu(self):
        actions = self.filters_menu.actions()
        separator = actions[-2]
        for act in actions[:-2]:
            self.filters_menu.removeAction(act)
        for flt in self.cfg.filters:
            act = QAction(flt["name"], self)
            act.triggered.connect(
                lambda _checked, name=flt["name"]: self.apply_filter(name))
            self.filters_menu.insertAction(separator, act)

    # -- result actions ----------------------------------------------------
    def _selected_rows(self):
        rows = []
        for item in self.tree.selectedItems():
            row = item.data(0, Qt.UserRole)
            if row is not None:
                rows.append(row)
        return rows

    def _full_path(self, row):
        return row["parent"] + row["name"]

    def open_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        for row in rows[:10]:
            path = self._full_path(row)
            try:
                open_file(path)
            except Exception as exc:
                QMessageBox.critical(self, "FileScout",
                                     "Cannot open %s:\n%s" % (path, exc))
                break

    def open_path_selected(self):
        for row in self._selected_rows():
            open_path(self._full_path(row))

    def explore_selected(self):
        for row in self._selected_rows():
            path = self._full_path(row)
            open_folder(path if row["is_folder"] else os.path.dirname(path))

    def copy_full_names(self):
        rows = self._selected_rows()
        if rows:
            self._copy("\n".join(self._full_path(r) for r in rows))

    def copy_names(self):
        rows = self._selected_rows()
        if rows:
            self._copy("\n".join(r["name"] for r in rows))

    def copy_paths(self):
        rows = self._selected_rows()
        if rows:
            self._copy("\n".join(r["parent"] for r in rows))

    def _copy(self, text):
        QApplication.clipboard().setText(text)

    def delete_selected(self):
        rows = self._selected_rows()
        if not rows:
            return
        if self.cfg.general["confirm_delete"]:
            answer = QMessageBox.question(
                self, "FileScout",
                "Move %d item(s) to the Recycle Bin?" % len(rows),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        paths = [self._full_path(r) for r in rows]
        if not recycle(paths):
            QMessageBox.critical(self, "FileScout",
                                 "Failed to delete the selected items.")
            return
        self.rescan()

    def properties_selected(self):
        for row in self._selected_rows()[:1]:
            if not show_properties(self._full_path(row)):
                QMessageBox.information(
                    self, "FileScout",
                    "Properties are only available on Windows.")

    def select_all(self):
        self.tree.selectAll()
        return True

    def _on_tree_right_click(self, pos):
        # customContextMenuRequested delivers viewport-relative coordinates,
        # the same space itemAt() and viewport().mapToGlobal() expect. Do NOT
        # map through the parent here: subtracting the header height shifts
        # the hit test up by ~one row and the menu would act on the row above.
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.tree.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction("Open", self.open_selected)
        menu.addAction("Open Path", self.open_path_selected)
        menu.addAction("Explore", self.explore_selected)
        menu.addSeparator()
        menu.addAction("Copy Full Name", self.copy_full_names)
        menu.addAction("Copy Name", self.copy_names)
        menu.addAction("Copy Path", self.copy_paths)
        menu.addSeparator()
        menu.addAction("Delete", self.delete_selected)
        menu.addAction("Properties", self.properties_selected)
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    # -- view helpers ------------------------------------------------------
    def _apply_column_visibility(self):
        for i, (key, _label, _width, _visible) in enumerate(COLUMNS, start=1):
            hidden = not self._column_actions[key].isChecked()
            self.tree.setColumnHidden(i, hidden)
        self._sync_sort_ui()

    def _toggle_statusbar(self, checked=None):
        checked = self.chk_statusbar.isChecked() if checked is None else checked
        self.statusBar().setVisible(bool(checked))

    # -- index control ------------------------------------------------------
    def rescan(self):
        self.index_worker.request_rescan()
        self.status_label.setText("Rescanning...")
        self.progress.setRange(0, 0)
        self.progress.show()

    def rebuild_index(self):
        answer = QMessageBox.question(
            self, "FileScout", "Rebuild the entire index from scratch?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.index_worker.request_rebuild()
        self.status_label.setText("Rebuilding index...")
        self.progress.setRange(0, 0)
        self.progress.show()

    def on_roots_changed(self):
        self.rescan()

    def reload_from_config(self):
        self._apply_always_on_top(bool(self.cfg.general["always_on_top"]))
        self.filter_box.clear()
        self.filter_box.addItems([f["name"] for f in self.cfg.filters])
        self.filter_box.setCurrentIndex(0)
        for key, _label in TOGGLES:
            self._toggle_checks[key].setChecked(bool(self.cfg.search[key]))
            self._menu_toggle_actions[key].setChecked(bool(self.cfg.search[key]))
        self.chk_folders_first.setChecked(bool(self.cfg.search["folders_first"]))
        self.chk_folders_first_menu.setChecked(
            bool(self.cfg.search["folders_first"]))
        self._sort_key = self.cfg.search["sort"]
        self._sort_asc = bool(self.cfg.search["sort_ascending"])
        self._sync_sort_ui()
        self._run_search()

    def show_options(self):
        if self._options_dialog is not None:
            self._options_dialog.raise_()
            self._options_dialog.activateWindow()
            return
        self._options_dialog = OptionsDialog(self)
        self._options_dialog.finished.connect(self._on_options_closed)
        self._options_dialog.show()

    def _on_options_closed(self):
        self._options_dialog = None

    # -- worker slots --------------------------------------------------------
    def _on_index_started(self, label, first_build, user_requested):
        self._index_first_build = bool(first_build)
        self._index_user_requested = bool(user_requested)
        self._root_status = {}
        if first_build:
            # the index really is incomplete: show progress + banner
            self.progress.setRange(0, 0)
            self.progress.show()
            self.status_label.setText(label + "...")
            self._root_status = {r: r + " 等待中" for r in self.cfg.roots()}
            self._refresh_banner()
        elif user_requested:
            # F5 / roots changed: the user asked, so show busy feedback
            self.progress.setRange(0, 0)
            self.progress.show()
            self.status_label.setText(label + "...")
            self.index_banner.hide()
        else:
            # periodic rescan of a complete index: stay quiet, no banner
            self.progress.hide()
            self.index_banner.hide()
            self.status_label.setText("Updating index...")

    def _refresh_banner(self):
        parts = []
        for r in self.cfg.roots():
            parts.append(self._root_status.get(r, r + " 等待中"))
        self.index_banner.setText(
            "索引构建中，结果尚不完整   |   " + "  ·  ".join(parts))
        self.index_banner.show()

    def _on_index_progress(self, directory, count):
        if not self._index_first_build and not self._index_user_requested:
            return  # periodic rescans stay quiet
        root = directory
        low = directory.lower()
        for r in self.cfg.roots():
            if low.startswith(r.lower()):
                root = r
                break
        self.status_label.setText("Indexing %s - %s objects"
                                  % (root, format(count, ",")))
        # Every volume keeps a fresh status even while the banner *text* is
        # throttled - updating the status after the throttle check left quiet
        # volumes stuck on "等待中" until their own next signal came through.
        self._root_status[root] = "%s 扫描中 %s" % (root, format(count, ","))
        # throttle banner updates: three volumes emit progress concurrently
        now = time.monotonic()
        if now - self._last_banner_update < 0.5:
            return
        self._last_banner_update = now
        self._refresh_banner()

    def _on_index_root_done(self, root, n, errors):
        if self._index_first_build:
            if n == 0 and not errors:
                self._root_status[root] = "%s 已是最新" % root
            else:
                self._root_status[root] = "%s 完成" % root
            self._refresh_banner()
        try:
            count, _size = self.index.stats()
            self.objects_label.setText("%d objects" % count)
        except Exception:
            pass

    def _on_index_done(self, _n, errors=0, samples=()):
        self.progress.hide()
        self.index_banner.hide()
        self._root_status = {}
        count, _size = self.index.stats()
        seconds = float(self.index.get_meta("last_scan_seconds", "0") or 0)
        if errors:
            detail = "; ".join("%s (%s)" % (p, r) for p, r in samples)
            self.status_label.setText(
                "Ready - %d objects indexed in %.1fs - %d folders could not "
                "be read" % (count, seconds, errors))
            self.status_label.setToolTip(
                "These folders were skipped (run as administrator to index "
                "more):\n" + detail)
        elif self._index_first_build:
            self.status_label.setText("Ready - %d objects indexed in %.1fs"
                                      % (count, seconds))
            self.status_label.setToolTip("")
        else:
            self.status_label.setText("Ready - %d objects (index updated in "
                                      "%.1fs)" % (count, seconds))
            self.status_label.setToolTip("")
        self.objects_label.setText("%d objects" % count)

    def _on_index_failed(self, message):
        self.progress.hide()
        self.status_label.setText("Index error: " + message)

    def _on_search_batch(self, qid, rows):
        if qid != self.current_qid:
            return  # stale result
        folder_icon = self._icon("folder")
        for row in rows:
            item = QTreeWidgetItem()
            item.setIcon(0, folder_icon if row["is_folder"]
                         else self._file_icon(row))
            item.setText(0, row["name"])
            values = self._row_values(row)
            for i, value in enumerate(values, start=1):
                item.setText(i, value)
            if not row["is_folder"]:
                item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            item.setData(0, Qt.UserRole, row)
            self.tree.addTopLevelItem(item)

    def _on_search_finished(self, qid, truncated, total_size, took):
        if qid != self.current_qid:
            return
        self.progress.hide()
        count = self.tree.topLevelItemCount()
        label = "%d object%s" % (count, "" if count == 1 else "s")
        if truncated:
            label += " (limit reached)"
        self.objects_label.setText(label)
        self.size_label.setText(format_size(total_size) if total_size else "")
        self.status_label.setText("Search finished in %.0f ms" % (took * 1000))

    def _on_search_failed(self, qid, message):
        if qid != self.current_qid:
            return
        self.progress.hide()
        self.status_label.setText("Search error: " + message)

    def _row_values(self, row):
        parent = row["parent"]
        display_parent = parent[:-1] if parent.endswith("\\") and len(parent) > 3 else parent
        return (
            display_parent,
            "" if row["is_folder"] else format_size(row["size"]),
            row["extension"],
            format_ts(row["date_modified"]),
            format_ts(row["date_created"]),
            format_ts(row["date_accessed"]),
            format_attributes(row["attributes"]),
        )

    def _icon(self, name):
        icon = self._icon_cache.get(name)
        if icon is None:
            icon = _folder_icon() if name == "folder" else _file_icon()
            self._icon_cache[name] = icon
        return icon

    def _file_icon(self, row):
        category = _icon_category(row)
        name = "file:" + (category or "default")
        icon = self._icon_cache.get(name)
        if icon is None:
            icon = _file_icon(_ICON_FILLS.get(category, "#ffffff"))
            self._icon_cache[name] = icon
        return icon

    # -- about / close ------------------------------------------------------
    def _show_about(self):
        QMessageBox.about(
            self, "About FileScout",
            "<b>FileScout %s</b><p>"
            "Instant filename search over a local SQLite index: wildcard and"
            " boolean queries, size/date filters, regex, content search and"
            " CSV/TXT/EFU export. Built with Python and PyQt5."
            % __version__)

    def closeEvent(self, event):
        try:
            self.index_worker.stop()
            self.index_worker.wait(3000)
        except Exception:
            pass
        if self.search_worker is not None:
            try:
                self.search_worker.stop()
                self.search_worker.wait(1000)
            except Exception:
                pass
        self.cfg.add_history(self.search_edit.text())
        blob = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        self.cfg.data["window"]["geometry_b64"] = blob
        self.cfg.data["window"]["columns"]["name"] = self.tree.columnWidth(0)
        for i, (key, _label, _width, _visible) in enumerate(COLUMNS, start=1):
            self.cfg.data["window"]["columns"][key] = self.tree.columnWidth(i)
        try:
            self.cfg.save()
        except OSError:
            pass
        try:
            self.index.close()
        except Exception:
            pass
        event.accept()


def _sort_key_to_column(sort_key):
    for column, key in COLUMN_TO_SORT.items():
        if key == sort_key:
            return column
    return "name"


def run_gui(cfg, index=None, initial_query="", initial_opts=None):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(cfg, index or Index(cfg.db_path), initial_query, initial_opts)
    window.show()
    return app.exec_()
