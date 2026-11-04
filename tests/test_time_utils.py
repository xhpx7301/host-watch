import unittest
from datetime import datetime

from time_utils import BEIJING_TZ, beijing_iso, format_beijing, now_beijing


class BeijingTimeTests(unittest.TestCase):
    def test_timestamps_are_written_with_beijing_offset(self):
        self.assertTrue(beijing_iso().endswith("+08:00"))
        self.assertEqual(now_beijing().utcoffset().total_seconds(), 8 * 60 * 60)
        self.assertEqual(beijing_iso(datetime(2026, 10, 16, 12, 0, tzinfo=BEIJING_TZ)), "2026-10-16T12:00:00+08:00")

    def test_utc_and_naive_values_format_as_beijing_time(self):
        self.assertEqual(format_beijing("2026-10-16T12:32:57+00:00"), "2026-10-16 20:32:57 北京时间")
        self.assertEqual(format_beijing("2026-10-16T20:32:57"), "2026-10-16 20:32:57 北京时间")
        self.assertEqual(format_beijing("unknown"), "unknown")


if __name__ == "__main__":
    unittest.main()
