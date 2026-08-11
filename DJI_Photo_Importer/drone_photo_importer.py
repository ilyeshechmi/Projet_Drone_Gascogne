#!/usr/bin/env python3
"""Lance DJI Photo Importer en mode graphique ou terminal."""

import sys

from drone_importer.main import run


if __name__ == "__main__":
    # Les arguments sont transmis tels quels à main.py (--scan, --import-all...).
    raise SystemExit(run(sys.argv[1:]))
