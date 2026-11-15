import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import delivery_bridge


class DeliveryProjectTests(unittest.TestCase):
    def test_omits_display_name_and_lets_single_runtime_project_resolve(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(delivery_bridge._project_args(), [])

    def test_explicit_project_override_wins(self):
        with mock.patch.dict(os.environ, {"UROAD_CC_CONNECT_PROJECT": "from-env"}, clear=False):
            self.assertEqual(delivery_bridge._project_args("explicit"), ["--project", "explicit"])


if __name__ == "__main__":
    unittest.main()
