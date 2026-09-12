import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
import sys
sys.path.insert(0, '/workspace/skills/uroad-cloud-report/scripts')
from parse_time_input import parse_time_range

BJT = ZoneInfo('Asia/Shanghai')

class ParseTimeInputTests(unittest.TestCase):
    received = datetime(2026, 9, 4, 16, 37, 0, tzinfo=BJT)

    def test_start_plus_duration(self):
        r = parse_time_range('15:30 开始，往后查 5 分钟', self.received)
        self.assertEqual((r.start, r.end), ('2026-09-04 15:30:00', '2026-09-04 15:35:00'))

    def test_before_after(self):
        r = parse_time_range('15:30 前后各 2 分钟', self.received)
        self.assertEqual((r.start, r.end), ('2026-09-04 15:28:00', '2026-09-04 15:32:00'))
        self.assertIn('前后各', r.notice)

    def test_cross_midnight(self):
        r = parse_time_range('23:58 开始，往后查 5 分钟', self.received)
        self.assertEqual((r.start, r.end), ('2026-09-04 23:58:00', '2026-09-05 00:03:00'))

    def test_yesterday_uses_received_date(self):
        r = parse_time_range('昨天 00:10 前后各 2 分钟', self.received)
        self.assertEqual((r.start, r.end), ('2026-09-03 00:08:00', '2026-09-03 00:12:00'))

    def test_single_clock_requires_duration(self):
        with self.assertRaises(ValueError):
            parse_time_range('15:30', self.received)

if __name__ == '__main__':
    unittest.main()
