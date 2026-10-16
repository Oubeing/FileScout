"""Entry point:  python -m filescout [options]

Behaviour:
  * no arguments            -> open the main window
  * -search/-filter given   -> open the window with that search
  * ... plus -exit          -> print results and exit
  * -rebuild                -> rebuild the index and exit
  * -version                -> print the version

The desktop GUI is built with PyQt5 (pip install PyQt5).
"""
from __future__ import annotations

import sys

from . import __version__
from . import cli


def main(argv=None):
    args = cli.build_parser().parse_args(argv)
    if args.version:
        print("FileScout %s" % __version__)
        return 0

    cfg = cli.load_config()

    if args.rebuild:
        return cli.rebuild(cfg, args)

    query, opts = cli.resolve_query(args, cfg)
    wants_output = bool(args.csv or args.txt or args.efu)
    if (args.search or args.filter) and (args.exit or wants_output):
        index = cli.Index(cfg.db_path)
        try:
            return cli.print_results(index, query, opts, args)
        finally:
            index.close()

    # GUI mode
    try:
        from .app import run_gui
    except ImportError as exc:
        print("filescout: the desktop GUI needs PyQt5 (%s)" % exc)
        print("filescout: install it with:  pip install PyQt5")
        return 1
    return run_gui(cfg, initial_query=query, initial_opts=opts)


if __name__ == "__main__":
    sys.exit(main())
