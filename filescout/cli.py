"""Command line interface.

Examples
--------
    python -m filescout -search "*.py TODO" -exit
    python -m filescout -search "size:>10mb" -sort size -sort-descending -exit
    python -m filescout -filter Pic -csv pics.csv
    python -m filescout -rebuild
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

from . import __version__
from .config import Config
from .indexer import Index
from .search import SearchOptions, combine_query, parse_sort_term, run_search
from .util import format_size, format_ts

SORT_FLAGS = [
    ("name", "name"),
    ("path", "path"),
    ("size", "size"),
    ("extension", "extension"),
    ("date-modified", "dm"),
    ("date-created", "dc"),
    ("date-accessed", "da"),
    ("attributes", "attrib"),
]

CSV_HEADER = ["Name", "Path", "Size", "Extension",
              "Date Modified", "Date Created", "Date Accessed", "Attributes"]
EFU_HEADER = ["Filename", "Size", "Extension",
              "Date Modified", "Date Created", "Date Accessed", "Attributes"]


def build_parser():
    p = argparse.ArgumentParser(
        prog="filescout",
        description="FileScout %s - instant filename search" % __version__)
    p.add_argument("-search", metavar="TEXT", help="search text (query syntax)")
    p.add_argument("-filter", metavar="NAME", help="apply a named filter (Audio, Pic, ...)")
    p.add_argument("-sort", metavar="NAME",
                   help="sort: name|path|size|extension|date-modified|"
                        "date-created|date-accessed|attributes")
    p.add_argument("-sort-ascending", dest="sort_ascending", action="store_true", default=None)
    p.add_argument("-sort-descending", dest="sort_descending", action="store_true", default=None)
    for flag, key in SORT_FLAGS:
        p.add_argument("-" + flag, dest="sort_by_" + key, action="store_true",
                       help="sort by %s" % flag.replace("-", " "))
    p.add_argument("-max-results", type=int, default=None, metavar="N")
    p.add_argument("-match-case", dest="match_case", action="store_true", default=None)
    p.add_argument("-match-whole-word", dest="match_whole_word", action="store_true", default=None)
    p.add_argument("-match-path", dest="match_path", action="store_true", default=None)
    p.add_argument("-regex", dest="regex", action="store_true", default=None)
    p.add_argument("-match-diacritics", dest="match_diacritics", action="store_true", default=None)
    p.add_argument("-duplicates", dest="duplicates", action="store_true", default=None)
    p.add_argument("-folders-first", dest="folders_first", action="store_true", default=None)
    p.add_argument("-csv", metavar="FILE", help="export results as CSV")
    p.add_argument("-txt", metavar="FILE", help="export full paths as a text list")
    p.add_argument("-efu", metavar="FILE", help="export as a file list (.efu)")
    p.add_argument("-exit", action="store_true",
                   help="run the search and exit instead of opening the window")
    p.add_argument("-rebuild", action="store_true", help="rebuild the index and exit")
    p.add_argument("-instance", metavar="NAME", help="named instance (accepted, ignored)")
    p.add_argument("-admin", action="store_true", help="accepted, ignored (no service model)")
    p.add_argument("-version", action="store_true", help="print version and exit")
    return p


def load_config():
    return Config()


def resolve_query(args, cfg):
    """Build (query, options) from CLI args and config."""
    query = args.search or ""
    sort = None
    ascending = None
    if args.sort:
        sort, ascending = parse_sort_term(args.sort)
    else:
        for _flag, key in SORT_FLAGS:
            if getattr(args, "sort_by_" + key, False):
                sort = key
                break
    if args.sort_descending:
        ascending = False
    if args.sort_ascending:
        ascending = True
    if args.filter:
        flt = cfg.filter_by_name(args.filter)
        if flt is None:
            raise SystemExit("filescout: unknown filter: %s" % args.filter)
        query = combine_query(flt.get("query", ""), query)
        if sort is None and flt.get("sort"):
            sort = flt["sort"]
    opts = SearchOptions.from_config(
        cfg,
        match_case=args.match_case,
        match_whole_word=args.match_whole_word,
        match_path=args.match_path,
        regex=args.regex,
        match_diacritics=args.match_diacritics,
        duplicates=args.duplicates,
        folders_first=args.folders_first,
        max_results=args.max_results,
        sort=sort,
        sort_ascending=ascending,
    )
    return query, opts


def _display_path(row):
    parent = row["parent"]
    if parent.endswith("\\") and len(parent) > 3:
        return parent[:-1]
    return parent


def _row_values(row):
    return (row["name"], _display_path(row),
            "" if row["is_folder"] else format_size(row["size"]),
            row["extension"], format_ts(row["date_modified"]),
            format_ts(row["date_created"]), format_ts(row["date_accessed"]))


def print_results(index, query, opts, args, stream=None):
    stream = stream or sys.stdout
    res = run_search(index, query, opts)
    rows = res.rows

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            for row in rows:
                writer.writerow([row["name"], _display_path(row),
                                 "" if row["is_folder"] else row["size"],
                                 row["extension"], format_ts(row["date_modified"]),
                                 format_ts(row["date_created"]), format_ts(row["date_accessed"])])
    if args.txt:
        with open(args.txt, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(row["parent"] + row["name"] + "\n")
    if args.efu:
        with open(args.efu, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(EFU_HEADER)
            for row in rows:
                writer.writerow([row["parent"] + row["name"],
                                 "" if row["is_folder"] else row["size"],
                                 row["extension"], format_ts(row["date_modified"]),
                                 format_ts(row["date_created"]), format_ts(row["date_accessed"])])

    wrote_file = bool(args.csv or args.txt or args.efu)
    if not wrote_file:
        _print_table(rows, stream)

    total = res.total_size
    summary = "%d object%s" % (len(rows), "" if len(rows) == 1 else "s")
    if total:
        summary += " (%s)" % format_size(total)
    if res.truncated:
        summary += " - result limit reached, use -max-results"
    print(summary, file=sys.stderr)
    return 0


def _print_table(rows, stream):
    headers = ("Name", "Path", "Size", "Date Modified")
    data = []
    for row in rows:
        data.append((row["name"], _display_path(row),
                     "" if row["is_folder"] else format_size(row["size"]),
                     format_ts(row["date_modified"])))
    widths = []
    for i, head in enumerate(headers):
        width = len(head)
        for line in data:
            width = max(width, len(line[i]))
        widths.append(width)
    out = ["  ".join(head.ljust(widths[i]) for i, head in enumerate(headers))]
    out.extend("  ".join(line[i].ljust(widths[i]) for i in range(4))
               for line in data)
    stream.write("\n".join(out) + "\n")


def rebuild(cfg, args=None, stream=None):
    stream = stream or sys.stderr
    index = Index(cfg.db_path)
    roots = cfg.roots()
    if not roots:
        print("filescout: no index roots configured", file=stream)
        return 1

    def progress(directory, count):
        stream.write("\rIndexing %s (%d objects)   " % (directory, count))
        stream.flush()

    index.rebuild(roots,
                  exclude_folders=cfg.indexes["exclude_folders"],
                  exclude_files=cfg.indexes["exclude_files"],
                  follow_junctions=cfg.indexes["follow_junctions"],
                  progress=progress)
    count, size = index.stats()
    seconds = float(index.get_meta("last_scan_seconds", "0") or 0)
    stream.write("\rIndexed %d objects (%s) in %.1fs%s\n"
                 % (count, format_size(size), seconds, " " * 20))
    if index.errors:
        stream.write("%d folders could not be read, e.g.:\n" % len(index.errors))
        for path, reason in index.errors[:10]:
            stream.write("  %s (%s)\n" % (path, reason))
    stream.flush()
    index.close()
    return 0


def main(argv=None):
    """Returns an exit code, or the string 'gui' to launch the window."""
    args = build_parser().parse_args(argv)
    if args.version:
        print("FileScout %s" % __version__)
        return 0
    cfg = load_config()

    if args.rebuild:
        return rebuild(cfg, args)

    query, opts = resolve_query(args, cfg)
    wants_output = bool(args.csv or args.txt or args.efu)
    if (args.search or args.filter) and (args.exit or wants_output):
        index = Index(cfg.db_path)
        try:
            return print_results(index, query, opts, args)
        finally:
            index.close()
    return "gui"
