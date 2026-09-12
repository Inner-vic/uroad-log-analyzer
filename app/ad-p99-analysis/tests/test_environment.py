import contextlib
import io
import json
import unittest
from unittest.mock import patch

from test_lark_source import FakeReader, URL
import check_environment


class EnvironmentTests(unittest.TestCase):
    def test_default_preflight_never_touches_a_document(self):
        with patch("check_environment.check_environment", return_value={"status": "ok", "document_access": "not_checked"}), \
                patch("check_environment.check_access") as access, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(check_environment.main([]), 0)
            access.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())["document_access"], "not_checked")

    def test_metadata_probe_reads_no_cell_content(self):
        reader = FakeReader()
        result = check_environment.check_access(URL, "user", reader=reader)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["cell_access"], "not_checked")
        self.assertEqual([command for command, _ in reader.calls], ["+revision-get", "+workbook-info"])
        self.assertNotIn("sample", json.dumps(result))

    def test_probe_requires_identity_and_checks_target_counts(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            check_environment.main(["--sheet-url", URL])
        reader = FakeReader()
        del reader.book["VLA Parking"]
        self.assertEqual(check_environment.check_access(URL, "bot", reader=reader)["status"], "failed")
