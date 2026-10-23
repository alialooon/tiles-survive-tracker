"""Launch Tracker with a bounded Windows-only game capture fallback.

The legacy update bootstrap accepts updates to launcher.py. Keeping the capture
compatibility fix in this file avoids updates that older bootstraps reject.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _foreground_is_game(hwnd: int | None) -> bool:
    """Win32 GetForegroundWindow can briefly return NULL (ctypes None).

    Returning False is safe: we will try to activate the game again instead
    of crashing when Windows is transitioning focus between two windows.
    """
    if hwnd is None or sys.platform != 'win32':
        return False
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        active = user32.GetForegroundWindow()
        return active is not None and int(active) == int(hwnd)
    except (OSError, AttributeError, ValueError, TypeError):
        return False


def _activate_game(hwnd: int) -> bool:
    """Best-effort foreground activation; never synthesize gameplay input."""
    if sys.platform != 'win32':
        return False
    import windows_capture as wc
    if wc._game_is_foreground(hwnd):
        return True
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
            return False
        user32.SetForegroundWindow(hwnd)
        for _ in range(5):
            if wc._game_is_foreground(hwnd):
                return True
            time.sleep(0.12)
    except (OSError, AttributeError, ValueError):
        return False
    return wc._game_is_foreground(hwnd)


def capture_game(hwnd: int):
    """Capture the game's client rectangle, not the desktop.

    Unity/DirectX may not support PrintWindow; only then use a bounded crop of
    the visible game window. Do not capture if another app is foreground.
    """
    import windows_capture as wc
    bbox = wc.client_bbox(hwnd)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    frame = wc._capture_printwindow(hwnd, width, height)
    if wc._is_usable(frame, width, height):
        return frame

    if not wc._game_is_foreground(hwnd) and not _activate_game(hwnd):
        raise RuntimeError(
            'Игровой движок не передал отдельный кадр и не удалось активировать Tiles Survive. '
            'Переключитесь на игру вручную и повторите снимок.'
        )
    if not wc._game_is_foreground(hwnd):
        raise RuntimeError('Игра перестала быть активным окном. Повторите снимок.')
    bbox = wc.client_bbox(hwnd)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    frame = wc._capture_visible_region(bbox)
    if not wc._is_usable(frame, width, height):
        raise RuntimeError('Не удалось получить изображение окна игры. Проверьте оконный режим.')
    return frame


def run() -> int:
    if not (ROOT / 'app.py').is_file():
        print('ERROR: app.py is missing. Extract the full Tracker archive.', flush=True)
        return 2
    import windows_capture as wc
    # Replace the old unchecked int(GetForegroundWindow()) call before any
    # capture: Win32 returns NULL during focus changes and ctypes maps it to None.
    wc._game_is_foreground = _foreground_is_game
    import app
    wc.capture_window = capture_game
    app.capture_window = capture_game
    app.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
