"""Defence-in-depth guard: uroad must not execute AD/fsdlog requests."""
from __future__ import annotations
import json, os, re

_LATENCY = re.compile(r"(?:时延|AD\s*P99|P99\s*(?:时延|延迟))", re.I)
_FSDLOG = re.compile(r"\bfsdlog\b", re.I)

def reject_non_uroad_request(metadata=None):
    metadata = metadata or {}
    raw = metadata.get("rawMessage") or metadata.get("raw_message") or os.environ.get("CC_CONNECT_RAW_MESSAGE", "")
    route = metadata.get("route") or os.environ.get("CC_CONNECT_ROUTE", "")
    channel = str(metadata.get("channel") or metadata.get("provider") or "").lower()
    if route and route != "uroad-cloud-report":
        raise RuntimeError(f"uroad execution rejected: explicit Skill route is {route}")
    if isinstance(raw, str) and _LATENCY.search(raw):
        raise RuntimeError("uroad execution rejected: latency requests must be handled by Supervisor latency-analysis")

def guard_from_environment():
    raw = os.environ.get("CC_CONNECT_REQUEST_METADATA", "")
    metadata = json.loads(raw) if raw else {}
    reject_non_uroad_request(metadata)
