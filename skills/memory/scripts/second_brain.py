#!/usr/bin/env python3
"""Resolve the portable shared runtime from this discovered skill."""
from pathlib import Path
import runpy
import sys

script = Path(__file__).resolve().parents[3] / 'scripts/second_brain.py'
sys.path.insert(0, str(script.parent))
runpy.run_path(str(script), run_name='__main__')
