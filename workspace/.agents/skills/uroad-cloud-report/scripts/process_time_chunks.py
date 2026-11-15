#!/usr/bin/env python3
"""Task-wide Uroad/CAN processing for the local CC Connect deployment."""
from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import time
from copy import deepcopy
from collections import Counter
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from time import perf_counter
from parquet_can_stream import load_indexed_contract

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_SERIES_POINTS = 2000
REQUIRED_CAN_SIGNALS = {"vehicleSpeed", "gear", "steeringAngle"}


class EvidenceStore:
    """Transactional disk index for cross-slice evidence."""
    UROAD_KINDS = {"frames": "frames", "events": "events", "timeline": "timeline", "edsgCycles": "edsgCycles"}
    SERIES = ("preCostSeries", "inferCostSeries", "postCostSeries", "e2eCostSeries", "fpsSeries")

    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE evidence(kind TEXT, stable_key TEXT, ts TEXT, payload TEXT, PRIMARY KEY(kind, stable_key));
            CREATE TABLE dedup(kind TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE slice_contract(slice INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE can_schema(signal TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE can_counts(signal TEXT PRIMARY KEY, raw_count INTEGER NOT NULL, slice_unique_count INTEGER NOT NULL);
            CREATE TABLE can_diag(signal TEXT PRIMARY KEY, channels TEXT NOT NULL, unknown_values TEXT NOT NULL);
            CREATE TABLE can_warning(value TEXT PRIMARY KEY);
            CREATE TABLE can_source_dedup(kind TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE can_environment(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
        """)

    def _insert(self, kind: str, item: dict) -> None:
        stable = json.dumps(("can", format(float(item.get("epochMs")), ".6f"), item.get("raw")) if kind.startswith("can:") and item.get("epochMs") is not None else _dedupe_key(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor = self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)", (kind, stable, _key(item)[0], json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
        if cursor.rowcount == 0:
            self.db.execute("INSERT INTO dedup VALUES(?,1) ON CONFLICT(kind) DO UPDATE SET count=count+1", (kind,))

    def add_slice(self, index: int, analysis: dict, can_data: dict) -> None:
        # Only these two containers are popped below; evidence records are
        # serialized without mutation. Avoid duplicating a full raw analysis.
        contract = dict(analysis)
        contract["charts"] = dict(analysis.get("charts") or {})
        with self.db:
            environment = can_data.get("environment")
            if isinstance(environment, dict) and any(environment.get(key) is not None for key in ("environment", "providerEnvironment")):
                if any(not isinstance(environment.get(key), str) or not environment[key] for key in ("environment", "providerEnvironment")):
                    raise RuntimeError("CAN evidence environment is incomplete")
                encoded_environment = json.dumps({key: environment[key] for key in ("environment", "providerEnvironment")}, sort_keys=True)
                prior_environment = self.db.execute("SELECT payload FROM can_environment WHERE id=1").fetchone()
                if prior_environment and prior_environment[0] != encoded_environment:
                    raise RuntimeError("CAN evidence environment mismatch")
                self.db.execute("INSERT OR IGNORE INTO can_environment VALUES(1,?)", (encoded_environment,))
            for field, kind in self.UROAD_KINDS.items():
                for item in contract.pop(field, []):
                    self._insert(kind, item)
            charts = contract.setdefault("charts", {})
            for series in self.SERIES:
                for item in charts.pop(series, []):
                    self._insert("series:" + series, item)
            charts.pop("canSignals", None)
            for signal, definition in can_data.get("signals", {}).items():
                schema = {key: definition.get(key) for key in ("name", "canId", "logicalBus", "unit", "confidence", "validation", "configuredChannels", "channelMapping")}
                encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                existing = self.db.execute("SELECT payload FROM can_schema WHERE signal=?", (signal,)).fetchone()
                if existing and existing[0] != encoded:
                    raise RuntimeError(f"CAN signal schema mismatch across slices: {signal}")
                self.db.execute("INSERT OR IGNORE INTO can_schema VALUES(?,?)", (signal, encoded))
                self.db.execute("INSERT INTO can_counts VALUES(?,?,?) ON CONFLICT(signal) DO UPDATE SET raw_count=raw_count+excluded.raw_count,slice_unique_count=slice_unique_count+excluded.slice_unique_count", (signal, definition.get("rawPointCount", len(definition.get("series", []))), definition.get("uniquePointCount", len(definition.get("series", [])))))
                prior = self.db.execute("SELECT channels,unknown_values FROM can_diag WHERE signal=?", (signal,)).fetchone()
                channels = set(definition.get("channels", [])) | (set(json.loads(prior[0])) if prior else set())
                unknown = set(definition.get("unknownRawValues", [])) | (set(json.loads(prior[1])) if prior else set())
                self.db.execute("INSERT INTO can_diag VALUES(?,?,?) ON CONFLICT(signal) DO UPDATE SET channels=excluded.channels,unknown_values=excluded.unknown_values", (signal, json.dumps(sorted(channels)), json.dumps(sorted(unknown))))
                for item in definition.get("series", []):
                    self._insert("can:" + signal, item)
            for warning in can_data.get("warnings", []):
                self.db.execute("INSERT OR IGNORE INTO can_warning VALUES(?)", (warning,))
            for kind, count in can_data.get("deduplication", {}).items():
                if kind == "segments" and isinstance(count, int) and count >= 0:
                    self.db.execute("INSERT INTO can_source_dedup VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET count=count+excluded.count", (kind, count))
            self.db.execute("INSERT INTO slice_contract VALUES(?,?)", (index, json.dumps(contract, ensure_ascii=False, separators=(",", ":"))))

    def iter_rows(self, kind: str):
        for row in self.db.execute("SELECT payload FROM evidence WHERE kind=? ORDER BY COALESCE(json_extract(payload,'$.epochMs'),ts), stable_key", (kind,)):
            yield json.loads(row[0])

    def rows(self, kind: str) -> list[dict]:
        return list(self.iter_rows(kind))

    def row_count(self, kind: str) -> int:
        return self.db.execute("SELECT count(*) FROM evidence WHERE kind=?", (kind,)).fetchone()[0]

    def sampled(self, kind: str, limit: int = MAX_SERIES_POINTS) -> list[dict]:
        return _sample_iterator(self.iter_rows(kind), self.row_count(kind), limit)

    def duplicate_counts(self) -> dict:
        return dict(self.db.execute("SELECT kind,count FROM dedup"))

    def iter_contracts(self):
        for row in self.db.execute("SELECT payload FROM slice_contract ORDER BY slice"):
            yield json.loads(row[0])

    def finalize_can(self) -> dict:
        signals, dedup = {}, self.duplicate_counts()
        warnings = [row[0] for row in self.db.execute("SELECT value FROM can_warning ORDER BY value")]
        provider_environment = None
        data_environment = None
        for signal, encoded in self.db.execute("SELECT signal,payload FROM can_schema ORDER BY signal"):
            definition = json.loads(encoded)
            unique_count = self.row_count("can:" + signal)
            if signal == "gear":
                transition_sql = "SELECT payload FROM (SELECT payload,json_extract(payload,'$.raw') raw,LAG(json_extract(payload,'$.raw')) OVER (ORDER BY COALESCE(json_extract(payload,'$.epochMs'),ts)) prev,LEAD(json_extract(payload,'$.raw')) OVER (ORDER BY COALESCE(json_extract(payload,'$.epochMs'),ts)) nxt FROM evidence WHERE kind=?) WHERE prev IS NULL OR nxt IS NULL OR raw!=prev OR raw!=nxt"
                transition_count = self.db.execute("SELECT count(*) FROM (" + transition_sql + ")", ("can:" + signal,)).fetchone()[0]
                transitions = (json.loads(row[0]) for row in self.db.execute(transition_sql, ("can:" + signal,)))
                rendered = _sample_iterator(transitions, transition_count)
            else:
                rendered = self.sampled("can:" + signal)
            raw_count = self.db.execute("SELECT raw_count FROM can_counts WHERE signal=?", (signal,)).fetchone()[0]
            diag = self.db.execute("SELECT channels,unknown_values FROM can_diag WHERE signal=?", (signal,)).fetchone()
            definition.update(channels=json.loads(diag[0]), unknownRawValues=json.loads(diag[1]), series=rendered, rawPointCount=raw_count, uniquePointCount=unique_count, outputPointCount=len(rendered), downsampled=len(rendered) < unique_count, coverage={"start": rendered[0].get("ts") if rendered else None, "end": rendered[-1].get("ts") if rendered else None})
            env = definition.get("environment") or {}
            if provider_environment is None:
                provider_environment = env.get("providerEnvironment")
                data_environment = env.get("environment")
            signals[signal] = definition
        populated_names = {definition.get("name") for definition in signals.values() if definition.get("series")}
        warnings = [warning for warning in warnings if not (
            "无数据" in warning and any(name and warning.startswith(name) for name in populated_names)
        )]
        missing = sorted(key for key in REQUIRED_CAN_SIGNALS if key not in signals or not signals[key].get("series"))
        if missing:
            raise RuntimeError(f"CAN 信号解析不完整: {', '.join(missing)}")
        source_dedup = dict(self.db.execute("SELECT kind,count FROM can_source_dedup"))
        environment_record = self.db.execute("SELECT payload FROM can_environment WHERE id=1").fetchone()
        if environment_record:
            environment = json.loads(environment_record[0])
            data_environment, provider_environment = environment["environment"], environment["providerEnvironment"]
        return {"schemaVersion": "1.0", "sourceAsc": "chunked", "headerTime": None, "signals": signals, "warnings": warnings,
                "deduplication": {**{key.removeprefix("can:"): value for key, value in dedup.items() if key.startswith("can:")}, **source_dedup},
                "environment": {"environment": data_environment, "providerEnvironment": provider_environment}}

    def finalize_uroad(self, can_data: dict, query: dict, run_id: str) -> dict:
        contracts = self.iter_contracts()
        try:
            report = merge_uroad([next(contracts)], can_data, query, run_id)
        except StopIteration as exc:
            raise RuntimeError("No completed slice contracts in evidence store") from exc
        for contract in contracts:
            report = merge_uroad([report, contract], can_data, query, run_id)
        dedup = self.duplicate_counts()
        for field, kind in self.UROAD_KINDS.items():
            report[field] = list(self.iter_rows(kind))
        for series in self.SERIES:
            kind = "series:" + series
            report["charts"][series] = self.sampled(kind)
            values = (point["value"] for point in self.iter_rows(kind))
            stats = {"count": 0, "sum": 0, "min": None, "max": None, "latest": None}
            for value in values:
                stats["count"] += 1; stats["sum"] += value; stats["latest"] = value
                stats["min"] = value if stats["min"] is None else min(stats["min"], value)
                stats["max"] = value if stats["max"] is None else max(stats["max"], value)
            if series in {"preCostSeries", "inferCostSeries", "postCostSeries", "e2eCostSeries"}:
                metric_name = {"preCostSeries": "preTotalMs", "inferCostSeries": "inferenceChainMs", "postCostSeries": "postTotalMs", "e2eCostSeries": "e2eMs"}[series]
                coverage = report["metrics"].get(metric_name, {}).get("coverage")
                performance_stats = {key: value for key, value in stats.items() if key != "latest"}
                report["metrics"][metric_name] = {**performance_stats, "avg": round(stats["sum"] / stats["count"], 3) if stats["count"] else None, **({"coverage": coverage} if coverage else {})}
            elif series == "fpsSeries":
                report["metrics"]["fps"] = stats
                report["summary"]["latestFps"] = stats["latest"]
        report["summary"]["avgE2EMs"] = report["metrics"]["e2eMs"]["avg"]
        report["metrics"]["eventCount"] = len(report["events"])
        report["metrics"]["frameCount"] = len(report["frames"])
        skip_counts = Counter(event.get("reasonCode") for event in report["events"] if event.get("category") == "skip" and event.get("reasonCode"))
        error_counts = Counter(event.get("reasonCode") for event in report["events"] if event.get("category") in {"error", "warning"} and event.get("reasonCode"))
        report["charts"]["skipReasonStats"] = [{"reason": key, "count": value} for key, value in sorted(skip_counts.items())]
        report["charts"]["errorReasonStats"] = [{"reason": key, "count": value} for key, value in sorted(error_counts.items())]
        report["metrics"]["skipReasonStats"] = dict(skip_counts)
        report["metrics"]["errorReasonStats"] = dict(error_counts)
        report["metrics"]["skipEventCount"] = sum(skip_counts.values())
        skipped = sum(f.get("status") == "skipped" for f in report["frames"])
        report["metrics"]["associatedSkippedCycles"] = skipped
        report["summary"].update(totalFrames=len(report["frames"]), normalFrames=sum(f.get("status") == "normal" for f in report["frames"]), skippedFrames=skipped, errorFrames=sum(f.get("status") == "error" for f in report["frames"]), skipEventCount=sum(skip_counts.values()), associatedSkippedCycles=skipped)
        report["summaryFacts"] = [f"识别到 {len(report['frames'])} 个带 frame 的事件记录。", f"识别到 {len(report['events'])} 条 uroad/关联事件。"]
        report["deduplication"] = {key: value for key, value in dedup.items() if not key.startswith("can:")}
        return report

    def close(self) -> None:
        self.db.close()


def _key(item: dict) -> tuple:
    timestamp = next((item[name] for name in ("ts", "timestamp", "preTimingTimestamp") if item.get(name) is not None), "")
    identifier = next((item[name] for name in ("eventId", "frame", "sequence", "epochMs") if item.get(name) is not None), "")
    return (str(timestamp), str(identifier))


def _dedupe_key(item: dict) -> tuple:
    if item.get("eventId") is not None:
        return ("event", item["eventId"])
    if item.get("epochMs") is not None:
        return ("point", item["epochMs"], item.get("raw"), item.get("value"))
    if item.get("frame") is not None:
        return ("frame", item.get("preTimingTimestamp") or item.get("postTimingTimestamp"), item.get("frameNamespace"), item["frame"])
    if item.get("ts") is not None and (item.get("title") is not None or item.get("category") is not None):
        return ("timeline", item["ts"], item.get("category"), item.get("title"), item.get("description"))
    return ("stable", json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _merge_records(parts: list[list[dict]]) -> tuple[list[dict], int]:
    unique, total = {}, 0
    for records in parts:
        for record in records:
            total += 1
            unique.setdefault(_dedupe_key(record), record)
    return sorted(unique.values(), key=_key), total - len(unique)


def _merge_metric(items: list[dict]) -> dict:
    valid = [item for item in items if isinstance(item, dict) and item.get("count", 0)]
    count = sum(item["count"] for item in valid)
    if not count:
        return {"count": 0, "sum": 0, "min": None, "max": None, "avg": None}
    total = sum(item.get("sum", item["avg"] * item["count"]) for item in valid)
    return {"count": count, "sum": total, "min": min(item["min"] for item in valid), "max": max(item["max"] for item in valid), "avg": round(total / count, 3)}


def _merge_counter(parts: list[dict]) -> dict:
    result = Counter()
    for part in parts:
        result.update(part or {})
    return dict(result)


def _cap_extrema(points: list[dict], limit: int = MAX_SERIES_POINTS) -> list[dict]:
    if len(points) <= limit:
        return points
    if limit <= 0:
        return []
    if limit == 1:
        return [points[0]]
    if limit == 2:
        return [points[0], points[-1]]
    if limit == 3:
        extreme = max(points, key=lambda p: abs(p["value"]))
        return sorted({_key(point): point for point in (points[0], extreme, points[-1])}.values(), key=_key)
    anchors = [points[0], points[-1]]
    anchors.extend((min(points, key=lambda p: p["value"]), max(points, key=lambda p: p["value"])))
    selected = {_key(point): point for point in anchors}
    buckets = max(1, limit // 2)
    candidates = []
    for index in range(buckets):
        chunk = points[index * len(points) // buckets:(index + 1) * len(points) // buckets]
        if chunk:
            candidates.extend((min(chunk, key=lambda p: p["value"]), max(chunk, key=lambda p: p["value"])))
    candidates = [point for point in sorted({_key(point): point for point in candidates}.values(), key=_key) if _key(point) not in selected]
    remaining = limit - len(selected)
    if remaining > 0 and candidates:
        if len(candidates) <= remaining:
            chosen = candidates
        elif remaining == 1:
            chosen = [candidates[len(candidates) // 2]]
        else:
            chosen = [candidates[round(index * (len(candidates) - 1) / (remaining - 1))] for index in range(remaining)]
        selected.update({_key(point): point for point in chosen})
    return sorted(selected.values(), key=_key)


def _sample_iterator(iterator, count: int, limit: int = MAX_SERIES_POINTS) -> list[dict]:
    if count <= limit:
        return list(iterator)
    buckets, selected, bucket = max(1, limit // 2), {}, []
    for index, point in enumerate(iterator):
        bucket.append(point)
        if (index + 1) * buckets // count != index * buckets // count or index == count - 1:
            for candidate in (min(bucket, key=lambda p: p["value"]), max(bucket, key=lambda p: p["value"])):
                selected[_dedupe_key(candidate)] = candidate
            bucket = []
    return _cap_extrema(sorted(selected.values(), key=_key), limit)


def merge_uroad(parts: list[dict], can_signals: dict, query: dict, run_id: str) -> dict:
    base = {"schemaVersion": parts[0].get("schemaVersion", "1.0"), "runId": run_id}
    frames, frame_dupes = _merge_records([p.get("frames", []) for p in parts])
    events, event_dupes = _merge_records([p.get("events", []) for p in parts])
    timeline, timeline_dupes = _merge_records([p.get("timeline", []) for p in parts])
    base.update({"frames": frames, "events": events, "timeline": timeline})
    sources = [p.get("source", {}) for p in parts]
    base["source"] = {"manifestPath": "chunked", "manifestSha256": None,
                      "parserVersion": sources[0].get("parserVersion"), "ruleVersion": sources[0].get("ruleVersion"),
                      "files": list(dict.fromkeys(name for source in sources for name in source.get("files", []))), "query": query}
    charts = {"seriesMeta": json.loads(json.dumps(parts[0].get("charts", {}).get("seriesMeta", {})))}
    series_dupes = {}
    for name in ("preCostSeries", "inferCostSeries", "postCostSeries", "e2eCostSeries", "fpsSeries"):
        charts[name], series_dupes[name] = _merge_records([p.get("charts", {}).get(name, []) for p in parts])
        charts[name] = _cap_extrema(charts[name])
    for name in ("skipReasonStats", "errorReasonStats"):
        counts = Counter()
        for part in parts:
            counts.update({item["reason"]: item["count"] for item in part.get("charts", {}).get(name, [])})
        charts[name] = [{"reason": reason, "count": count} for reason, count in counts.items()]
    skip_counts = Counter(event.get("reasonCode") for event in events if event.get("category") == "skip" and event.get("reasonCode"))
    error_counts = Counter(event.get("reasonCode") for event in events if event.get("category") in {"error", "warning"} and event.get("reasonCode"))
    if skip_counts:
        charts["skipReasonStats"] = [{"reason": reason, "count": count} for reason, count in sorted(skip_counts.items())]
    if error_counts:
        charts["errorReasonStats"] = [{"reason": reason, "count": count} for reason, count in sorted(error_counts.items())]
    charts["canSignals"] = can_signals
    base["charts"] = charts
    normal, skipped, errors = (sum(1 for f in frames if f.get("status") == status) for status in ("normal", "skipped", "error"))
    metrics_parts = [p.get("metrics", {}) for p in parts]
    coverage = next((p.get("e2eMs", {}).get("coverage") for p in metrics_parts if p.get("e2eMs", {}).get("coverage") == "overrun_only"),
                    next((p.get("e2eMs", {}).get("coverage") for p in metrics_parts if p.get("e2eMs", {}).get("coverage")), "unavailable"))
    latest_fps_point = max(charts["fpsSeries"], key=_key) if charts["fpsSeries"] else None
    counter_fields = ("edsgTaskStats", "frameDropStats", "lidarValidityStats", "skipReasonStats", "errorReasonStats", "graphOverrunStats")
    metrics = {field: _merge_counter([m.get(field, {}) for m in metrics_parts]) for field in counter_fields}
    for field in ("totalLinesScanned", "edsgCycleCount", "edsgInferenceReportMatches", "frameDropCount", "preTimingCount", "postTimingCount", "skipEventCount", "associatedSkippedCycles", "graphOverrunCount", "chassisGraphOverrunCount"):
        metrics[field] = sum(m.get(field, 0) for m in metrics_parts)
    for field in ("inferenceChainMs", "trajectoryGenerationMs", "graphExecMs", "preTotalMs", "postTotalMs"):
        metrics[field] = _merge_metric([m.get(field, {}) for m in metrics_parts])
    e2e_metric = _merge_metric([m.get("e2eMs", {}) for m in metrics_parts])
    fps_metrics = [m.get("fps", {}) for m in metrics_parts if m.get("fps", {}).get("count")]
    fps_count = sum(item.get("count", 0) for item in fps_metrics)
    metrics.update(eventCount=len(events), frameCount=len(frames), e2eMs={**e2e_metric, "coverage": coverage},
                   fps={"count": fps_count, "latest": (fps_metrics[-1].get("latest") if fps_metrics else latest_fps_point.get("value") if latest_fps_point else None),
                        "min": min((item["min"] for item in fps_metrics), default=None), "max": max((item["max"] for item in fps_metrics), default=None)})
    metrics["skipReasonStats"] = {item["reason"]: item["count"] for item in charts["skipReasonStats"]}
    metrics["errorReasonStats"] = {item["reason"]: item["count"] for item in charts["errorReasonStats"]}
    metrics["skipEventCount"] = sum(metrics["skipReasonStats"].values())
    metrics["associatedSkippedCycles"] = skipped
    base["metrics"] = metrics
    statuses = [p.get("summary", {}).get("runningStatus") for p in parts]
    startup = [p.get("summary", {}).get("startupStatus") for p in parts]
    issues_by_code = {}
    for part in parts:
        for issue in part.get("issues", []):
            code = issue.get("issueCode")
            if code not in issues_by_code:
                issues_by_code[code] = json.loads(json.dumps(issue))
            else:
                merged = issues_by_code[code]
                merged["count"] = merged.get("count", 0) + issue.get("count", 0)
                merged["evidenceRefs"] = list(dict.fromkeys(merged.get("evidenceRefs", []) + issue.get("evidenceRefs", [])))
                if merged["evidenceRefs"]:
                    merged["count"] = len(merged["evidenceRefs"])
                if issue.get("breakdown"):
                    merged["breakdown"] = _merge_counter([merged.get("breakdown", {}), issue["breakdown"]])
    issues = list(issues_by_code.values())
    main_issue = max(issues, key=lambda i: (i.get("severity") == "high", i.get("count", 0))).get("fact") if issues else None
    base["issues"] = issues
    base["summary"] = {"title": "uroad 运行分析结果", "description": f"uroad 已识别 {len(frames)} 个应用处理记录。",
        "runningStatus": "error" if "error" in statuses else "warning" if "warning" in statuses else "healthy" if statuses else "not_started",
        "startupStatus": "success" if "success" in startup else "unknown", "totalFrames": len(frames), "normalFrames": normal,
        "skippedFrames": skipped, "errorFrames": errors, "latestFps": metrics["fps"]["latest"], "avgE2EMs": metrics["e2eMs"]["avg"],
        "avgE2ECoverage": coverage, "skipEventCount": metrics["skipEventCount"], "associatedSkippedCycles": skipped, "mainIssue": main_issue}
    event_stats = {}
    for part in parts:
        for key, stat in part.get("eventStats", {}).items():
            merged = event_stats.setdefault(key, {"count": 0, "firstTimestamp": stat.get("firstTimestamp"), "lastTimestamp": stat.get("lastTimestamp"), "samples": [], "eventIds": []})
            ids = [value for value in stat.get("eventIds", []) if value is not None]
            if ids:
                merged["eventIds"] = list(dict.fromkeys(merged["eventIds"] + ids))
                merged["count"] = len(merged["eventIds"])
            else:
                merged["count"] += stat.get("count", 0)
            timestamps = [v for v in (merged.get("firstTimestamp"), stat.get("firstTimestamp")) if v]
            merged["firstTimestamp"] = min(timestamps) if timestamps else None
            timestamps = [v for v in (merged.get("lastTimestamp"), stat.get("lastTimestamp")) if v]
            merged["lastTimestamp"] = max(timestamps) if timestamps else None
            merged["samples"] = (merged["samples"] + stat.get("samples", []))[:3]
    base["eventStats"] = event_stats
    base["edsgCycles"], _ = _merge_records([p.get("edsgCycles", []) for p in parts])
    nodes = set().union(*(p.get("inferenceNodes", {}) for p in parts))
    base["inferenceNodes"] = {node: _merge_metric([p.get("inferenceNodes", {}).get(node, {}) for p in parts]) for node in sorted(nodes)}
    warnings = list(dict.fromkeys(w for p in parts for w in p.get("completeness", {}).get("warnings", [])))
    base["completeness"] = {"startup": "detected" if any(p.get("completeness", {}).get("startup") == "detected" for p in parts) else "unknown",
        "shutdown": "detected" if any(p.get("completeness", {}).get("shutdown") == "detected" for p in parts) else "unknown",
        "frameCorrelation": "edsg_frame_id" if any(p.get("completeness", {}).get("frameCorrelation") == "edsg_frame_id" for p in parts) else "partial",
        "inferenceTiming": "available" if charts["inferCostSeries"] else "unavailable", "e2eTiming": "partial" if coverage == "overrun_only" else "available" if e2e_metric["count"] else "unavailable",
        "canSignals": "available" if can_signals and all(s.get("series") for s in can_signals.get("signals", {}).values()) else "partial", "warnings": warnings}
    base["summaryFacts"] = [f"识别到 {len(frames)} 个带 frame 的事件记录。", f"识别到 {len(events)} 条 uroad/关联事件。"]
    base["evidence"] = {"sourceFiles": base["source"]["files"], "lineBased": True, "sliceCount": len(parts)}
    base["deduplication"] = {"frames": frame_dupes, "events": event_dupes, "timeline": timeline_dupes, **series_dupes}
    return base


def cleanup_chunk(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(4):
        try:
            temporary.replace(path)
            return
        except PermissionError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 3:
                raise
            time.sleep(0.05 * 2**attempt)


def _full_can_available(can_data: dict) -> bool:
    return all(can_data.get("signals", {}).get(key, {}).get("series") for key in REQUIRED_CAN_SIGNALS)


def retain_original_chunk(chunk: Path, run_dir: Path, index: int) -> list[str]:
    """Move original archives and extracted logs into the task retention area."""
    retained_root = run_dir / "original" / f"{index:04d}"
    retained: list[str] = []
    for name in ("raw", "input"):
        source = chunk / name
        if not source.exists():
            continue
        destination = retained_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError(f"原始数据保留目标已存在: {destination}")
        shutil.move(str(source), str(destination))
        retained.append(str(destination.relative_to(run_dir).as_posix()))
    return retained


def cleanup_completed_chunk(chunk: Path, *, indexed_can: bool) -> None:
    if not indexed_can:
        cleanup_chunk(chunk)
        return
    # The CAN JSON, index and receipts bind exact paths. Keep them in place for
    # the retention period; remove only redundant Uroad outputs after ingestion.
    for relative in ("analysis/analysis.json", "analysis/parse-progress.json", "manifest.json"):
        target = chunk / relative
        if target.resolve().is_relative_to(chunk.resolve()):
            target.unlink(missing_ok=True)


def _parser_progress(chunk: Path, run_dir: Path) -> dict:
    """Read only the current parser's bounded, path-free progress schema."""
    path = chunk / "analysis" / "parse-progress.json"
    result = {"path": path.relative_to(run_dir).as_posix(), "available": False}
    try:
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 65536
                or not path.resolve().is_relative_to(chunk.resolve())):
            return result
        value = json.loads(path.read_text(encoding="utf-8"))
        counters = ("files_read", "lines_read", "cycles_processed", "inference_reports", "matches")
        if (not isinstance(value, dict)
                or set(value) != {"schema", "stage", "elapsed_seconds", *counters}
                or value.get("schema") != "uroad-parse-progress-v1"
                or value.get("stage") not in {"reading_inputs", "indexing_inference", "correlating_inference", "building_result", "completed", "failed"}
                or any(type(value.get(key)) is not int or value[key] < 0 for key in counters)
                or type(value.get("elapsed_seconds")) not in (int, float)
                or not math.isfinite(value["elapsed_seconds"]) or value["elapsed_seconds"] < 0):
            return result
        return {**result, "available": True, "value": value}
    except (OSError, ValueError, TypeError, OverflowError):
        # Progress is supplementary evidence. Its absence must not replace the
        # actual stage timeout, process failure, or request-binding error.
        return result


def _parquet_progress(chunk: Path, run_dir: Path) -> dict:
    """Copy only the finite, identity-free CAN progress into task state."""
    path = chunk / "can-parquet" / "progress.json"
    result = {"path": path.relative_to(run_dir).as_posix(), "available": False}
    try:
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 65536
                or not path.resolve().is_relative_to(chunk.resolve())):
            return result
        from parquet_can_signals import validate_progress
        value = validate_progress(json.loads(path.read_text(encoding="utf-8")))
        return {**result, "available": True, "value": value}
    except (OSError, ValueError, TypeError, OverflowError):
        return result


def process(args, run_dir: Path, run: dict, save, call, scripts: dict) -> None:
    state_lock = getattr(save, "state_lock", None) or RLock()
    original_save = save
    def save(path, value):
        with state_lock:
            original_save(path, value)
    save.state_lock = state_lock
    def now():
        return datetime.now(timezone.utc).isoformat()
    def preserve_failure():
        with state_lock:
            run["failureRetention"] = {"policy": "task_retention", "paths": ["slices", "merge-state"],
                                       "reason": "processing_failed"}
            save(run_dir / "run.json", run)
    chunks = [(args.start, args.end)]
    with state_lock:
        run["chunks"] = {"total": len(chunks), "completed": 0, "active": None, "items": []}
    merge_root = run_dir / "merge-state"
    merge_root.mkdir()
    store = EvidenceStore(merge_root / "evidence.sqlite3")
    slices_root = run_dir / "slices"
    primary_error = None
    retained_indexed_can = False
    selected_env = getattr(args, "resolved_cloud_env", None) or getattr(args, "cloud_env", "prod")
    provider_env = getattr(args, "resolved_provider_environment", None) or selected_env
    try:
        for index, (start, end) in enumerate(chunks, 1):
            chunk = slices_root / f"{index:04d}"
            state = {"index": index, "start": start, "end": end, "source": "uroad+can", "stage": "parallel_join", "status": "running",
                     "environment": selected_env,
                     "lanes": {"uroad": {"stage": "pending", "status": "pending", "stages": []},
                               "can": {"stage": "pending", "status": "pending", "stages": []}}}
            with state_lock:
                run["chunks"]["active"] = state
                save(run_dir / "run.json", run)
            slice_error = None
            try:
                can_path = chunk / "analysis" / "can-signals.json"

                def stage_call(lane_name, stage, command):
                    began = perf_counter()
                    phase = {"stage": stage, "status": "running", "startedAt": now()}
                    with state_lock:
                        lane = state["lanes"][lane_name]
                        lane.setdefault("startedAt", phase["startedAt"])
                        lane.update(stage=stage, status="running")
                        lane["stages"].append(phase)
                        save(run_dir / "run.json", run)
                    error = None
                    try:
                        return call(command)
                    except BaseException as exc:
                        error = exc
                        exc.lane_source = lane_name
                        exc.lane_stage = stage
                        raise
                    finally:
                        with state_lock:
                            phase.update(status="failed" if error is not None else "completed",
                                         endedAt=now(), elapsedSeconds=round(perf_counter() - began, 6))
                            if error is not None:
                                phase["errorType"] = type(error).__name__
                                lane["status"] = "failed"
                            if stage == "uroad_parse":
                                lane["parserProgress"] = _parser_progress(chunk, run_dir)
                            elif stage == "parquet_can":
                                lane["parquetProgress"] = _parquet_progress(chunk, run_dir)
                            save(run_dir / "run.json", run)

                def finish_lane(lane_name, began, error=None):
                    with state_lock:
                        lane = state["lanes"][lane_name]
                        lane.update(status="failed" if error is not None else "completed", endedAt=now(),
                                    elapsedSeconds=round(perf_counter() - began, 6))
                        if error is not None:
                            error.lane_source = lane_name
                            error.lane_stage = lane["stage"]
                        save(run_dir / "run.json", run)

                def uroad_lane():
                    began = perf_counter()
                    try:
                        command = [scripts["python"], scripts["uroad"], "--output-dir", str(chunk), "--run-id", run["runId"], "--vin", args.vin, "--start", start, "--end", end, "--eea-platform", args.eea_platform, "--page-size", str(args.page_size), "--cloud-env", selected_env]
                        if args.max_files is not None:
                            command.extend(["--max-files", str(args.max_files)])
                        stage_call("uroad", "uroad_download", command)
                        manifest = json.loads((chunk / "manifest.json").read_text(encoding="utf-8"))
                        expected = {"cloudEnv": selected_env, "vin": args.vin, "startTime": start, "endTime": end, "logClass": "log_fsdA_service"}
                        if not manifest.get("extracted", {}).get("uroadMatched"):
                            raise RuntimeError("未找到 uroad 数据")
                        wrong = [key for key, value in expected.items() if manifest.get("query", {}).get(key) != value]
                        if manifest.get("query", {}).get("providerEnvironment") != provider_env:
                            wrong.append("providerEnvironment")
                        if wrong:
                            raise RuntimeError(f"uroad manifest 与请求环境不匹配: {', '.join(wrong)}")
                        stage_call("uroad", "uroad_parse", [scripts["python"], scripts["analysis"], "--manifest", str(chunk / "manifest.json"), "--output-dir", str(chunk / "analysis"), "--progress"])
                        analyzed = json.loads((chunk / "analysis" / "analysis.json").read_text(encoding="utf-8"))
                        analyzed.setdefault("source", {}).setdefault("query", manifest.get("query", {}))
                        finish_lane("uroad", began)
                        return analyzed
                    except BaseException as exc:
                        finish_lane("uroad", began, exc)
                        raise

                def can_lane():
                    began = perf_counter()
                    try:
                        parquet_used = False
                        command = [scripts["python"], scripts["can_stream"], "--vin", args.vin,
                                   "--start", start, "--end", end, "--eea-platform", args.eea_platform,
                                   "--page-size", str(args.page_size), "--output", str(can_path),
                                   "--work-dir", str(chunk / "can-work"), "--cloud-env", selected_env]
                        if args.max_files is not None:
                            command.extend(["--max-files", str(args.max_files)])
                        try:
                            stage_call("can", "can_stream", command)
                        except RuntimeError as primary_exc:
                            if "非 zstd 文件" not in str(primary_exc) or not scripts.get("parquet_can"):
                                raise
                            fallback = [scripts["python"], scripts["parquet_can"], "--vin", args.vin,
                                        "--start", start, "--end", end, "--output", str(can_path),
                                        "--cloud-env", selected_env, "--work-dir", str(chunk / "can-parquet"),
                                        "--run-id", run["runId"], "--page-size", str(args.page_size)]
                            if args.max_files is not None:
                                fallback.extend(["--max-files", str(args.max_files)])
                            stage_call("can", "parquet_can", fallback)
                            parquet_used = True
                        if parquet_used:
                            value = load_indexed_contract(
                                can_path, chunk / "can-parquet" / "points.sqlite3",
                                chunk / "can-parquet" / "contract-metadata.json",
                                {"runId": run["runId"], "vin": args.vin, "start": start, "end": end,
                                 "environment": selected_env, "providerEnvironment": provider_env},
                                Path(__file__).resolve().parents[1] / "references" / "can-signals.json")
                            with state_lock:
                                state["lanes"]["can"]["indexedEvidence"] = True
                        else:
                            value = json.loads(can_path.read_text(encoding="utf-8"))
                        if not _full_can_available(value):
                            raise RuntimeError("当前切片没有完整的必需 CAN 信号数据")
                        env = value.get("environment") or {}
                        if env.get("environment") != selected_env or env.get("providerEnvironment") != provider_env:
                            raise RuntimeError("CAN 结果环境与当前报告环境不一致")
                        finish_lane("can", began)
                        return value
                    except BaseException as exc:
                        finish_lane("can", began, exc)
                        raise

                executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"slice-{index}")
                futures = {}
                try:
                    try:
                        futures["uroad"] = executor.submit(uroad_lane)
                        futures["can"] = executor.submit(can_lane)
                    except BaseException:
                        # A failed second submission must not release the task
                        # while its first lane still owns files and save calls.
                        with state_lock:
                            state.update(status="draining", drainingStartedAt=now(),
                                         source="uroad+can", stage="parallel_start")
                            save(run_dir / "run.json", run)
                        for future in futures.values():
                            future.cancel()
                        wait(futures.values())
                        with state_lock:
                            state.update(status="failed", branchesDrained=True, drainedAt=now())
                            for name, future in futures.items():
                                state["lanes"][name]["status"] = "cancelled" if future.cancelled() else "failed" if future.exception() is not None else "completed"
                            save(run_dir / "run.json", run)
                        raise
                    done, pending = wait(futures.values(), return_when=FIRST_EXCEPTION)
                    failures = [future for future in done if not future.cancelled() and future.exception() is not None]
                    if failures:
                        for future in pending:
                            future.cancel()
                        observed = failures[0].exception()
                        with state_lock:
                            state.update(status="draining", drainingStartedAt=now(),
                                         source=getattr(observed, "lane_source", "uroad+can"),
                                         stage=getattr(observed, "lane_stage", "parallel_join"))
                            save(run_dir / "run.json", run)
                        wait(futures.values())
                        failure = next((futures[name].exception() for name in ("uroad", "can")
                                        if not futures[name].cancelled() and futures[name].exception() is not None), failures[0].exception())
                        with state_lock:
                            state.update(source=getattr(failure, "lane_source", "uroad+can"),
                                         stage=getattr(failure, "lane_stage", "parallel_join"), status="failed",
                                         branchesDrained=True, drainedAt=now())
                            for name, future in futures.items():
                                state["lanes"][name]["status"] = "cancelled" if future.cancelled() else "failed" if future.exception() is not None else "completed"
                            save(run_dir / "run.json", run)
                        raise failure
                    wait(futures.values())
                    current_uroad, can_data = futures["uroad"].result(), futures["can"].result()
                    with state_lock:
                        state["lanes"]["uroad"]["status"] = "completed"
                        state["lanes"]["can"]["status"] = "completed"
                        state.update(branchesDrained=True, drainedAt=now())
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                with state_lock:
                    run["stage"] = "merge"
                    state.update(stage="merge", source="uroad+can")
                    save(run_dir / "run.json", run)
                store.add_slice(index, current_uroad, can_data)
                retained = retain_original_chunk(chunk, run_dir, index)
                with state_lock:
                    indexed_can = bool(state["lanes"]["can"].get("indexedEvidence"))
                    if indexed_can:
                        retained_indexed_can = True
                        state["retainedCANEvidence"] = [
                            (chunk / "can-parquet").relative_to(run_dir).as_posix(),
                            can_path.relative_to(run_dir).as_posix()]
                    state["retainedOriginal"] = retained
                    state["status"] = "completed"
                    run["chunks"]["items"].append(deepcopy(state))
                    run["chunks"]["completed"] = index
                    run["chunks"]["active"] = None
                    save(run_dir / "run.json", run)
            except BaseException as exc:
                slice_error = exc
                raise
            finally:
                if slice_error is None:
                    cleanup_completed_chunk(chunk, indexed_can=bool(state["lanes"]["can"].get("indexedEvidence")))
                else:
                    preserve_failure()
        try:
            merged_can = store.finalize_can()
        except RuntimeError:
            last_start, last_end = chunks[-1]
            with state_lock:
                run["chunks"]["active"] = {"index": len(chunks), "start": last_start, "end": last_end, "source": "can", "stage": "can_stream", "status": "failed", "environment": selected_env}
                save(run_dir / "run.json", run)
            raise
        query = {"cloudEnv": selected_env, "providerEnvironment": provider_env, "vin": args.vin, "startTime": args.start, "endTime": args.end, "logClass": "log_fsdA_service"}
        merged = store.finalize_uroad(merged_can, query, run["runId"])
        analysis_dir = run_dir / "analysis"
        analysis_dir.mkdir(exist_ok=True)
        final_paths = (analysis_dir / "can-signals.json", analysis_dir / "analysis.json", run_dir / "manifest.json")
        final_manifest = {"schemaVersion": "1.1", "runId": run["runId"], "query": query, "environment": {"environment": selected_env, "providerEnvironment": provider_env}, "chunks": len(chunks)}
        try:
            atomic_json(final_paths[0], merged_can)
            atomic_json(final_paths[1], merged)
            atomic_json(final_paths[2], final_manifest)
        except Exception:
            for final_path in final_paths:
                final_path.unlink(missing_ok=True)
                final_path.with_suffix(final_path.suffix + ".tmp").unlink(missing_ok=True)
            raise
        store.close()
        cleanup_chunk(merge_root)
    except BaseException as exc:
        primary_error = exc
        preserve_failure()
        raise
    finally:
        try:
            store.close()
        except sqlite3.Error:
            pass
        if primary_error is None and not retained_indexed_can:
            cleanup_chunk(slices_root)
