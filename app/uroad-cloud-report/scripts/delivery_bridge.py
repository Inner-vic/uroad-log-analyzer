#!/usr/bin/env python3
"""Bridge OpenClaw outbound delivery callbacks to the uroad task registry.

This module deliberately does not send messages. The active Feishu outbound
adapter calls ``record_delivery_result`` after its send attempt; retry callers
read the saved report path and invoke that same adapter, never the pipeline.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_registry import mark_delivery, pending_deliveries
from validate_report_artifact import validate_attachment_path


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def delivery_succeeded(result: Any) -> bool:
    """Accept only an explicit successful outbound result."""
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return False
    status = str(result.get("status", "")).lower()
    if status in {"failed", "error", "rejected"}:
        return False
    # Feishu results normally carry a message id; allow explicit ok for tests
    # and future adapters, but never treat an upload/file id alone as sent.
    return bool(result.get("messageId") or result.get("message_id") or result.get("ok") is True)


def record_delivery_result(run_id: str, result: Any) -> dict:
    """Persist a Feishu callback and return a small audit record."""
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if delivery_succeeded(result):
        message_id = result.get("messageId") or result.get("message_id")
        mark_delivery(run_id, "sent", delivered_at=now, error=None, messageId=message_id, messageSentAt=now, uploadCompletedAt=now)
        return {"runId": run_id, "status": "sent", "messageId": message_id}
    error = _text(result.get("error") if isinstance(result, dict) else result) or "outbound delivery returned no success evidence"
    mark_delivery(run_id, "delivery_failed", error=error, failureStage=(result.get("failureStage") if isinstance(result, dict) else None))
    return {"runId": run_id, "status": "failed", "error": error}


def retry_candidates() -> list[dict]:
    """Return existing completed reports only; never enqueue analysis."""
    return pending_deliveries()


def validate_before_send(run_json: str | Path, attachment: str | Path) -> dict:
    """Mandatory adapter hook; callers must use returned report path verbatim."""
    return validate_attachment_path(Path(run_json), Path(attachment))


if __name__ == "__main__":
    os.environ.setdefault("OPENCLAW_WORKSPACE", "/workspace")
    print(json.dumps(retry_candidates(), ensure_ascii=False, indent=2))
