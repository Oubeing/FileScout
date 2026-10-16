#!/usr/bin/env python3
"""FileScout - launcher script.

Usage:
    python FileScout.py                 open the main window
    python FileScout.py -search "*.py"  search from the command line
    python FileScout.py --help          full option list

Equivalent to:  python -m filescout
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from filescout.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
