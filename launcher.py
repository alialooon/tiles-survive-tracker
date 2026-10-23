"""Stable Windows launcher. The PowerShell bootstrap synchronizes verified GitHub updates first."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run() -> int:
    app = ROOT / "app.py"
    if not app.is_file():
        print("ERROR: app.py is missing. Extract the complete Tracker package.", flush=True)
        return 2
    return subprocess.call([sys.executable, "-u", str(app)], cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(run())
