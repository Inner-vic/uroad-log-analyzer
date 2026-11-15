"""Offline checks: natural input never starts a real task or performs I/O."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import parse_time_input as time_input
import run_uroad_task as task_entry

BEIJING = ZoneInfo("Asia/Shanghai")


class NaturalTimeInputTests(unittest.TestCase):
    received = datetime(2026, 10, 23, 10, 20, 30, tzinfo=BEIJING)

    def assert_range(self, text: str, start: str, end: str):
        parsed = time_input.parse_time_range(text, self.received)
        self.assertEqual((parsed.start, parsed.end), (start, end))
        self.assertEqual(parsed.original, text)
        self.assertEqual(parsed.received_at, self.received.isoformat())
        return parsed

    def test_chinese_colon_full_range(self):
        self.assert_range("2026-10-21 15：48 到 2026-10-21 16：22", "2026-10-21 15:48:00", "2026-10-21 16:22:00")

    def test_iso_aware_full_range_keeps_explicit_beijing_offset(self):
        parsed = self.assert_range("2026-10-21T15:48:00+08:00 到 2026-10-21T16:22:00+08:00", "2026-10-21 15:48:00", "2026-10-21 16:22:00")
        self.assertEqual([change["kind"] for change in parsed.normalization["changes"]], ["timezone", "timezone"])
        self.assertIn("明确时区偏移", parsed.notice)

    def test_iso_utc_full_range_converts_to_beijing(self):
        self.assert_range("2026-10-21T07:48:00Z 到 2026-10-21T08:22:00Z", "2026-10-21 15:48:00", "2026-10-21 16:22:00")

    def test_iso_foreign_offset_full_range_converts_to_beijing(self):
        self.assert_range("2026-10-21 15:48:00+09:00 - 2026-10-21 16:22:00+09:00", "2026-10-21 14:48:00", "2026-10-21 15:22:00")

    def test_iso_compact_offset_full_range(self):
        self.assert_range("2026-10-21T15:48:00+0800 至 2026-10-21T16:22:00+0800", "2026-10-21 15:48:00", "2026-10-21 16:22:00")

    def test_iso_t_without_offset_keeps_existing_beijing_default(self):
        self.assert_range("2026-10-21T15:48:00-2026-10-21T16:22:00", "2026-10-21 15:48:00", "2026-10-21 16:22:00")

    def test_iso_offset_conversion_may_change_calendar_date(self):
        self.assert_range("2026-10-21T23:48:00Z 到 2026-10-22T00:22:00Z", "2026-10-22 07:48:00", "2026-10-22 08:22:00")

    def test_iso_fractional_query_time_is_not_truncated(self):
        with self.assertRaises(ValueError):
            time_input.parse_time_range("2026-10-21T15:48:00.5+08:00 到 2026-10-21T16:22:00+08:00", self.received)

    def test_iso_invalid_offset_is_not_normalized_to_another_offset(self):
        with self.assertRaisesRegex(ValueError, "时区偏移|时刻|日期时间"):
            time_input.parse_time_range("2026-10-21T15:48:00+08:99 到 2026-10-21T16:22:00+08:99", self.received)

    def test_iso_offsets_do_not_hide_extra_clock(self):
        with self.assertRaisesRegex(ValueError, "多个时刻"):
            time_input.parse_time_range("2026-10-21T15:48:00+08:00 到 2026-10-21T16:22:00+08:00，另外17:00", self.received)

    def test_generic_module_cache_cannot_replace_shared_normalizer(self):
        def unexpected(*args, **kwargs):
            self.fail("generic natural_input/semantic_input cache was used")
        polluted = SimpleNamespace(normalize_input=unexpected, apply_interpretation=unexpected)
        with patch.dict(sys.modules, {"natural_input": polluted, "semantic_input": polluted}):
            self.assert_range("10月21日15：30到16：00", "2026-10-21 15:30:00", "2026-10-21 16:00:00")
            self.test_evidence_bound_ai_interpretation_of_natural_prose()

    def test_shared_modules_only_load_from_absolute_sibling(self):
        expected = Path(__file__).resolve().parents[1] / "chassis-business-routing" / "scripts"
        for name in ("natural_input", "semantic_input"):
            with self.subTest(module=name):
                self.assertEqual(Path(time_input._shared_input_module(name).__file__).resolve(), (expected / (name + ".py")).resolve())

    def test_unique_module_cache_with_wrong_path_is_rejected(self):
        with patch.dict(sys.modules, {"_ccconnect_chassis_natural_input": SimpleNamespace(__file__="D:/not-the-sibling.py")}):
            with self.assertRaisesRegex(ValueError, "路径异常"):
                time_input.parse_time_range("10月21日15：30到16：00", self.received)

    def test_full_width_temporal_digits(self):
        self.assert_range("２０２６年１０月２１日１５：４８至１６：２２", "2026-10-21 15:48:00", "2026-10-21 16:22:00")

    def test_colloquial_period_and_inherited_period(self):
        parsed = self.assert_range("10月21日 下午三点半到四点", "2026-10-21 15:30:00", "2026-10-21 16:00:00")
        self.assertIn("年份", parsed.notice)
        self.assertIn("下午", parsed.notice)
        self.assertIn("沿用", parsed.notice)

    def test_day_only_uses_receipt_month_with_notice(self):
        parsed = self.assert_range("21号 15:30至16:00", "2026-10-21 15:30:00", "2026-10-21 16:00:00")
        self.assertIn("2026-10", parsed.notice)

    def test_relative_date_before_after(self):
        self.assert_range("昨天 15：30 前后各 2 分钟", "2026-10-22 15:28:00", "2026-10-22 15:32:00")

    def test_chinese_clock_minute_not_used_as_duration(self):
        self.assert_range("10月21日 15点30分 开始，往后查 5 分钟", "2026-10-21 15:30:00", "2026-10-21 15:35:00")

    def test_chinese_clock_seconds_preserved(self):
        self.assert_range("10月21日 14时35分20秒 前后各 10 秒", "2026-10-21 14:35:10", "2026-10-21 14:35:30")

    def test_ascii_seconds_preserved_in_duration(self):
        self.assert_range("2026-10-21 14:35:20 开始，往后查 5 分钟", "2026-10-21 14:35:20", "2026-10-21 14:40:20")

    def test_ascii_seconds_preserved_in_full_range(self):
        self.assert_range("2026-10-21 14:35:20 到 14:35:40", "2026-10-21 14:35:20", "2026-10-21 14:35:40")

    def test_explicit_date_is_not_reported_as_receipt_default(self):
        parsed = self.assert_range("2026-10-21 14:35:20 前后各 10 秒", "2026-10-21 14:35:10", "2026-10-21 14:35:30")
        self.assertNotIn("未提供日期", parsed.notice)

    def test_legacy_bare_clock_duration_keeps_today_notice(self):
        parsed = self.assert_range("15:30 开始，往后查 5 分钟", "2026-10-23 15:30:00", "2026-10-23 15:35:00")
        self.assertIn("原有时长查询约定", parsed.notice)
        self.assertIn("2026-10-23", parsed.notice)

    def test_unsupported_date_words_never_fall_back_to_today(self):
        for text in ("上周三15:30持续5分钟", "明天15:30持续5分钟", "上个月21号15:30前后各2分钟"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "日期|年月日"):
                time_input.parse_time_range(text, self.received)

    def test_naive_received_time_is_beijing_wall_time(self):
        parsed = time_input.parse_time_range("昨天 15:30 前后各 1 分钟", datetime(2026, 10, 23, 0, 20))
        self.assertEqual(parsed.start, "2026-10-22 15:29:00")
        self.assertEqual(parsed.received_at, "2026-10-23T00:20:00+08:00")

    def test_aware_received_time_converts_once_to_beijing(self):
        parsed = time_input.parse_time_range("昨天 15:30 前后各 1 分钟", datetime(2026, 10, 22, 17, 20, tzinfo=timezone.utc))
        self.assertEqual(parsed.start, "2026-10-22 15:29:00")
        self.assertEqual(parsed.received_at, "2026-10-23T01:20:00+08:00")

    def test_original_whitespace_is_kept(self):
        self.assert_range("  2026-10-21 15:30至16:00\n", "2026-10-21 15:30:00", "2026-10-21 16:00:00")

    def test_fractional_minutes_exactly_convertible_to_seconds(self):
        self.assert_range("2026-10-21 15:30 往后查 0.5 分钟", "2026-10-21 15:30:00", "2026-10-21 15:30:30")

    def test_legacy_hour_duration_is_not_a_second_clock(self):
        self.assert_range("2026-10-21 15:30 往后查 5 时", "2026-10-21 15:30:00", "2026-10-21 20:30:00")

    def test_receipt_microseconds_are_the_same_fixed_anchor(self):
        received = self.received.replace(microsecond=123456)
        parsed = time_input.parse_time_range("昨天15:30前后各2分钟", received)
        self.assertEqual(parsed.received_at, received.isoformat())
        self.assertEqual(parsed.normalization["received_at"], received.isoformat())

    def test_start_duration_may_cross_midnight(self):
        self.assert_range("2026-10-21 23:58 往后查 5 分钟", "2026-10-21 23:58:00", "2026-10-22 00:03:00")

    def test_explicit_range_may_cross_midnight(self):
        self.assert_range("2026-10-21 23:58 到 2026-10-22 00:03", "2026-10-21 23:58:00", "2026-10-22 00:03:00")

    def test_dated_range_rejects_implicit_cross_midnight(self):
        with self.assertRaisesRegex(ValueError, "结束时间必须晚于|跨日"):
            time_input.parse_time_range("2026-10-21 23:58 到 00:03", self.received)

    def test_no_date_ordinary_range_does_not_guess_today(self):
        with self.assertRaisesRegex(ValueError, "日期"):
            time_input.parse_time_range("下午三点到四点", self.received)

    def test_missing_period_is_confirmation_not_am_guess(self):
        with self.assertRaisesRegex(ValueError, "上午或下午"):
            time_input.parse_time_range("10月21日 三点到四点", self.received)

    def test_invalid_date_is_not_auto_repaired(self):
        with self.assertRaisesRegex(ValueError, "日期"):
            time_input.parse_time_range("2026-02-30 15:30至16:00", self.received)

    def test_future_yearless_date_needs_confirmation(self):
        with self.assertRaisesRegex(ValueError, "年份|完整日期"):
            time_input.parse_time_range("12月21日 15:30至16:00", self.received)

    def test_vague_time_needs_confirmation(self):
        with self.assertRaisesRegex(ValueError, "准确|精确"):
            time_input.parse_time_range("昨天15点左右前后各2分钟", self.received)

    def test_unconvertible_fractional_duration_is_not_truncated(self):
        with self.assertRaisesRegex(ValueError, "整秒"):
            time_input.parse_time_range("2026-10-21 15:30 往后查 0.01 分钟", self.received)

    def test_fractional_timestamp_is_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            time_input.parse_time_range("2026-10-21 15:30:00.5至16:00", self.received)

    def test_compound_duration_is_not_silently_shortened(self):
        with self.assertRaisesRegex(ValueError, "复合时长"):
            time_input.parse_time_range("2026-10-21 15:30 往后查 1小时30分钟", self.received)

    def test_conflicting_range_operators_require_clarification(self):
        with self.assertRaisesRegex(ValueError, "前后范围和持续范围"):
            time_input.parse_time_range("2026-10-21 15:30前后各2分钟，往后查5分钟", self.received)

    def test_multiple_duration_ranges_require_clarification(self):
        with self.assertRaisesRegex(ValueError, "多个持续范围"):
            time_input.parse_time_range("2026-10-21 15:30 往后查5分钟，再往后查10分钟", self.received)

    def test_zero_duration_rejected(self):
        with self.assertRaisesRegex(ValueError, "大于 0"):
            time_input.parse_time_range("2026-10-21 15:30往后查0分钟", self.received)

    def test_overflow_duration_returns_clear_input_error(self):
        for text in ("2026-10-21 15:30往后查1000000000000小时", "2026-10-21 15:30前后各1000000000000小时"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "时间范围过大"):
                time_input.parse_time_range(text, self.received)

    def test_unrecognized_expression_requires_clarification(self):
        with self.assertRaises(ValueError):
            time_input.parse_time_range("看看那天下午的日志", self.received)

    def test_normalization_audit_has_fixed_anchor_and_changes(self):
        parsed = time_input.parse_time_range("昨天15：30前后各2分钟", self.received)
        self.assertEqual(parsed.normalization["received_at"], self.received.isoformat())
        self.assertTrue(parsed.normalization["changes"])
        self.assertEqual(parsed.normalization["method"], "normalization")

    def test_evidence_bound_ai_interpretation_of_natural_prose(self):
        text = "10月21日15:30开始，一直跑到16:00结束"
        proposal = {"source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "received_at": self.received.isoformat(), "windows": [{"quote": text, "start": "2026-10-21T15:30:00+08:00", "end": "2026-10-21T16:00:00+08:00"}]}
        parsed = time_input.parse_time_range(text, self.received, interpretation=proposal)
        self.assertEqual((parsed.start, parsed.end), ("2026-10-21 15:30:00", "2026-10-21 16:00:00"))
        self.assertEqual(parsed.original, text)
        self.assertEqual(parsed.normalization["method"], "validated_interpretation")
        self.assertEqual(parsed.normalization["interpretation"], proposal)
        self.assertIn("AI", parsed.notice)

    def test_ai_interpretation_cannot_change_source_time(self):
        text = "10月21日15:30开始，一直跑到16:00结束"
        proposal = {"source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "received_at": self.received.isoformat(), "windows": [{"quote": text, "start": "2026-10-21T15:30:00+08:00", "end": "2026-10-21T16:01:00+08:00"}]}
        with self.assertRaisesRegex(ValueError, "无法核实"):
            time_input.parse_time_range(text, self.received, interpretation=proposal)

    def test_ai_receipt_anchor_may_preserve_microseconds(self):
        received = self.received.replace(microsecond=123456)
        text = "10月21日15:30开始，一直跑到16:00结束"
        proposal = {"source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "received_at": received.isoformat(), "windows": [{"quote": text, "start": "2026-10-21T15:30:00+08:00", "end": "2026-10-21T16:00:00+08:00"}]}
        parsed = time_input.parse_time_range(text, received, interpretation=proposal)
        self.assertEqual(parsed.received_at, received.isoformat())
        self.assertEqual(parsed.normalization["received_at"], received.isoformat())

    def test_ai_interpretation_cannot_invent_missing_date(self):
        text = "15:30开始，一直跑到16:00结束"
        proposal = {"source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "received_at": self.received.isoformat(), "windows": [{"quote": text, "start": "2026-10-23T15:30:00+08:00", "end": "2026-10-23T16:00:00+08:00"}]}
        with self.assertRaisesRegex(ValueError, "无法核实"):
            time_input.parse_time_range(text, self.received, interpretation=proposal)


class UroadNaturalInputEntryTests(unittest.TestCase):
    def test_invalid_natural_time_does_not_requeue_existing_task(self):
        argv = ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00001", "--raw-time-expression", "下午三点到四点", "--received-at", "2026-10-23T10:20:30+08:00", "--retry-existing", "--existing-run-id", "offline-fixture"]
        with patch.object(sys, "argv", argv), patch.dict(task_entry.os.environ, {}, clear=True), patch.object(task_entry, "requeue_retryable") as requeue, patch.object(task_entry.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "日期"):
                task_entry.main()
            requeue.assert_not_called()
            run.assert_not_called()

    def test_cli_keeps_naive_receipt_for_parser_and_metadata_audit(self):
        # All execution is mocked: this test creates no registry/task/report.
        argv = ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00001", "--raw-time-expression", "昨天 15:30 前后各 2 分钟", "--received-at", "2026-10-23T00:20:00"]
        completed = SimpleNamespace(stdout="", stderr="", returncode=0)
        with patch.object(sys, "argv", argv), patch.dict(task_entry.os.environ, {}, clear=True), patch.object(task_entry, "parse_time_range", wraps=time_input.parse_time_range) as parse, patch.object(task_entry.subprocess, "run", return_value=completed) as run, patch.object(task_entry, "should_trigger_worker", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(task_entry.main(), 0)
            self.assertIsNone(parse.call_args.args[1].tzinfo)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--start") + 1], "2026-10-22 15:28:00")
            self.assertEqual(command[command.index("--end") + 1], "2026-10-22 15:32:00")
            metadata = json.loads(run.call_args.kwargs["env"][task_entry.REQUEST_METADATA_ENV])
            self.assertEqual(metadata["rawTimeExpression"], argv[4])
            self.assertEqual(metadata["inputNormalization"]["received_at"], "2026-10-23T00:20:00+08:00")

    def test_cli_rejects_explicit_dates_with_interpretation(self):
        argv = ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00001", "--start", "2026-10-21 15:30:00", "--end", "2026-10-21 16:00:00", "--interpretation", "{}"]
        with patch.object(sys, "argv", argv), patch.object(task_entry.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "不能与 start/end"):
                task_entry.main()
            run.assert_not_called()

    def test_cli_rejects_nonobject_interpretation_before_execution(self):
        argv = ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00001", "--interpretation", "[]"]
        with patch.object(sys, "argv", argv), patch.object(task_entry.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "JSON 对象"):
                task_entry.main()
            run.assert_not_called()

    def test_cli_ai_interpretation_is_validated_before_mocked_pipeline(self):
        text = "10月21日15:30开始，一直跑到16:00结束"
        received = "2026-10-23T10:20:30+08:00"
        proposal = {"source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "received_at": received, "windows": [{"quote": text, "start": "2026-10-21T15:30:00+08:00", "end": "2026-10-21T16:00:00+08:00"}]}
        argv = ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00001", "--raw-time-expression", text, "--received-at", received, "--interpretation", json.dumps(proposal, ensure_ascii=False)]
        with patch.object(sys, "argv", argv), patch.dict(task_entry.os.environ, {}, clear=True), patch.object(task_entry.subprocess, "run", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)) as run, patch.object(task_entry, "should_trigger_worker", return_value=False):
            self.assertEqual(task_entry.main(), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--start") + 1], "2026-10-21 15:30:00")
            metadata = json.loads(run.call_args.kwargs["env"][task_entry.REQUEST_METADATA_ENV])
            self.assertEqual(metadata["rawTimeExpression"], text)
            self.assertEqual(metadata["inputNormalization"]["interpretation"], proposal)

    def test_cli_invalid_ai_candidate_never_starts_pipeline(self):
        text = "15:30开始，一直跑到16:00结束"
        received = "2026-10-23T10:20:30+08:00"
        proposal = {"source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "received_at": received, "windows": [{"quote": text, "start": "2026-10-23T15:30:00+08:00", "end": "2026-10-23T16:00:00+08:00"}]}
        argv = ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00001", "--raw-time-expression", text, "--received-at", received, "--interpretation", json.dumps(proposal, ensure_ascii=False)]
        with patch.object(sys, "argv", argv), patch.dict(task_entry.os.environ, {}, clear=True), patch.object(task_entry.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "无法核实"):
                task_entry.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
