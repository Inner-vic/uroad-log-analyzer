import unittest
import types
from datetime import datetime, timezone
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from parquet_to_asc import normalize_rows, read_parquet, rows_to_asc


class ParquetToAscTests(unittest.TestCase):
    def test_read_parquet_uses_pyarrow_table_rows(self):
        expected = [{"timestamp": 1, "can_id": 0x12A, "data": b"\x01"}]
        parquet_module = types.ModuleType("pyarrow.parquet")
        parquet_module.read_table = mock.Mock(return_value=mock.Mock(to_pylist=mock.Mock(return_value=expected)))
        pyarrow_module = types.ModuleType("pyarrow")
        pyarrow_module.__path__ = []
        pyarrow_module.parquet = parquet_module

        with mock.patch.dict("sys.modules", {"pyarrow": pyarrow_module, "pyarrow.parquet": parquet_module}):
            self.assertEqual(read_parquet(Path("fixture.parquet")), expected)
        parquet_module.read_table.assert_called_once_with(Path("fixture.parquet"))

    def test_common_schema_and_hex_payload(self):
        rows = normalize_rows([
            {"timestamp": "2026-09-08T05:37:00Z", "can_id": "0x12A", "channel": 2, "data": "01 02 0a ff"},
            {"timestamp": 1788845821000, "can_id": 0x108, "channel": 1, "payload": [1, 2]},
        ])
        self.assertEqual(rows[0]["can_id"], 0x12A)
        self.assertEqual(rows[0]["data"], b"\x01\x02\x0a\xff")
        self.assertEqual(rows[1]["data"], b"\x01\x02")

    def test_renders_parseable_asc(self):
        rows = normalize_rows([{"ts": 1788845820.25, "id": "12A", "bus": 2, "payload": "01020aff"}])
        text = rows_to_asc(rows)
        self.assertIn("date Tue Sep 08 13:37:00", text)
        self.assertIn("2 12A Rx d 4 01 02 0A FF", text)
        self.assertTrue(text.endswith("\n"))

    def test_missing_column_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "missing timestamp"):
            normalize_rows([{"can_id": 1, "data": "00"}])


if __name__ == "__main__":
    unittest.main()
