"""Offline regression tests for the v6 read-only nickname scanner."""
import unittest
from unittest.mock import patch
import numpy as np
import launcher as search


class NicknameTests(unittest.TestCase):
    def test_exact_ally_tag(self):
        self.assertEqual(search.match_name('[MTT] KingBonbee', 'KingBonbee', .9), (True, 1.0))

    def test_confusable_letters(self):
        self.assertEqual(search.normalize('KіngBоnbее'), 'kingbonbee')

    def test_single_ocr_typo(self):
        self.assertTrue(search.match_name('KingBonbce', 'KingBonbee', .8)[0])

    def test_unrelated(self):
        self.assertFalse(search.match_name('KingBorland', 'KingBonbee', .92)[0])

    def test_low_confidence(self):
        self.assertFalse(search.match_name('KingBonbce', 'KingBonbee', .25)[0])

    def test_missing_foreground(self):
        with patch.object(search.sys, 'platform', 'linux'):
            self.assertIsNone(search.foreground_window())

    def test_no_desktop_capture(self):
        with patch.object(search, 'foreground_window', return_value=12):
            self.assertIsNone(search.capture_visible_game(13))

    def test_repeat_frame(self):
        frame = np.zeros((400, 600, 3), dtype=np.uint8)
        thumb = search.tiny_frame(frame)
        self.assertFalse(search.sufficiently_new(thumb, thumb))

    def test_mock_ocr(self):
        class MockResult:
            txts = ('[MTT] KingBonbee', 'AnotherPerson')
            scores = (.94, .80)
            boxes = np.array([
                [[10, 4], [142, 4], [142, 22], [10, 22]],
                [[10, 45], [125, 45], [125, 60], [10, 60]],
            ])
        frame = np.zeros((800, 1600, 3), dtype=np.uint8)
        lines = search.ocr_lines(frame, lambda _: MockResult())
        self.assertEqual(int(lines[0][2][0][1]), 58)
        self.assertEqual(len(search.player_hits(lines, 'KingBonbee')), 1)


if __name__ == '__main__':
    unittest.main()
