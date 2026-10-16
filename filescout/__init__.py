"""FileScout - instant filename search for Windows.

A from-scratch Python implementation of a local filename search engine with
an SQLite backed index: wildcard/boolean query syntax, size and date filters,
regex, content search, CSV/TXT/EFU export and a PyQt5 desktop GUI.

Modules:
    config   - JSON configuration (indexes, excludes, bookmarks, filters, history)
    indexer  - SQLite backed filesystem index with fast incremental rescans
    search   - query language parser/compiler (wildcards, boolean operators,
               macros, sorting, size/date filters, regex, content:)
    cli      - command line interface
    app      - PyQt5 desktop GUI (main window, options, bookmarks)
"""

__version__ = "1.0.0"
APP_NAME = "FileScout"
APP_TITLE = "FileScout"
