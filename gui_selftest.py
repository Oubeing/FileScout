"""Real PyQt5 GUI smoke test (offscreen platform).

Unlike the old tkinter stub test, this drives the actual Qt widgets: it builds
a real MainWindow, waits for the index worker and streaming search results,
exercises sorting, toggles, bookmarks, history, the options dialog and the
close path.

Run:  QT_QPA_PLATFORM=offscreen python gui_selftest.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE = tempfile.mkdtemp(prefix="fcgui_")
HOME = os.path.join(BASE, "home")
ROOT = os.path.join(BASE, "root")
ROOT2 = os.path.join(BASE, "vol2")
os.environ["FILESCOUT_HOME"] = HOME
os.environ["FILESCOUT_ROOTS"] = ROOT + ";" + ROOT2

# test tree
os.makedirs(os.path.join(ROOT, "docs"))
os.makedirs(os.path.join(ROOT, "pics"))
os.makedirs(os.path.join(ROOT, "empty_dir"))
files = [
    ("readme.txt", "hello world\n"),
    ("docs/notes.md", "# notes\n"),
    ("docs/report.pdf", "%PDF-1.4 fake\n"),
    ("pics/cat.jpg", "\xff\xd8\xff jpeg"),
    ("pics/dog.png", "\x89PNG fake"),
    ("main.py", "print('hi')\n"),
    ("utils.py", "def f():\n    return 1\n"),
    ("app.js", "console.log(1)\n"),
    ("archive.zip", "PK\x03\x04 fake zip"),
    ("song.mp3", "ID3 fake audio"),
]
for rel, content in files:
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(content)
with open(os.path.join(ROOT, "docs", "buried.txt"), "w") as fh:
    fh.write("deep\n")

# second "volume" (only non-.py files so the *.py count stays 2)
os.makedirs(os.path.join(ROOT2, "data"))
with open(os.path.join(ROOT2, "notes2.txt"), "w") as fh:
    fh.write("second volume\n")
with open(os.path.join(ROOT2, "data", "extra.js"), "w") as fh:
    fh.write("console.log(2)\n")

from PyQt5.QtCore import Qt, QPoint  # noqa: E402
from PyQt5.QtGui import QClipboard, QContextMenuEvent  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QInputDialog, QMenu,  # noqa: E402
                             QMessageBox)

from filescout.app import MainWindow  # noqa: E402
from filescout.config import Config  # noqa: E402
from filescout.indexer import Index  # noqa: E402

# make modal dialogs non-blocking
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.critical = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.about = staticmethod(lambda *a, **k: None)
QInputDialog.getText = staticmethod(lambda *a, **k: ("gui-bookmark", True))
QMenu.exec_ = lambda self, *a, **k: None

FAILURES = []


def check(name, condition, note=""):
    status = "PASS" if condition else "FAIL"
    print("  [%s] %s%s" % (status, name, (" - " + note) if note else ""))
    if not condition:
        FAILURES.append(name)


def wait_for(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main():
    app = QApplication([])
    cfg = Config()
    index = Index(cfg.db_path)
    index.scan(cfg.roots())
    print("pre-indexed %d objects" % index.stats()[0])

    window = MainWindow(cfg, index)
    window.show()

    # 0. rebranding: no external references anywhere in the UI
    check("window title rebranded", window.windowTitle() == "FileScout",
          window.windowTitle())
    check("default filter renamed",
          window.filter_box.currentText() == "All",
          window.filter_box.currentText())
    menus = {a.text(): a.menu() for a in window.menuBar().actions() if a.menu()}
    help_texts = [a.text() for a in menus.get("Help").actions()]
    check("Help menu has no website link",
          not any("website" in t.lower() or "http" in t.lower()
                  for t in help_texts), str(help_texts))
    about_calls = []
    QMessageBox.about = staticmethod(lambda *a, **k: about_calls.append(a))
    try:
        window._show_about()
    finally:
        QMessageBox.about = staticmethod(lambda *a, **k: None)
    about_text = about_calls[0][2] if about_calls else ""
    check("About dialog rebranded without links",
          "FileScout" in about_text and "voidtools" not in about_text.lower()
          and "http" not in about_text.lower(), about_text[:60])

    # 1. index worker finishes the startup scan
    ok = wait_for(lambda: "Ready" in window.status_label.text(), timeout=30)
    check("startup index scan finishes", ok, window.status_label.text())
    total = index.stats()[0]
    check("status bar shows object count",
          window.objects_label.text() == "%d objects" % total,
          window.objects_label.text())
    check("index banner hidden after scan", window.index_banner.isHidden())
    roots = cfg.roots()
    check("two volumes configured", len(roots) == 2, str(roots))
    check("both volumes marked done in meta",
          all(index.get_meta("root_done:" + r.lower()) for r in roots),
          str([(r, index.get_meta("root_done:" + r.lower())) for r in roots]))

    # 1b. banner shows while the index is incomplete and hides when done
    # (driven directly: a rebuild of this tiny tree finishes in milliseconds)
    window._on_index_started("Indexing", True, False)
    check("index banner shown during first build",
          not window.index_banner.isHidden())
    window._on_index_progress("C:\\Users\\", 1234)
    check("banner tracks volume being scanned",
          "扫描中" in window._root_status.get("C:\\Users\\", ""),
          str(window._root_status))
    check("status bar shows live object count",
          "1,234" in window.status_label.text(),
          window.status_label.text())
    window._on_index_root_done("C:\\", 500, 0)
    check("root_done marks volume complete",
          "完成" in window._root_status.get("C:\\", ""),
          str(window._root_status))
    window._on_index_done(total, 0, [])
    check("index banner hidden after first build", window.index_banner.isHidden())

    # 1c. a periodic rescan of an already complete index stays quiet
    window._on_index_started("Indexing", False, False)
    check("periodic rescan shows no banner", window.index_banner.isHidden())
    window._on_index_progress("C:\\Users\\", 1234)
    check("periodic rescan leaves the status bar alone",
          window.status_label.text() == "Updating index...",
          window.status_label.text())
    window._on_index_done(total, 0, [])
    check("periodic rescan finishes quietly",
          "index updated" in window.status_label.text(),
          window.status_label.text())

    # 2. streaming search: *.py
    window.search_edit.setText("*.py")
    ok = wait_for(lambda: window.tree.topLevelItemCount() == 2)
    check("wildcard search *.py streams 2 results", ok,
          str(window.tree.topLevelItemCount()))

    # 3. search with an empty query
    window.search_edit.setText("")
    ok = wait_for(lambda: window.tree.topLevelItemCount() == total)
    check("empty query lists all objects", ok,
          str(window.tree.topLevelItemCount()))

    # 4. row values
    item = window.tree.topLevelItem(0)
    row = item.data(0, Qt.UserRole)
    check("row payload attached", isinstance(row, dict) and "name" in row)
    check("name column filled", bool(item.text(0)))
    check("size column filled or empty for folders",
          item.text(2) == "" or "bytes" in item.text(2) or "KB" in item.text(2),
          item.text(2))

    # 4b. right-click must target the row under the cursor, not the row above
    # (regression: a stray mapFromParent() shifted the hit test up one row)
    window.search_edit.setText("")
    ok = wait_for(lambda: window.tree.topLevelItemCount() == total)
    check("context-menu test: full list restored", ok,
          str(window.tree.topLevelItemCount()))
    tree = window.tree
    tree.doItemsLayout()
    QApplication.processEvents()
    target_row = min(5, tree.topLevelItemCount() - 1)
    target = tree.topLevelItem(target_row)
    center = tree.visualItemRect(target).center()  # viewport coordinates
    tree.setCurrentItem(None)
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, center,
                           tree.viewport().mapToGlobal(center))
    QApplication.sendEvent(tree.viewport(), ev)
    check("right-click selects the clicked row",
          tree.currentItem() is target,
          "expected %s, got %s" % (
              target.text(0),
              tree.currentItem().text(0) if tree.currentItem() else None))
    rows = window._selected_rows()
    check("right-clicked row becomes the menu target",
          len(rows) == 1 and rows[0]["name"] == target.text(0),
          str([r["name"] for r in rows]))

    # 4c. right-click inside an existing multi-selection keeps it intact,
    # and right-click outside replaces the selection with that row only
    tree.clearSelection()
    tree.setCurrentItem(None)
    for r in range(3):
        tree.topLevelItem(r).setSelected(True)
    inside = tree.topLevelItem(1)
    center = tree.visualItemRect(inside).center()
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, center,
                           tree.viewport().mapToGlobal(center))
    QApplication.sendEvent(tree.viewport(), ev)
    check("right-click inside selection keeps every row selected",
          len(tree.selectedItems()) == 3, str(len(tree.selectedItems())))
    outside = tree.topLevelItem(min(8, tree.topLevelItemCount() - 1))
    center = tree.visualItemRect(outside).center()
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, center,
                           tree.viewport().mapToGlobal(center))
    QApplication.sendEvent(tree.viewport(), ev)
    check("right-click outside selection replaces it",
          tree.selectedItems() == [outside], str(
              [i.text(0) for i in tree.selectedItems()]))

    # 4d. right-click on empty space below the last row does nothing
    tree.clearSelection()
    tree.setCurrentItem(None)
    last = tree.topLevelItem(tree.topLevelItemCount() - 1)
    empty_y = tree.visualItemRect(last).bottom() + 10
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(center.x(), empty_y),
                           tree.viewport().mapToGlobal(QPoint(center.x(), empty_y)))
    QApplication.sendEvent(tree.viewport(), ev)
    check("right-click on empty space selects nothing",
          tree.currentItem() is None and not tree.selectedItems(),
          str([i.text(0) for i in tree.selectedItems()]))

    # 4e. banner keeps every volume's status fresh inside the throttle window
    # (regression: quiet volumes stayed stuck on "等待中" because the status
    # was written after the throttle early-return)
    roots = cfg.roots()
    window._on_index_started("Indexing", True, False)
    window._last_banner_update = time.monotonic()   # force the throttle shut
    for i, r in enumerate(roots[:3]):
        window._on_index_progress(r + "sub\\", 100 * (i + 1))
    statuses = [window._root_status.get(r) for r in roots[:3]]
    check("every volume status updated inside throttle window",
          statuses == ["%s 扫描中 %s" % (r, format(100 * (i + 1), ","))
                       for i, r in enumerate(roots[:3])], str(statuses))
    window._on_index_done(total, 0, [])

    # 5. header click sorting (size column = index 2)
    window._on_header_clicked(2)
    check("header click sets sort key to size", window._sort_key == "size",
          window._sort_key)
    window._on_header_clicked(2)
    check("second header click toggles direction",
          window._sort_asc is False)
    window._on_header_clicked(0)
    check("name header sorts by name", window._sort_key == "name")

    # 6. sort menu radio action
    window._sort_actions["dm"].trigger()
    check("sort menu sets dm", window._sort_key == "dm", window._sort_key)

    # 7. match toggles persist to config
    window._toggle_checks["match_case"].setChecked(True)
    wait_for(lambda: cfg.search["match_case"] is True)
    check("toggle persists to config", cfg.search["match_case"] is True)

    # 8. bookmark add (QInputDialog patched)
    window.add_bookmark()
    names = [b["name"] for b in cfg.bookmarks]
    check("bookmark added", "gui-bookmark" in names, str(names))
    actions = window.bookmarks_menu.actions()
    check("bookmark menu rebuilt",
          any(a.text() == "gui-bookmark" for a in actions))

    # 9. history popup
    cfg.add_history("*.txt")
    cfg.save()
    window.show_history()
    popup = getattr(window, "_history_popup", None)
    check("history popup opens", popup is not None)
    if popup is not None:
        check("history popup has entries", popup.count() > 0)
        popup.close()

    # 10. options dialog open + apply
    window.show_options()
    dialog = window._options_dialog
    check("options dialog opens", dialog is not None)
    if dialog is not None:
        dialog.spin_max_results.setValue(500)
        dialog._apply()
        wait_for(lambda: cfg.general["max_results"] == 500)
        check("options applied to config", cfg.general["max_results"] == 500)
        # reload_from_config re-runs the search: wait for the list to refill
        ok = wait_for(lambda: window.tree.topLevelItemCount() == total)
        check("results refill after options apply", ok,
              str(window.tree.topLevelItemCount()))
        dialog.close()

    # 11. filters menu rebuild
    window.filters_menu.aboutToShow.emit()
    labels = [a.text() for a in window.filters_menu.actions()]
    check("filters menu populated", "Pic" in labels, str(labels[:4]))

    # 12. select all + copy
    window.select_all()
    check("select all selects every row",
          len(window.tree.selectedItems()) == total)
    window.copy_full_names()
    text = QApplication.clipboard().text()
    check("copy full names to clipboard", "main.py" in text, text[:60])

    # 13. column visibility (attributes is the last column, index 7)
    check("attributes column hidden by default", window.tree.isColumnHidden(7))
    window._column_actions["attributes"].trigger()
    check("attributes column shown", not window.tree.isColumnHidden(7))

    # 14. status bar toggle
    window._toggle_statusbar(False)
    check("status bar hidden", not window.statusBar().isVisible())
    window._toggle_statusbar(True)
    check("status bar shown", window.statusBar().isVisible())

    # 15. close persists geometry
    window.close()
    ok = wait_for(lambda: not window.isVisible())
    check("window closed", ok)
    check("geometry persisted",
          bool(cfg.data["window"].get("geometry_b64")))
    check("column widths persisted",
          cfg.data["window"]["columns"].get("name", 0) > 0)

    print()
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("ALL GUI CHECKS PASSED")
    return 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(BASE, ignore_errors=True)
    sys.exit(code)
