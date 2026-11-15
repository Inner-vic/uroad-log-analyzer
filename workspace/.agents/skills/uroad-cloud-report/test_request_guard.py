import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from request_guard import reject_non_uroad_request


class RequestGuardTests(unittest.TestCase):
    def test_direct_explicit_skill_call_is_allowed(self):
        with patch.dict(os.environ, {}, clear=True):
            reject_non_uroad_request({"channel": "feishu", "rawMessage": "日志分析"})

    def test_other_explicit_route_is_rejected(self):
        with patch.dict(os.environ, {"CC_CONNECT_ROUTE": "ad-p99-analysis"}, clear=True):
            with self.assertRaises(RuntimeError):
                reject_non_uroad_request({"channel": "feishu", "rawMessage": "日志分析"})

    def test_uroad_route_is_allowed(self):
        with patch.dict(os.environ, {"CC_CONNECT_ROUTE": "uroad-cloud-report"}, clear=True):
            reject_non_uroad_request({"channel": "feishu", "rawMessage": "日志分析"})

    def test_latency_never_enters_uroad(self):
        with patch.dict(os.environ, {"CC_CONNECT_ROUTE": "uroad-cloud-report"}, clear=True):
            with self.assertRaises(RuntimeError):
                reject_non_uroad_request({"channel": "feishu", "rawMessage": "时延分析 VIN"})


if __name__ == "__main__":
    unittest.main()