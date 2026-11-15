"""Offline temporal normalization contracts; no upstream services involved."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta

from natural_input import normalize_input


class NaturalInputTests(unittest.TestCase):
    anchor = datetime(2026, 10, 23, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    def parse(self, text: str) -> dict:
        return normalize_input(text, self.anchor)

    def test_chinese_date_and_fullwidth_colons(self):
        result = self.parse("走查测试 DEM0VEH1CLEX00001，2026年10月21日15：48：30至16：22")
        self.assertEqual(result["text"], "走查测试 DEM0VEH1CLEX00001，2026-10-21 15:48:30至16:22:00")
        self.assertFalse(result["needs_confirmation"])

    def test_fullwidth_digits_and_point(self):
        result = self.parse("日志 ２０２６年１０月２１日 １５．４８至１６：２２")
        self.assertEqual(result["text"], "日志 2026-10-21 15:48:00至16:22:00")
        self.assertFalse(result["needs_confirmation"])
        for value in ("２０２６．１０．２１ １５：４８", "２０２６／１０／２１ １５：４８", "２０２６－１０－２１ １５：４８"):
            with self.subTest(value=value):
                self.assertEqual(self.parse(value)["text"], "2026-10-21 15:48:00")

    def test_han_date_clock_and_range_period(self):
        result = self.parse("昨天，下午三点半到四点二十二分")
        self.assertEqual(result["text"], "2026-10-22，15:30:00到16:22:00")
        self.assertFalse(result["needs_confirmation"])
        self.assertIn("沿用", result["changes"][-1]["basis"])

    def test_spoken_range_period_inheritance(self):
        result = self.parse("昨天从下午三点四十八开始一直到四点二十二结束")
        self.assertEqual(result["text"], "2026-10-22从15:48:00开始一直到16:22:00结束")
        self.assertFalse(result["needs_confirmation"])

    def test_period_does_not_cross_sentence_or_windows(self):
        for value in ("下午三点；四点", "下午三点\n四点", "下午三点。四点", "下午三点，另一个窗口四点", "下午三点昨天四点"):
            with self.subTest(value=value):
                self.assertTrue(self.parse(value)["needs_confirmation"])

    def test_clear_duration_is_not_clock(self):
        for value in ("昨天15:30往后查5时", "昨天15:30持续5时", "昨天15:30前后各五时"):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertFalse(result["needs_confirmation"])
                self.assertTrue(result["text"].endswith(value[value.rfind("30") + 2:]))

    def test_no_space_fullwidth_clock_range(self):
        self.assertEqual(self.parse("昨天15：48-16：22")["text"], "2026-10-22 15:48:00-16:22:00")

    def test_ascii_same_day_ranges_are_not_extreme_timezone_offsets(self):
        for source, target in (("2026-10-21 15:00-15:35", "2026-10-21 15:00:00-15:35:00"),
                               ("2026-10-21 17:31-18:06", "2026-10-21 17:31:00-18:06:00")):
            with self.subTest(source=source):
                result = self.parse(source)
                self.assertEqual(result["text"], target)
                self.assertFalse(result["needs_confirmation"])
                self.assertFalse(any(c["kind"] == "timezone" for c in result["changes"]))
        result = self.parse("2026-10-21 15:00-05:00")
        self.assertEqual(result["text"], "2026-10-22 04:00:00")
        self.assertEqual(result["changes"][0]["kind"], "timezone")

    def test_han_complete_date(self):
        result = self.parse("二零二六年十月二十一日上午十一点三刻")
        self.assertEqual(result["text"], "2026-10-21 11:45:00")
        self.assertFalse(result["needs_confirmation"])

    def test_relative_anchor_is_beijing(self):
        utc = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)
        result = normalize_input("今天15：48，前天15点整", utc)
        self.assertEqual(result["text"], "2026-10-23 15:48:00，2026-10-21 15:00:00")
        self.assertEqual(result["received_at"], "2026-10-23T04:00:00+08:00")

    def test_month_day_missing_year(self):
        result = self.parse("10月21日下午三点一刻")
        self.assertEqual(result["text"], "2026-10-21 15:15:00")
        self.assertTrue(any("接收" in x["basis"] for x in result["changes"]))

    def test_numeric_month_day(self):
        result = self.parse("10/21 15：48 到 16：22")
        self.assertEqual(result["text"], "2026-10-21 15:48:00 到 16:22:00")

    def test_day_missing_month_year(self):
        result = self.parse("21号下午三点半")
        self.assertEqual(result["text"], "2026-10-21 15:30:00")
        self.assertIn("2026-10", result["changes"][0]["basis"])

    def test_same_sentence_month_inheritance(self):
        result = self.parse("2026年9月20日15：48到21号16：22")
        self.assertEqual(result["text"], "2026-09-20 15:48:00到2026-09-21 16:22:00")
        self.assertFalse(result["needs_confirmation"])

    def test_standalone_year_month_inheritance(self):
        result = self.parse("2026年9月，21号15：48")
        self.assertIn("2026-09-21 15:48:00", result["text"])
        self.assertFalse(result["needs_confirmation"])

    def test_future_yearless_date_requires_confirmation(self):
        for value in ("12月31日15：48", "10月25日15：48", "25号15：48", "昨天15：48到25号16：22", "昨天15：48到12月31日16：22"):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertTrue(result["needs_confirmation"])
                self.assertIn("晚于接收日期", result["message"])
                self.assertNotIn("2025", result["text"])

    def test_year_rollover_is_not_guessed(self):
        result = normalize_input("12月31日15：48到1月1日16：22", datetime(2026, 1, 2, tzinfo=self.anchor.tzinfo))
        self.assertTrue(result["needs_confirmation"])
        self.assertIn("12月31日", result["text"])
        result = self.parse("2025年12月31日15：48到1月1日16：22")
        self.assertTrue(result["needs_confirmation"])
        self.assertIn("跨年日期确认", result["missing"])

    def test_ambiguous_chinese_hour(self):
        result = self.parse("昨天三点半到四点")
        self.assertTrue(result["needs_confirmation"])
        self.assertIn("上午或下午", result["missing"])

    def test_unrecognized_date_not_treated_as_omitted(self):
        for value in ("上周三15:30持续5分钟", "明天15:30持续5分钟", "去年10月21日15:30", "上个月21号下午三点", "大前天15:30"):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertTrue(result["needs_confirmation"])
                self.assertEqual(result["text"], value)
                self.assertIn("不会按当天", result["message"])

    def test_malformed_dates_not_repaired(self):
        for value in ("2026年2月30日15：48", "2026-13-01 15：48", "2026/02/29 15：48", "32号15：48"):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertTrue(result["needs_confirmation"])
                self.assertIn("有效日期", result["missing"])

    def test_invalid_clock(self):
        for value in ("昨天25：48", "昨天15：78", "昨天上午十五点", "昨天晚上十二点"):
            with self.subTest(value=value):
                self.assertTrue(self.parse(value)["needs_confirmation"])

    def test_night_and_inconsistent_periods_are_not_afternoon(self):
        for period in ("夜里", "夜间", "晚上", "晚间", "傍晚"):
            with self.subTest(period=period):
                result = self.parse(f"2026年10月22日{period}一点到两点")
                self.assertTrue(result["needs_confirmation"])
                self.assertNotIn("13:00", result["text"])
                self.assertNotIn("14:00", result["text"])
        for value in ("昨天早上十五点", "昨天早晨零点", "昨天下午九点", "昨天晚上十五点", "昨天傍晚十点"):
            with self.subTest(value=value):
                self.assertTrue(self.parse(value)["needs_confirmation"])
        for value, clock in (("昨天晚上九点", "21:00:00"), ("昨天傍晚五点", "17:00:00"), ("昨天凌晨一点", "01:00:00")):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertFalse(result["needs_confirmation"])
                self.assertIn(clock, result["text"])

    def test_explicit_non_beijing_zones_are_not_ignored(self):
        for zone in ("UTC", "UTC+0", "美东时间", "美国东部时间", "GMT", "EDT"):
            with self.subTest(zone=zone):
                result = self.parse("2026年10月22日15:30到16:00 " + zone)
                self.assertTrue(result["needs_confirmation"])
                self.assertIn("查询时区确认", result["missing"])
        value = "2026-10-22T15:30:00-05:00"
        result = self.parse(value)
        self.assertEqual(result["text"], "2026-10-23 04:30:00")
        self.assertFalse(result["needs_confirmation"])
        self.assertEqual(result["changes"][0]["kind"], "timezone")

    def test_abbreviated_relative_days_not_ignored(self):
        for relative in ("昨晚", "昨夜", "昨早", "昨晨", "前晚", "前夜"):
            with self.subTest(relative=relative):
                result = self.parse(relative + "23:00前后各1分钟")
                self.assertTrue(result["needs_confirmation"])
                self.assertEqual(result["text"], relative + "23:00前后各1分钟")

    def test_alternative_clock_guard(self):
        for value in ("2026年10月22日15:30到16:00或17:00", "昨天15:30或者16:00", "昨天15:30或是16:00", "昨天15:30到16:00二选一", "昨天下午三点还是四点"):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertTrue(result["needs_confirmation"])
                self.assertIn("唯一时间范围", result["missing"])
        result = self.parse("日志2026年10月22日15:30到16:00，看看时延或CPU")
        self.assertFalse(result["needs_confirmation"])
        result = self.parse("时延或CPU")
        self.assertFalse(result["needs_confirmation"])

    def test_vague_time_not_exactly_accepted(self):
        for value in ("昨天大概下午三点半", "昨天15：48左右", "大约今天15：48"):
            with self.subTest(value=value):
                self.assertTrue(self.parse(value)["needs_confirmation"])
                self.assertIn("精确时间", self.parse(value)["missing"])

    def test_non_temporal_prose(self):
        for value in ("帮忙完善自然语言解析", "解析左右轮日志", "制品V1.23 CPU 15.48%", ""):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertEqual(result["text"], value)
                self.assertFalse(result["needs_confirmation"])

    def test_url_vin_labels_and_artifact_preserved(self):
        value = "Base PCM更新 DEM0VEH1CLEX00001 https://example.test/2026-10-21?x=15：48&y=10月21日 包_build_2026-10-21.tar.gz"
        result = self.parse(value)
        self.assertEqual(result["text"], value)
        self.assertFalse(result["needs_confirmation"])
        value = "https://example.test/2026-10-2115:48:00 包_build_2026-10-2115:48:00"
        self.assertEqual(self.parse(value)["text"], value)

    def test_url_ends_at_chinese_sentence_punctuation(self):
        value = ("PCM更新：昨天15：48到16：22 https://example.feishu.cn/sheets/First?clock=15：48；"
                 "Base：前天14：50到15：24 https://example.feishu.cn/sheets/Second?date=10/21")
        result = self.parse(value)
        self.assertEqual(result["text"],
                         "PCM更新：2026-10-22 15:48:00到16:22:00 https://example.feishu.cn/sheets/First?clock=15：48；"
                         "Base：2026-10-21 14:50:00到15:24:00 https://example.feishu.cn/sheets/Second?date=10/21")
        self.assertFalse(result["needs_confirmation"])
        for punctuation in "，；。！？":
            with self.subTest(punctuation=punctuation):
                source = "https://example.test/file?clock=15：48" + punctuation + "昨天15：48"
                target = "https://example.test/file?clock=15：48" + punctuation + "2026-10-22 15:48:00"
                self.assertEqual(self.parse(source)["text"], target)

    def test_iso_timezone_datetime_equivalent_and_audited(self):
        value = "2026-10-21T15:48:30+08:00 到 2026-10-21T16:22:00+08:00"
        result = self.parse(value)
        self.assertEqual(result["text"], "2026-10-21 15:48:30 到 2026-10-21 16:22:00")
        self.assertEqual(len(result["changes"]), 2)
        self.assertTrue(all(change["kind"] == "timezone" for change in result["changes"]))
        self.assertIn("+08:00", result["changes"][0]["original"])
        self.assertIn("北京时间", result["changes"][0]["basis"])
        self.assertFalse(result["needs_confirmation"])

    def test_iso_offsets_share_beijing_wall_time(self):
        for source, target in (("2026-10-21T15:00:00Z", "2026-10-21 23:00:00"),
                               ("2026-10-21T21:00:00-05:00", "2026-10-22 10:00:00"),
                               ("2026-10-21T00:15+09:00", "2026-10-20 23:15:00")):
            with self.subTest(source=source):
                result = self.parse(source)
                self.assertEqual(result["text"], target)
                self.assertFalse(result["needs_confirmation"])
                self.assertEqual(result["changes"][0]["original"], source)

    def test_naive_compact_range_year_is_not_an_offset(self):
        value = "2026-10-21T15:48:00-2026-10-21T16:22:00"
        result = self.parse(value)
        self.assertEqual(result["text"], value)
        self.assertFalse(result["needs_confirmation"])
        self.assertFalse(any(change["kind"] == "timezone" for change in result["changes"]))

    def test_invalid_iso_offset_not_normalized_by_datetime(self):
        for offset in ("+08:99", "+08:60", "+0860", "+24:00", "-24:00"):
            with self.subTest(offset=offset):
                value = "2026-10-21T15:48:00" + offset
                result = self.parse(value)
                self.assertEqual(result["text"], value)
                self.assertTrue(result["needs_confirmation"])
                self.assertIn("有效时区偏移", result["missing"])

    def test_compact_iso_offsets_in_compact_range(self):
        result = self.parse("2026-10-21T15:48:00+0800-2026-10-21T16:22:00+0800")
        self.assertEqual(result["text"], "2026-10-21 15:48:00-2026-10-21 16:22:00")
        self.assertFalse(result["needs_confirmation"])
        self.assertEqual([change["kind"] for change in result["changes"]], ["timezone", "timezone"])

    def test_iso_fractional_seconds_require_confirmation(self):
        for value in ("2026-10-21T15:48:30.123+08:00", "2026-10-21T15:48:30.000Z"):
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertEqual(result["text"], value)
                self.assertTrue(result["needs_confirmation"])
                self.assertIn("整秒时间确认", result["missing"])

    def test_iso_beijing_date_supplies_day_only_month(self):
        result = self.parse("2026-09-20T15:48:00+08:00 到21号16：22")
        self.assertEqual(result["text"], "2026-09-20 15:48:00 到2026-09-21 16:22:00")
        self.assertFalse(result["needs_confirmation"])

    def test_bare_ascii_clock_left_to_business_date_policy(self):
        result = self.parse("日志15:48持续10分钟")
        self.assertEqual(result["text"], "日志15:48:00持续10分钟")
        self.assertFalse(result["needs_confirmation"])

    def test_no_whole_text_nfkc(self):
        value = "制品ＡＢＣ／１２３，备注①；昨天１５：４８"
        result = self.parse(value)
        self.assertEqual(result["text"], "制品ＡＢＣ／１２３，备注①；2026-10-22 15:48:00")


if __name__ == "__main__":
    unittest.main()
