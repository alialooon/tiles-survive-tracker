"""Capture a Windows game client without sampling the full desktop.

Many DirectX/Unity clients return an empty buffer from PrintWindow.  When that
happens, bring the already selected game to the foreground and grab ONLY its
client rectangle from the display.  No input events are sent to the game.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

import numpy as np
import windows_capture as wc


def _focus_game(hwnd: int) -> bool:
    """Best-effort Win32 activation; never use synthetic keyboard/mouse input."""
    if wc._game_is_foreground(hwnd):
        return True
    if sys.platform != 'win32':
        return False
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
            return False
        user32.ShowWindowAsync(hwnd, 5)  # SW_SHOW; never minimize the game
        user32.SetForegroundWindow(hwnd)
        # Windows can finish foreground activation a little after the call.
        for _ in range(5):
            if wc._game_is_foreground(hwnd):
                return True
            time.sleep(0.12)
    except (OSError, AttributeError, ValueError):
        return False
    return wc._game_is_foreground(hwnd)


def capture_window(hwnd: int) -> np.ndarray:
    """Return game-client BGR pixels, never an unbounded desktop screenshot."""
    bbox = wc.client_bbox(hwnd)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    native = wc._capture_printwindow(hwnd, width, height)
    if wc._is_usable(native, width, height):
        return native

    if not wc._game_is_foreground(hwnd) and not _focus_game(hwnd):
        raise RuntimeError(
            'Игра не предоставила отдельный кадр, а Windows не разрешила '
            'активировать её окно. Щёлкните по Tiles Survive, откройте карту '
            'и повторите снимок. Рабочий стол не сохраняется.'
        )
    # Recheck the current client bounds after foreground activation.
    active_bbox = wc.client_bbox(hwnd)
    if not wc._game_is_foreground(hwnd):
        raise RuntimeError('Окно игры перестало быть активным. Повторите снимок.')
    picture = wc._capture_visible_region(active_bbox)
    expected = (active_bbox[3] - active_bbox[1],
                active_bbox[2] - active_bbox[0], 3)
    if picture.shape != expected:
        raise RuntimeError('Не совпадают размеры кадра игры. Проверьте масштаб Windows.')
    if not wc._is_usable(picture, expected[1], expected[0]):
        raise RuntimeError('Игра вернула пустой кадр. Используйте оконный режим.')
    return picture
