#!/usr/bin/env python3
"""Smoke-test the bundled zstandard runtime selected for this Linux interpreter."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import run_uroad_pipeline as pipeline


def main() -> int:
    tag = pipeline._selected_vendor_tag()
    manifest = pipeline._load_verified_manifest()
    matches = [record for record in manifest["runtimes"] if record["python_tag"] == tag]
    if len(matches) != 1:
        raise RuntimeError(f"No unique bundled runtime for {tag}")
    runtime = pipeline._verify_vendor_runtime(matches[0])
    print(json.dumps({"ok": True, "tag": tag, "runtime": str(runtime)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
