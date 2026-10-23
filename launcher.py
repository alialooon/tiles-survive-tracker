"""Start the GUI with the DirectX/Unity window-capture hotfix."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run() -> int:
    if not (ROOT / 'app.py').is_file():
        print('Missing app.py. Extract the complete Tracker installation.', flush=True)
        return 2
    from capture_hotfix import capture_window
    import app
    app.capture_window = capture_window
    app.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
