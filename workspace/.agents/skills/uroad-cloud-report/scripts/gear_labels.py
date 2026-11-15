"""Render gear codes without confusing raw enums with physical transmission gears."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _definition() -> dict:
    config = Path(__file__).resolve().parents[1] / "references" / "can-signals.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    return next(item for item in payload.get("signals", []) if item.get("key") == "gear")


def gear_display(raw: object, label: object = None, confidence: object = None) -> str:
    raw_text = str(raw if raw is not None else "").strip()
    definition = _definition()
    semantic = str(label or definition.get("enum", {}).get(raw_text) or "").strip()
    confidence_text = str(confidence or definition.get("confidence") or "unknown")
    if semantic and confidence_text == "validated":
        return f"{semantic}（原始编码 {raw_text}）"
    if raw_text and raw_text.lstrip("-").isdigit():
        return f"原始编码 {raw_text}（语义未确认）"
    return raw_text or "不可用"
