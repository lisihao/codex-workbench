#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_workbench.plugin_cache_upgrade import main


if __name__ == "__main__":
    raise SystemExit(main())
