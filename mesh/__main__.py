"""
mesh/__main__.py — Package execution entry point for `python -m mesh`.
"""
from __future__ import annotations

import sys

if len(sys.argv) == 2 and sys.argv[1] in ("-v", "--version", "version"):
    from .version import VERSION

    print(f"mesh {VERSION}")
    sys.exit(0)

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
