import unittest
from pathlib import Path
import sys

try:
    from tools.openclaw_skill_router import parse_asgard_dispatch, route_message
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from openclaw_skill_router import parse_asgard_dispatch, route_message


class SkillRouterTests(unittest.TestCase):
    P99_A = "https://example.feishu.cn/sheets/SampleSheetAlpha"
    P99_B = "https://example.feishu.cn/sheets/SampleSheetBeta"

    def test_routes_explicit_two_url_p99_request(self):
        decision = route_message(
            f"AD P99 对比\n{self.P99_A}\n{self.P99_B}", bot_mentioned=True
        )
        self.assertEqual(decision.route, "ad-p99-comparison")
        self.assertEqual(decision.sheet_urls, (self.P99_A, self.P99_B))

    def test_routes_two_url_comparison_without_exact_p99_spelling(self):
        decision = route_message(
            f"时延对比分析\n{self.P99_A}\n{self.P99_B}", bot_mentioned=True
        )
        self.assertEqual(decision.route, "ad-p99-comparison")

    def test_does_not_trigger_p99_for_one_url(self):
        decision = route_message(f"AD P99 对比 {self.P99_A}", bot_mentioned=True)
        self.assertEqual(decision.route, "no-match")

    def test_routes_explicit_uroad_request(self):
        decision = route_message(
            "请分析 uroad 日志，VIN: DEM0VEH1CLE000001，"
            "时间：2026-08-30 16:54:00 - 2026-08-30 17:24:00",
            bot_mentioned=True,
        )
        self.assertEqual(decision.route, "uroad-log-analysis")

    def test_does_not_route_two_time_p99_context_to_uroad(self):
        decision = route_message(
            "AD P99 对比 DEM0VEH1CLE000001 2026-08-30 16:54:00 "
            "2026-08-30 17:24:00 2026-08-30 18:55:00 2026-08-30 19:22:00",
            bot_mentioned=True,
        )
        self.assertEqual(decision.route, "no-match")

    def test_fsdlog_never_starts_a_local_skill(self):
        decision = route_message(
            "fsdlog DEM0VEH1CLE000001, 2026-08-30 18:55:00,"
            "2026-08-30 19:22:00, perf-m100-ultra",
            bot_mentioned=True,
        )
        self.assertEqual(decision.route, "no-match")

    def test_explicit_two_command_latency_request_routes_to_dispatch(self):
        text = (
            "fsdlog DEM0VEH1CLE000001, 2026-08-30 16:54:00,"
            "2026-08-30 17:24:00, perf-m100-ultra "
            "fsdlog DEM0VEH1CLE000001, 2026-08-30 18:55:00,"
            "2026-08-30 19:22:00, perf-m100-ultra 时延分析"
        )
        decision = route_message(text, bot_mentioned=True)
        commands = parse_asgard_dispatch(text)
        self.assertEqual(decision.route, "asgard-dispatch")
        self.assertEqual(len(commands), 2)
        self.assertNotEqual(commands[0], commands[1])

    def test_one_fsdlog_with_latency_word_is_not_dispatched(self):
        decision = route_message(
            "fsdlog DEM0VEH1CLE000001, 2026-08-30 16:54:00,"
            "2026-08-30 17:24:00, perf-m100-ultra 时延分析",
            bot_mentioned=True,
        )
        self.assertEqual(decision.route, "no-match")

    def test_pending_dispatch_requires_two_role_labelled_urls(self):
        text = f"Baseline: {self.P99_A}\n替换后: {self.P99_B}"
        decision = route_message(
            text, bot_mentioned=True, has_pending_ad_p99_dispatch=True
        )
        self.assertEqual(decision.route, "ad-p99-comparison")

        unlabelled = route_message(
            f"{self.P99_A}\n{self.P99_B}",
            bot_mentioned=True,
            has_pending_ad_p99_dispatch=True,
        )
        self.assertEqual(unlabelled.route, "no-match")

    def test_cross_vin_or_overlapping_commands_are_not_dispatched(self):
        text = (
            "时延分析 fsdlog DEM0VEH1CLE000001, 2026-08-30 16:54:00,"
            "2026-08-30 17:24:00, perf-m100-ultra "
            "fsdlog DEM0VEH1CLE000002, 2026-08-30 17:00:00,"
            "2026-08-30 19:22:00, perf-m100-ultra"
        )
        self.assertIsNone(parse_asgard_dispatch(text))
        self.assertEqual(route_message(text, bot_mentioned=True).route, "no-match")

    def test_continuation_wins_before_general_routing(self):
        decision = route_message(
            f"替换后 #23c6eec08965b620 {self.P99_B}", bot_mentioned=True
        )
        self.assertEqual(decision.route, "ad-p99-continuation")
        self.assertEqual(decision.comparison_key, "23c6eec08965b620")

    def test_message_without_mention_is_ignored(self):
        decision = route_message(
            f"AD P99 对比 {self.P99_A} {self.P99_B}", bot_mentioned=False
        )
        self.assertEqual(decision.route, "no-match")


if __name__ == "__main__":
    unittest.main()
