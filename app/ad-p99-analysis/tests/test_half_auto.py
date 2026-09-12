import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import half_auto


def result_for(value, source):
    return {
        "source": {"url": source, "revision": "r1"},
        "metrics": {
            "slow_driver": {"status": "ok", "p99_ms": value},
            "fast_driver": {"status": "ok", "p99_ms": value + 1},
            "CNOA": {"status": "ok", "p99_ms": value + 2},
            "parking": {"status": "ok", "p99_ms": value + 3},
        },
    }


class HalfAutoWorkflowTests(unittest.TestCase):
    REQUEST = "VIN：DEM0VEH1CLE000001；20260402120000/20260402130000；20260401120000/20260401130000"
    BASELINE_URL = "https://li.feishu.cn/sheets/BaselineToken?sheet=VLA"
    REPLACEMENT_URL = "https://li.feishu.cn/sheets/ReplacementToken?sheet=VLA"

    def test_creates_commands_and_runs_only_after_two_role_bound_results(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            created = half_auto.create_job(self.REQUEST, state_dir=directory, asgard_platform="perf-m100-ultra")
            key = created["comparison_key"]
            self.assertEqual(created["status"], "waiting_cards")
            self.assertIn("fsdlog DEM0VEH1CLE000001", created["baseline"]["asgard_command"])
            self.assertIn("2026-04-01 12:00:00", created["baseline"]["asgard_command"])
            self.assertEqual(created["asgard_submission_order"][0]["role"], "baseline")
            self.assertIn("一条独立聊天消息", created["asgard_submission_order"][0]["send_as"])

            card = {"elements": [{"tag": "button", "text": "查看结果", "url": self.BASELINE_URL}]}
            waiting = half_auto.attach_result(directory, key, role="auto", label="Baseline 结果卡片", card_document=card)
            self.assertEqual(waiting["status"], "waiting_cards")
            self.assertTrue(waiting["baseline"]["received"])
            ready = half_auto.attach_result(directory, key, role="auto", label="替换后", sheet_url=self.REPLACEMENT_URL)
            self.assertEqual(ready["status"], "ready_to_analyze")

            def fake_reader(url, **_):
                return result_for(10 if url == self.BASELINE_URL else 8, url)

            comparison = half_auto.run_job(directory, key, identity="user", read_fn=fake_reader)
            self.assertEqual(comparison["status"], "ok")
            self.assertEqual(comparison["comparisons"]["slow_driver"]["delta_ms"], -2.0)
            _, saved = half_auto.load_job(directory, key)
            self.assertEqual(saved["status"], "completed")

    def test_rejects_missing_label_duplicate_role_and_non_sheet_card_url(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            created = half_auto.create_job(self.REQUEST, state_dir=directory, asgard_platform="perf-m100-ultra")
            key = created["comparison_key"]
            with self.assertRaisesRegex(Exception, "标记"):
                half_auto.attach_result(directory, key, role="auto", label="结果", sheet_url=self.BASELINE_URL)
            with self.assertRaisesRegex(Exception, "不是可读取"):
                half_auto.attach_result(directory, key, role="baseline", sheet_url="https://li.feishu.cn/docx/abc")
            half_auto.attach_result(directory, key, role="baseline", sheet_url=self.BASELINE_URL)
            with self.assertRaisesRegex(Exception, "拒绝用后到卡片覆盖"):
                half_auto.attach_result(directory, key, role="baseline", sheet_url=self.REPLACEMENT_URL)

    def test_parses_one_reply_event_without_chat_history(self):
        context = half_auto.parse_attachment_context("Baseline #23c6eec08965b620，请处理这张卡片")
        self.assertEqual(context, {"comparison_key": "23c6eec08965b620", "role": "baseline"})
        with self.assertRaisesRegex(Exception, "只含一个"):
            half_auto.parse_attachment_context("Baseline #23c6eec08965b620 #aaaaaaaaaaaaaaaa")

    def test_cards_can_arrive_in_any_order_and_match_their_visible_time(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            created = half_auto.create_job(self.REQUEST, state_dir=directory, asgard_platform="perf-m100-ultra")
            key = created["comparison_key"]
            replacement_card = {
                "elements": [
                    {"tag": "markdown", "content": "2026-04-02 12:00:00 - 2026-04-02 13:00:00"},
                    {"tag": "button", "text": "查看结果", "url": self.REPLACEMENT_URL},
                ]
            }
            baseline_card = {
                "elements": [
                    {"tag": "markdown", "content": "2026-04-01 12:00:00 - 2026-04-01 13:00:00"},
                    {"tag": "button", "text": "查看结果", "url": self.BASELINE_URL},
                ]
            }
            self.assertEqual(half_auto.discover_card_target(directory, replacement_card),
                             {"comparison_key": key, "role": "replacement"})
            replacement = half_auto.attach_card_automatically(directory, replacement_card)
            self.assertTrue(replacement["replacement"]["received"])
            baseline = half_auto.attach_card_automatically(directory, baseline_card)
            self.assertEqual(baseline["status"], "ready_to_analyze")

    def test_ingest_current_card_event_runs_after_the_second_card(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            half_auto.create_job(self.REQUEST, state_dir=directory, asgard_platform="perf-m100-ultra")
            baseline_card = {
                "elements": [
                    {"tag": "markdown", "content": "2026-04-01 12:00:00 - 2026-04-01 13:00:00"},
                    {"tag": "button", "text": "查看结果", "url": self.BASELINE_URL},
                ]
            }
            replacement_card = {
                "elements": [
                    {"tag": "markdown", "content": "2026-04-02 12:00:00 - 2026-04-02 13:00:00"},
                    {"tag": "button", "text": "查看结果", "url": self.REPLACEMENT_URL},
                ]
            }

            def fake_reader(url, **_):
                return result_for(10 if url == self.BASELINE_URL else 8, url)

            first = half_auto.ingest_card_event(directory, baseline_card, identity="user", read_fn=fake_reader)
            self.assertEqual(first["status"], "waiting_cards")
            second = half_auto.ingest_card_event(directory, replacement_card, identity="user", read_fn=fake_reader)
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["comparison"]["comparisons"]["slow_driver"]["delta_ms"], -2.0)

    def test_two_labelled_manual_urls_trigger_comparison_in_any_line_order(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            message = "\n".join([
                self.REQUEST,
                f"替换后结果表：{self.REPLACEMENT_URL}",
                f"Baseline 结果表：{self.BASELINE_URL}",
            ])

            def fake_reader(url, **_):
                return result_for(10 if url == self.BASELINE_URL else 8, url)

            result = half_auto.ingest_manual_url_message(directory, message, identity="user", read_fn=fake_reader)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["comparison"]["comparisons"]["slow_driver"]["delta_ms"], -2.0)

    def test_one_manual_url_prompts_for_a_keyed_second_url_without_chat_history(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            first_message = "\n".join([self.REQUEST, f"Baseline 结果表：{self.BASELINE_URL}"])
            first = half_auto.ingest_manual_url_message(directory, first_message, identity="user")
            self.assertEqual(first["status"], "waiting_urls")
            key = first["comparison_key"]
            second_message = f"替换后 #{key} {self.REPLACEMENT_URL}"

            def fake_reader(url, **_):
                return result_for(10 if url == self.BASELINE_URL else 8, url)

            second = half_auto.ingest_manual_url_message(directory, second_message, identity="user", read_fn=fake_reader)
            self.assertEqual(second["status"], "completed")

    def test_two_plain_workbook_urls_trigger_quick_comparison_without_sheet_query(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            baseline_url = "https://li.feishu.cn/sheets/BaselineToken"
            replacement_url = "https://li.feishu.cn/sheets/ReplacementToken"
            message = f"AD P99 对比\n{baseline_url}，\n{replacement_url}"

            def fake_reader(url, **_):
                return result_for(10 if url == baseline_url else 8, url)

            result = half_auto.ingest_manual_url_message(directory, message, identity="user", read_fn=fake_reader)
            self.assertEqual(result["status"], "ok")
            self.assertIsNone(result["vin"])
            self.assertEqual(result["context_verification"]["status"], "user_unverified")
            self.assertEqual(result["comparisons"]["slow_driver"]["delta_ms"], -2.0)


if __name__ == "__main__":
    unittest.main()
