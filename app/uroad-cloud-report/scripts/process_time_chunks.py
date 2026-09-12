#!/usr/bin/env python3
"""Memory-bounded helpers for sequential fifteen-minute uroad/CAN processing."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import Counter
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CHUNK_SECONDS = 900
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
        """)

    def _insert(self, kind: str, item: dict) -> None:
        stable = json.dumps(("can", format(float(item.get("epochMs")), ".6f"), item.get("raw")) if kind.startswith("can:") and item.get("epochMs") is not None else _dedupe_key(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor = self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)", (kind, stable, _key(item)[0], json.dumps(item, ensure_ascii=False, separators=(",", ":"))))
        if cursor.rowcount == 0:
            self.db.execute("INSERT INTO dedup VALUES(?,1) ON CONFLICT(kind) DO UPDATE SET count=count+1", (kind,))

    def add_slice(self, index: int, analysis: dict, can_data: dict) -> None:
        contract = json.loads(json.dumps(analysis))
        with self.db:
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


def time_chunks(start: str, end: str) -> list[tuple[str, str]]:
    first, last = datetime.strptime(start, TIME_FORMAT), datetime.strptime(end, TIME_FORMAT)
    chunks, cursor = [], first
    while cursor <= last:
        chunk_end = min(last, cursor + timedelta(seconds=CHUNK_SECONDS - 1))
        chunks.append((cursor.strftime(TIME_FORMAT), chunk_end.strftime(TIME_FORMAT)))
        cursor = chunk_end + timedelta(seconds=1)
    return chunks


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
    temporary.replace(path)


def _full_can_available(can_data: dict) -> bool:
    return all(can_data.get("signals", {}).get(key, {}).get("series") for key in REQUIRED_CAN_SIGNALS)


def process(args, run_dir: Path, run: dict, save, call, scripts: dict) -> None:
    chunks = time_chunks(args.start, args.end)
    run["chunks"] = {"total": len(chunks), "completed": 0, "active": None, "items": []}
    merge_root = run_dir / "merge-state"
    merge_root.mkdir()
    store = EvidenceStore(merge_root / "evidence.sqlite3")
    slices_root = run_dir / "slices"
    primary_error = None
    selected_env = getattr(args, "resolved_cloud_env", None) or getattr(args, "cloud_env", "prod")
    provider_env = getattr(args, "resolved_provider_environment", None) or selected_env
    try:
        for index, (start, end) in enumerate(chunks, 1):
            chunk = slices_root / f"{index:04d}"
            state = {"index": index, "start": start, "end": end, "source": "uroad+can", "stage": "parallel_join", "status": "running",
                     "environment": selected_env,
                     "lanes": {"uroad": {"stage": "acquire_analyze", "status": "running"},
                               "can": {"stage": "acquire_stream", "status": "running"}}}
            run["chunks"]["active"] = state
            save(run_dir / "run.json", run)
            slice_error = None
            try:
                can_path = chunk / "analysis" / "can-signals.json"

                def uroad_lane():
                    try:
                        command = [scripts["python"], scripts["uroad"], "--output-dir", str(chunk), "--run-id", run["runId"], "--vin", args.vin, "--start", start, "--end", end, "--eea-platform", args.eea_platform, "--page-size", str(args.page_size), "--cloud-env", selected_env]
                        if args.max_files is not None:
                            command.extend(["--max-files", str(args.max_files)])
                        call(command)
                        manifest = json.loads((chunk / "manifest.json").read_text(encoding="utf-8"))
                        expected = {"cloudEnv": selected_env, "vin": args.vin, "startTime": start, "endTime": end, "logClass": "log_fsdA_service"}
                        if not manifest.get("extracted", {}).get("uroadMatched"):
                            raise RuntimeError("未找到 uroad 数据")
                        wrong = [key for key, value in expected.items() if manifest.get("query", {}).get(key) != value]
                        if manifest.get("query", {}).get("providerEnvironment") != provider_env:
                            wrong.append("providerEnvironment")
                        if wrong:
                            raise RuntimeError(f"uroad manifest 与请求环境不匹配: {', '.join(wrong)}")
                        call([scripts["python"], scripts["analysis"], "--manifest", str(chunk / "manifest.json"), "--output-dir", str(chunk / "analysis")])
                        analyzed = json.loads((chunk / "analysis" / "analysis.json").read_text(encoding="utf-8"))
                        analyzed.setdefault("source", {}).setdefault("query", manifest.get("query", {}))
                        return analyzed
                    except BaseException as exc:
                        exc.lane_source = "uroad"
                        exc.lane_stage = "uroad_acquire_analyze"
                        raise
                    finally:
                        for source_dir in (chunk / "raw", chunk / "input"):
                            cleanup_chunk(source_dir)

                def can_lane():
                    try:
                        command = [scripts["python"], scripts["can_stream"], "--vin", args.vin,
                                   "--start", start, "--end", end, "--eea-platform", args.eea_platform,
                                   "--page-size", str(args.page_size), "--output", str(can_path),
                                   "--work-dir", str(chunk / "can-work"), "--cloud-env", selected_env]
                        if args.max_files is not None:
                            command.extend(["--max-files", str(args.max_files)])
                        call(command)
                        value = json.loads(can_path.read_text(encoding="utf-8"))
                        if not _full_can_available(value):
                            raise RuntimeError("当前切片没有完整的必需 CAN 信号数据")
                        env = value.get("environment") or {}
                        if env.get("environment") != selected_env or env.get("providerEnvironment") != provider_env:
                            raise RuntimeError("CAN 结果环境与当前报告环境不一致")
                        return value
                    except BaseException as exc:
                        exc.lane_source = "can"
                        exc.lane_stage = "can_stream"
                        raise

                executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"slice-{index}")
                futures = {"uroad": executor.submit(uroad_lane), "can": executor.submit(can_lane)}
                try:
                    done, pending = wait(futures.values(), return_when=FIRST_EXCEPTION)
                    failures = [future for future in done if not future.cancelled() and future.exception() is not None]
                    if failures:
                        for future in pending:
                            future.cancel()
                        wait(futures.values())
                        failure = next((futures[name].exception() for name in ("uroad", "can")
                                        if not futures[name].cancelled() and futures[name].exception() is not None), failures[0].exception())
                        state.update(source=getattr(failure, "lane_source", "uroad+can"),
                                     stage=getattr(failure, "lane_stage", "parallel_join"), status="failed")
                        for name, future in futures.items():
                            state["lanes"][name]["status"] = "cancelled" if future.cancelled() else "failed" if future.exception() is not None else "completed"
                        save(run_dir / "run.json", run)
                        raise failure
                    wait(futures.values())
                    current_uroad, can_data = futures["uroad"].result(), futures["can"].result()
                    state["lanes"]["uroad"]["status"] = "completed"
                    state["lanes"]["can"]["status"] = "completed"
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                store.add_slice(index, current_uroad, can_data)
                state["status"] = "completed"
                run["chunks"]["items"].append(dict(state))
                run["chunks"]["completed"] = index
                run["chunks"]["active"] = None
                save(run_dir / "run.json", run)
            except (Exception, KeyboardInterrupt) as exc:
                slice_error = exc
                raise
            finally:
                try:
                    cleanup_chunk(chunk)
                except OSError as cleanup_exc:
                    if slice_error is not None:
                        slice_error.cleanup_errors = getattr(slice_error, "cleanup_errors", []) + [f"slice {index}: {cleanup_exc}"]
                    else:
                        raise
        try:
            merged_can = store.finalize_can()
        except RuntimeError:
            last_start, last_end = chunks[-1]
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
    except (Exception, KeyboardInterrupt) as exc:
        primary_error = exc
        raise
    finally:
        try:
            store.close()
        except sqlite3.Error:
            pass
        try:
            cleanup_chunk(slices_root)
        except OSError as cleanup_exc:
            if primary_error is not None:
                primary_error.cleanup_errors = getattr(primary_error, "cleanup_errors", []) + [f"slices: {cleanup_exc}"]
            else:
                raise
