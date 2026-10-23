"""Regression tests: a NULL Win32 foreground HWND must not crash Tracker."""
import unittest
from unittest.mock import Mock, patch
import launcher


class ForegroundTests(unittest.TestCase):
    def test_null_foreground_is_not_crash(self):
        fake = Mock()
        fake.GetForegroundWindow.return_value = None
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(
            launcher.ctypes, 'WinDLL', return_value=fake, create=True
        ):
            self.assertFalse(launcher._foreground_is_game(123))
            self.assertFalse(launcher._foreground_is_game(None))

    def test_matching_foreground_is_true(self):
        fake = Mock()
        fake.GetForegroundWindow.return_value = 123
        with patch.object(launcher.sys, 'platform', 'win32'), patch.object(
            launcher.ctypes, 'WinDLL', return_value=fake, create=True
        ):
            self.assertTrue(launcher._foreground_is_game(123))
            self.assertFalse(launcher._foreground_is_game(456))


if __name__ == '__main__':
    unittest.main()
