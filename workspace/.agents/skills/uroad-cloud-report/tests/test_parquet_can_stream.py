import copy
import hashlib
import json
import random
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import parquet_can_stream as subject
from parquet_to_asc import normalize_rows, rows_to_asc
from parse_can_signals import CST, parse_asc


class _Batch:
    def __init__(self, rows):
        self.rows = rows

    def to_pylist(self):
        return self.rows


class ParquetCanStreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "analysis" / "can-signals.json"
        self.index = self.root / "can-parquet" / "points.sqlite3"
        self.metadata = self.root / "can-parquet" / "contract-metadata.json"
        self.definitions = Path(__file__).parents[1] / "references" / "can-signals.json"
        self.start, self.end = "2026-11-03 10:22:00", "2026-11-03 10:22:02"
        self.epoch = datetime.fromisoformat(self.start).replace(tzinfo=CST).timestamp()
        self.binding = {"runId": "synthetic-only", "vin": "SYNTHETIC", "start": self.start,
                        "end": self.end, "environment": "prod", "providerEnvironment": "prod"}
        self.sources = {}
        self.batch_calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name, rows):
        path = self.root / name
        self.sources[str(path.resolve())] = rows
        return path

    def arrow(self):
        sources, calls = self.sources, self.batch_calls

        class ParquetFile:
            def __init__(self, path):
                self.rows = sources[str(Path(path).resolve())]
                self.schema_arrow = types.SimpleNamespace(names=list(dict.fromkeys(
                    key for row in self.rows for key in row)))

            def iter_batches(self, *, batch_size, columns, use_threads):
                calls.append({"batch_size": batch_size, "columns": columns, "use_threads": use_threads})
                for offset in range(0, len(self.rows), batch_size):
                    yield _Batch([{key: value for key, value in row.items() if key in columns}
                                  for row in self.rows[offset:offset + batch_size]])

            def close(self):
                pass

        arrow = types.ModuleType("pyarrow")
        arrow.__path__ = []
        parquet = types.ModuleType("pyarrow.parquet")
        parquet.ParquetFile = ParquetFile
        parquet.read_table = mock.Mock(side_effect=AssertionError("whole-file reads are forbidden"))
        arrow.parquet = parquet
        return mock.patch.dict(sys.modules, {"pyarrow": arrow, "pyarrow.parquet": parquet})

    def payload(self, can_id, value=1):
        size = 16 if can_id == 0x106 else 32
        data = bytearray(size)
        if can_id == 0x108:
            data[9] = (value & 7) << 1
        else:
            data[2:4] = value.to_bytes(2, "big")
        return bytes(data)

    def row(self, offset, can_id=0x12A, channel=2, value=1):
        return {"timestamp": self.epoch + offset, "can_id": can_id,
                "channel": channel, "data": self.payload(can_id, value)}

    def build(self, paths, **kwargs):
        with self.arrow():
            return subject.build_contract(paths, self.definitions, self.start, self.end,
                self.output, self.index, self.metadata, self.binding, **kwargs)

    def load(self, **kwargs):
        args = dict(output=self.output, index_path=self.index, metadata_path=self.metadata,
                    binding=self.binding, definitions_path=self.definitions)
        args.update(kwargs)
        return subject.load_indexed_contract(**args)

    def materialise(self, contract):
        result = dict(contract)
        result["signals"] = {key: {**definition, "series": list(definition["series"])}
                             for key, definition in contract["signals"].items()}
        return result

    def oracle(self, paths):
        rows = []
        for path in paths:
            rows.extend(normalize_rows(self.sources[str(path.resolve())]))
        rows.sort(key=lambda row: row["epoch"])
        asc = self.root / "oracle.asc"
        asc.write_text(rows_to_asc(rows), encoding="utf-8")
        result = parse_asc(asc, self.definitions, self.start, self.end, evidence_mode=True)
        result["sourceAsc"] = str(self.index.parent / "combined.asc")
        result["environment"] = {"environment": "prod", "providerEnvironment": "prod"}
        result["sourceParquet"] = {"files": len(paths), "rows": len(rows)}
        return result

    def test_unordered_multifile_matches_complete_legacy_oracle(self):
        rows = [self.row(-1, can_id=0x999), self.row(0), self.row(0.0007), self.row(0.0014),
                self.row(0.0000003, value=2), self.row(0.0000001, value=3),
                self.row(1, 0x106, 9, 8000), self.row(1.0005, 0x106, 2, 8000),
                self.row(1.0015, 0x106, 2, 8000), self.row(1, 0x108, 1, 6),
                self.row(1.5, 0x108, 1, 3), self.row(1, 0x12A, 9, 99),
                self.row(2.999999), self.row(3), self.row(-0.000001)]
        random.Random(17).shuffle(rows)
        paths = [self.source("a.parquet", rows[::2]), self.source("b.parquet", rows[1::2])]
        self.build(paths, batch_size=2)
        self.assertEqual(self.materialise(self.load()), self.oracle(paths))
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), self.oracle(paths))
        self.assertTrue(all(call["batch_size"] == 2 and call["use_threads"] is False for call in self.batch_calls))

    def test_alias_null_fallback_numbers_and_payload_forms(self):
        rows = [{"Timestamp": None, "ts": self.epoch * 1000, "CAN_ID": "12A", "channel": 2,
                 "data": None, "payload": list(self.payload(0x12A, 96)), "unneeded": "discard"},
                {"timestamp": self.epoch + 1, "id": "0x108", "channel": 0,
                 "payload": self.payload(0x108, 7).hex()},
                {"time": int((self.epoch + 2) * 1_000_000), "identifier": "106", "bus": "0x2",
                 "frame_data": self.payload(0x106, 7800)},
                {"time": datetime.fromtimestamp(self.epoch + 0.5, CST), "identifier": 0x106,
                 "bus": 9, "bytes": self.payload(0x106, 7800)}]
        paths = [self.source("aliases.pq", rows)]
        self.build(paths, batch_size=1)
        self.assertEqual(self.materialise(self.load()), self.oracle(paths))
        self.assertNotIn("unneeded", self.batch_calls[0]["columns"])
        self.assertIn("Timestamp", self.batch_calls[0]["columns"])
        self.assertIn("ts", self.batch_calls[0]["columns"])

    def test_duplicate_anchor_does_not_advance_on_discard(self):
        paths = [self.source("anchor.pq", [self.row(0), self.row(0.0007), self.row(0.0014)])]
        self.build(paths, batch_size=1)
        signal = self.load()["signals"]["vehicleSpeed"]
        self.assertEqual(signal["rawPointCount"], 3)
        self.assertEqual(len(signal["series"]), 2)
        self.assertEqual(self.materialise(self.load()), self.oracle(paths))

    def test_different_payload_same_decoded_raw_is_not_collapsed(self):
        first, second = self.row(0), self.row(0.0005)
        data = bytearray(second["data"])
        data[-1] = 5
        second["data"] = bytes(data)
        paths = [self.source("payload.pq", [first, second])]
        self.build(paths)
        points = list(self.load()["signals"]["vehicleSpeed"]["series"])
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["raw"], points[1]["raw"])

    def test_negative_zero_relative_header_matches_old_parser(self):
        paths = [self.source("header.pq", [self.row(0.0000007), self.row(0.002)])]
        self.build(paths)
        self.assertEqual(self.materialise(self.load()), self.oracle(paths))

    def test_full_evidence_over_2000_and_repeatable_read_only_iterators(self):
        self.end = "2026-11-03 10:22:10"
        self.binding["end"] = self.end
        paths = [self.source("large.pq", [self.row(i * 0.002, 0x108, 1, i % 8) for i in range(4600)])]
        self.build(paths, batch_size=37)
        contract = self.load()
        series = contract["signals"]["gear"]["series"]
        self.assertIsInstance(series, subject.SQLiteSeries)
        self.assertEqual(len(series), 4600)
        self.assertEqual(sum(1 for _ in series), 4600)
        self.assertEqual(sum(1 for _ in series), 4600)
        self.assertFalse(contract["signals"]["gear"]["downsampled"])
        self.assertEqual(self.materialise(contract), self.oracle(paths))
        iterator = iter(series)
        next(iterator)
        iterator.close()
        # Closing even a partially consumed iterator releases Windows handles.
        renamed = self.index.with_suffix(".renamed")
        self.index.rename(renamed)
        renamed.rename(self.index)

    def test_evidence_store_existing_api_consumes_disk_series(self):
        from process_time_chunks import EvidenceStore
        paths = [self.source("evidence.pq", [self.row(0), self.row(0.1, 0x108, 1, 2), self.row(0.2, 0x106, 2, 7800)])]
        self.build(paths)
        store = EvidenceStore(self.root / "evidence.sqlite3")
        try:
            store.add_slice(1, {}, self.load())
            self.assertEqual(store.row_count("can:vehicleSpeed"), 1)
            self.assertEqual(store.row_count("can:gear"), 1)
            self.assertEqual(store.row_count("can:steeringAngle"), 1)
        finally:
            store.close()

    def test_receipt_rejects_each_binding_path_hash_or_contract_change(self):
        paths = [self.source("receipt.pq", [self.row(0)])]
        self.build(paths)
        original = self.metadata.read_text(encoding="utf-8")
        for field, replacement in (("binding", {**self.binding, "runId": "other"}),
                                   ("outputSha256", "0" * 64), ("indexSha256", "0" * 64),
                                   ("definitionsSha256", "0" * 64), ("signalCounts", {}),
                                   ("paths", {"output": "other"})):
            with self.subTest(field=field):
                metadata = json.loads(original)
                metadata[field] = replacement
                self.metadata.write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.load()
        self.metadata.write_text(original, encoding="utf-8")
        changed = json.loads(original)
        changed["contract"]["warnings"].append("tampered")
        changed["contractSha256"] = subject._object_hash(changed["contract"])
        self.metadata.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "索引与收据"):
            self.load()
        self.metadata.write_text(original, encoding="utf-8")
        with self.assertRaises(ValueError):
            self.load(binding={**self.binding, "runId": "wrong"})
        other = self.root / "other.json"
        other.write_bytes(self.output.read_bytes())
        with self.assertRaisesRegex(ValueError, "路径"):
            self.load(output=other)

    def test_output_index_and_definition_file_tampering_rejected(self):
        paths = [self.source("files.pq", [self.row(0)])]
        self.build(paths)
        for path in (self.output, self.index):
            original = path.read_bytes()
            path.write_bytes(original + b"x")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                self.load()
            path.write_bytes(original)
        definitions = self.root / "definitions.json"
        definitions.write_bytes(self.definitions.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "信号定义"):
            self.load(definitions_path=definitions)

    def test_failed_memory_error_retains_partial_and_nonempty_safe_diagnostic(self):
        path = self.source("memory.pq", [self.row(0)])
        progress = []
        self.output.parent.mkdir()
        self.output.write_text("existing-output", encoding="utf-8")
        with mock.patch.object(subject, "_iter_batches", side_effect=MemoryError()):
            with self.assertRaises(MemoryError) as caught:
                subject.build_contract([path], self.definitions, self.start, self.end,
                    self.output, self.index, self.metadata, self.binding, progress=progress.append)
        diagnostic = subject.failure_diagnostic(caught.exception)
        self.assertEqual(diagnostic["exceptionType"], "MemoryError")
        self.assertIn("内存不足", diagnostic["error"])
        self.assertEqual(diagnostic["stage"], "reading_parquet")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing-output")
        self.assertFalse(self.index.exists())
        self.assertFalse(self.metadata.exists())
        self.assertTrue(list(self.index.parent.glob("points.sqlite3.building-*")))
        self.assertEqual(progress[-1]["stage"], "failed")
        self.assertTrue(subject.failure_diagnostic(MemoryError())["error"])

    def test_failed_progress_callback_cannot_mask_success(self):
        paths = [self.source("progress.pq", [self.row(0)])]
        def broken(_):
            raise RuntimeError("progress only")
        self.build(paths, progress=broken)
        self.assertEqual(len(self.load()["signals"]["vehicleSpeed"]["series"]), 1)

    def test_empty_missing_and_nonfinite_rows_fail_without_completion_receipt(self):
        for rows in ([], [{"can_id": 0x12A, "data": b"0"}], [self.row(float("nan"))]):
            with self.subTest(rows=rows):
                paths = [self.source("invalid.pq", rows)]
                with self.assertRaises(subject.ParquetStreamError):
                    self.build(paths)
                self.assertFalse(self.metadata.exists())
                self.assertFalse(self.index.exists())

    def test_existing_completed_index_not_silently_overwritten(self):
        paths = [self.source("repeat.pq", [self.row(0)])]
        self.build(paths)
        original = self.index.read_bytes()
        with self.assertRaises(subject.ParquetStreamError):
            self.build(paths)
        self.assertEqual(self.index.read_bytes(), original)

    def test_host_provider_mapping_testtwo_is_preserved_and_bound(self):
        self.binding.update(environment="test", providerEnvironment="testtwo")
        paths = [self.source("test-provider.pq", [self.row(0)])]
        self.build(paths)
        contract = self.load()
        self.assertEqual(contract["environment"], {"environment": "test", "providerEnvironment": "testtwo"})
        with self.assertRaisesRegex(ValueError, "任务绑定"):
            self.load(binding={**self.binding, "providerEnvironment": "test"})

    def test_real_parquet_row_groups_and_batches_match_legacy_oracle(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("the controlled pyarrow runtime is unavailable")
        rows = [self.row(-1, 0x999), self.row(1.0005, 0x106, 9, 7900),
                self.row(1, 0x106, 2, 7900), self.row(0.0007), self.row(0),
                self.row(0.0014), self.row(1, 0x108, 1, 7)]
        paths = [self.source("real.parquet", rows)]
        pq.write_table(pa.Table.from_pylist(rows), paths[0], row_group_size=3)
        subject.build_contract(paths, self.definitions, self.start, self.end,
            self.output, self.index, self.metadata, self.binding, batch_size=2)
        self.assertEqual(self.materialise(self.load()), self.oracle(paths))

    def typed_fixture(self, rows, *, name="typed.parquet", timestamp_timezone=None,
                      payload_type=None, extra_alias=False):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("the controlled pyarrow runtime is unavailable")
        schema = pa.schema([("channel_id", pa.uint8()), ("msg_id", pa.uint32()),
                            ("timestamp", pa.timestamp("us", tz=timestamp_timezone)),
                            ("channel_type", pa.uint8()), ("data", payload_type or pa.binary())]
                           + ([("can_id", pa.uint32())] if extra_alias else []))
        converted = []
        for row in rows:
            timestamp = row.get("timestamp")
            if isinstance(timestamp, (int, float)):
                timestamp = datetime.fromtimestamp(timestamp, timezone.utc)
                if timestamp_timezone is None:
                    timestamp = timestamp.replace(tzinfo=None)
            converted.append({"channel_id": row.get("channel"), "msg_id": row.get("can_id"),
                              "timestamp": timestamp, "channel_type": 1, "data": row.get("data"),
                              **({"can_id": row.get("extra_id", row.get("can_id"))} if extra_alias else {})})
        path = self.source(name, converted)
        pq.write_table(pa.Table.from_pylist(converted, schema=schema), path, row_group_size=3)
        return path

    def real_build(self, paths, batch_size=2):
        return subject.build_contract(paths, self.definitions, self.start, self.end,
            self.output, self.index, self.metadata, self.binding, batch_size=batch_size)

    def test_actual_typed_schema_filters_only_targets_and_preserves_header_and_order(self):
        rows = [self.row(1, 0x999), self.row(0.0014), self.row(1, 0x108, 0, 7),
                self.row(-1, 0x999), self.row(0.0007), self.row(0), self.row(3),
                self.row(1, 0x106, 9, 7900), self.row(1.0005, 0x106, 2, 7900),
                self.row(1, 0x999), self.row(0.5, 0x108, None, 2)]
        paths = [self.typed_fixture(rows[::2], name="typed-a.parquet"),
                 self.typed_fixture(rows[1::2], name="typed-b.parquet")]
        with mock.patch.object(subject, "_normalise_row", wraps=subject._normalise_row) as normalise:
            self.real_build(paths)
        self.assertEqual(normalise.call_count, sum(row["can_id"] in (0x12A, 0x108, 0x106) for row in rows))
        self.assertEqual(self.materialise(self.load()), self.oracle(paths))
        batches = list(subject._iter_batches(paths[0], 2, target_ids={0x12A, 0x108, 0x106}))
        self.assertTrue(all(isinstance(batch, subject.BatchView) for batch in batches))
        self.assertEqual(sum(batch.total_rows for batch in batches), len(rows[::2]))
        self.assertEqual(self.load()["sourceParquet"]["rows"], len(rows))

    def test_typed_required_null_non_target_falls_back_and_keeps_old_failure(self):
        for field in ("timestamp", "can_id", "data"):
            with self.subTest(field=field):
                row = self.row(-1, 0x999)
                row[field] = None
                path = self.typed_fixture([row, self.row(0)], name="null-" + field + ".parquet")
                batches = list(subject._iter_batches(path, 4096, target_ids={0x12A, 0x108, 0x106}))
                self.assertIsInstance(batches[0], list)
                with self.assertRaises(subject.ParquetStreamError) as caught:
                    self.real_build([path])
                self.assertIsInstance(caught.exception.__cause__, ValueError)
                with self.assertRaises(ValueError):
                    self.oracle([path])

    def test_typed_multiple_aliases_fall_back_to_all_rows_and_existing_precedence(self):
        rows = [self.row(-1, 0x999), {**self.row(0), "extra_id": 0x999}, self.row(1, 0x108, 1, 4)]
        path = self.typed_fixture(rows, extra_alias=True)
        with mock.patch.object(subject, "_normalise_row", wraps=subject._normalise_row) as normalise:
            self.real_build([path])
        self.assertEqual(normalise.call_count, len(rows))
        self.assertEqual(self.materialise(self.load()), self.oracle([path]))

    def test_typed_utc_and_timezone_aware_timestamps_match_original_rules(self):
        for zone in (None, "UTC", "Asia/Shanghai"):
            with self.subTest(zone=zone):
                path = self.typed_fixture([self.row(-1, 0x999), self.row(-0.000001), self.row(0),
                    self.row(2.999999), self.row(3)], name="timezone-" + str(zone).replace("/", "-") + ".parquet",
                    timestamp_timezone=zone)
                self.real_build([path])
                self.assertEqual(self.materialise(self.load()), self.oracle([path]))
                for artifact in (self.output, self.index, self.metadata):
                    artifact.unlink()

    def test_typed_large_and_fixed_binary_payloads_take_fast_path(self):
        import pyarrow as pa
        for payload_type in (pa.large_binary(), pa.binary(32)):
            with self.subTest(payload_type=payload_type):
                path = self.typed_fixture([self.row(-1, 0x999), self.row(0), self.row(1, 0x108, 1, 4)],
                                          payload_type=payload_type)
                batches = list(subject._iter_batches(path, 2, target_ids={0x12A, 0x108, 0x106}))
                self.assertTrue(all(isinstance(batch, subject.BatchView) for batch in batches))
                self.real_build([path])
                self.assertEqual(self.materialise(self.load()), self.oracle([path]))
                for artifact in (self.output, self.index, self.metadata):
                    artifact.unlink()

    def test_non_guaranteed_types_do_not_enable_fast_path(self):
        import pyarrow as pa
        fields = [("timestamp", pa.timestamp("us")), ("msg_id", pa.uint32()),
                  ("channel_id", pa.uint8()), ("data", pa.binary())]
        for column, replacement in (("timestamp", pa.int64()), ("timestamp", pa.string()),
                                    ("msg_id", pa.string()), ("msg_id", pa.int64()),
                                    ("msg_id", pa.float64()), ("channel_id", pa.string()),
                                    ("data", pa.string())):
            with self.subTest(column=column, replacement=replacement):
                schema = pa.schema([(name, replacement if name == column else kind) for name, kind in fields])
                self.assertIsNone(subject._typed_columns(schema, pa))

    def test_typed_original_ordinals_include_filtered_rows_across_batch_boundaries(self):
        path = self.typed_fixture([self.row(0), self.row(-1, 0x999), self.row(0.0000004, value=2),
                                   self.row(1, 0x999), self.row(0.0000001, value=3)])
        views = list(subject._iter_batches(path, 2, target_ids={0x12A, 0x108, 0x106}))
        offsets = []
        base = 0
        for view in views:
            offsets.extend(base + index for index, _ in view.selected)
            base += view.total_rows
        self.assertEqual(offsets, [0, 2, 4])
        self.real_build([path])
        self.assertEqual(self.materialise(self.load()), self.oracle([path]))

    def test_nanosecond_non_target_interior_does_not_hide_old_conversion_error(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        base = int(self.epoch * 1_000_000) * 1000
        schema = pa.schema([("timestamp", pa.timestamp("ns")), ("msg_id", pa.uint32()),
                            ("channel_id", pa.uint8()), ("data", pa.binary())])
        rows = [{"timestamp": base + offset, "msg_id": identifier, "channel_id": 2,
                 "data": self.payload(0x12A)}
                for offset, identifier in ((0, 0x12A), (400, 0x999), (1000, 0x12A))]
        table = pa.Table.from_pylist(rows, schema=schema)
        self.assertIsNone(subject._typed_columns(schema, pa))
        path = self.root / "ns-interior.parquet"
        pq.write_table(table, path)
        try:
            original_rows = table.to_pylist()
        except (ValueError, OverflowError) as original:
            with self.assertRaises(subject.ParquetStreamError) as caught:
                self.real_build([path], batch_size=4096)
            self.assertIsInstance(caught.exception.__cause__, type(original))
        else:
            self.sources[str(path.resolve())] = original_rows
            self.real_build([path], batch_size=4096)
            self.assertEqual(self.materialise(self.load()), self.oracle([path]))


if __name__ == "__main__":
    unittest.main()
