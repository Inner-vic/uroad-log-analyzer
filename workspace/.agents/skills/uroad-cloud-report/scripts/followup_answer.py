#!/usr/bin/env python3
"""Bind one Uroad follow-up to verified task-scoped data for Codex analysis.

This helper deliberately performs no field-specific parsing or statistics.  It
only resolves identity, ordering, retention, and the exact local data scope;
the calling Codex session performs the requested analysis.
"""
from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

from ccconnect_request_context import RequestContextError, followup_identity_from_context
from followup_context import RegistryError, claim_runnable_followup, enqueue_followup, resolve_followup_task


def prepare(question: str, context_header: str, job_id: str | None = None) -> dict:
    identity = followup_identity_from_context(context_header)
    resolved_job_id = job_id or resolve_followup_task(**identity, question=question)
    accepted = enqueue_followup(resolved_job_id, question=question, **identity)
    if accepted.get("status") != "running":
        return {"jobId": resolved_job_id, "accepted": accepted, "status": "queued"}
    claimed = claim_runnable_followup(resolved_job_id, f"interactive-{uuid4().hex}")
    if not claimed:
        return {"jobId": resolved_job_id, "accepted": accepted, "status": "processing"}
    entry, data = claimed["entry"], claimed["dataContext"]
    if entry.get("questionId") != accepted.get("questionId"):
        return {"jobId": resolved_job_id, "accepted": accepted, "status": "queued"}
    return {
        "jobId": resolved_job_id,
        "accepted": accepted,
        "status": "ready_for_codex",
        "questionId": entry["questionId"],
        "question": entry["question"],
        "dataVersion": entry.get("dataVersion", data.get("version", 1)),
        "paths": data.get("paths") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id")
    parser.add_argument("--question", required=True)
    parser.add_argument("--ccconnect-context", required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.question, args.ccconnect_context, args.job_id)
    except (OSError, RegistryError, RequestContextError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())