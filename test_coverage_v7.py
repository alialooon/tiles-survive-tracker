"""Regression tests for exact-name search and coordinate extraction.

Run: python -m unittest test_coverage_v7.py
"""
import unittest
from unittest.mock import patch
import numpy as np
import launcher as v


class NicknameAndCoverageTests(unittest.TestCase):
    def test_similar_names_not_alerts(self):
        for item in ('mns bonbee', '[MTT] Bonbee', 'KingBonbe', 'KingBombee',
                     'KingBonbee42', '[MTT] KingBonbee2', 'King Bonbee'):
            with self.subTest(item=item):
                self.assertFalse(v.match_name(item, 'KingBonbee', .99)[0])

    def test_complete_target_name_matches(self):
        for item in ('KingBonbee', '[MTT] KingBonbee', '[MTT]KingBonbee'):
            with self.subTest(item=item):
                self.assertEqual(v.match_name(item, 'KingBonbee', .95), (True, 1.0))

    def test_confidence_requirement(self):
        self.assertFalse(v.match_name('KingBonbee', 'KingBonbee', .4)[0])

    def test_read_center_only_if_present(self):
        for item in ('#752 X:483 Y:545', 'X483 Y545', 'X: 483  Y: 545'):
            self.assertEqual(v.parse_map_coordinates(item), (483, 545))
        for item in ('Y:545', 'KingBonbee', 'X:99999 Y:5', ''):
            self.assertIsNone(v.parse_map_coordinates(item))

    def test_header_ocr_is_separate_from_map_ocr(self):
        class Result:
            txts = ['#752 X:483 Y:545']
        frame = np.zeros((680, 1195, 3), dtype=np.uint8)
        self.assertEqual(v.extract_map_coordinates(frame, lambda _: Result(), '752'),
                         (483, 545))

    def test_no_desktop_capture_if_game_not_foreground(self):
        with patch.object(v, 'foreground_window', return_value=None):
            self.assertIsNone(v.capture_visible_game(1255))


if __name__ == '__main__':
    unittest.main()
