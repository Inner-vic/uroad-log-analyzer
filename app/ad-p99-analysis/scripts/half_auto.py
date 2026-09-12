"""Human-initiated, two-card AD P99 comparison workflow for OpenClaw."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ad_p99 import (
    TIME_RANGE_PATTERN,
    InputError,
    checked_url,
    compare_results,
    inspect_card,
    parse_comparison_request,
    parse_time_token,
    read_local,
)

STATE_VERSION = 1
KEY_PATTERN = re.compile(r"[0-9a-f]{16}")
ATTACHMENT_KEY_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])", re.IGNORECASE)
SHEET_PATH_PATTERN = re.compile(r"/(?:sheets|spreadsheets|wiki)/[A-Za-z0-9_-]+/?$")
URL_PATTERN = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)
ROLE_MARKERS = {
    "baseline": ("baseline", "基线"),
    "replacement": ("替换后", "刷包后", "替换小包", "小包后"),
}


def state_path(state_dir, comparison_key):
    if not isinstance(comparison_key, str) or KEY_PATTERN.fullmatch(comparison_key) is None:
        raise InputError("comparison_key 格式无效。")
    root = Path(state_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{comparison_key}.json").resolve()
    if path.parent != root:
        raise InputError("状态路径无效。")
    return path


def write_json(path, value, *, create=False):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if create:
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(payload)
        except FileExistsError:
            raise InputError("该 comparison_key 已存在；请复用已有任务，不要重复创建。") from None
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def load_job(state_dir, comparison_key):
    path = state_path(state_dir, comparison_key)
    try:
        value = json.loads(read_local(path).decode("utf-8-sig"))
    except FileNotFoundError:
        raise InputError("未找到该 comparison_key 的对比任务。") from None
    except json.JSONDecodeError:
        raise InputError("对比任务状态损坏，不能继续处理。") from None
    if not isinstance(value, dict) or value.get("state_version") != STATE_VERSION:
        raise InputError("对比任务状态版本不受支持。")
    if value.get("comparison_key") != comparison_key:
        raise InputError("对比任务状态与 comparison_key 不匹配。")
    return path, value


def asgard_time(value):
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        raise InputError("对比任务时间格式无效。") from None


def asgard_command(metadata, platform):
    if not isinstance(platform, str) or re.fullmatch(r"[A-Za-z0-9._-]+", platform.strip()) is None:
        raise InputError("阿斯加德平台参数只能包含字母、数字、点、下划线和连字符。")
    return "fsdlog {vin}, {start}, {end}, {platform}".format(
        vin=metadata["vin"], start=asgard_time(metadata["start"]),
        end=asgard_time(metadata["end"]), platform=platform.strip(),
    )


def create_job(request_text, *, state_dir, asgard_platform):
    request = parse_comparison_request(request_text)
    return create_job_from_request(request, state_dir=state_dir, asgard_platform=asgard_platform)


def create_job_from_request(request, *, state_dir, asgard_platform=None):
    baseline = request["baseline_metadata"]
    replacement = request["replacement_metadata"]
    job = {
        "state_version": STATE_VERSION,
        "kind": "ad_p99_half_auto_job",
        "comparison_key": request["comparison_key"],
        "status": "waiting_cards",
        "entry_mode": "asgard_cards" if asgard_platform is not None else "manual_urls",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vin": request["vin"],
        "baseline_metadata": baseline,
        "replacement_metadata": replacement,
        "asgard_platform": asgard_platform,
        "asgard_commands": ({
            "baseline": asgard_command(baseline, asgard_platform),
            "replacement": asgard_command(replacement, asgard_platform),
        } if asgard_platform is not None else {}),
        "received": {"baseline": None, "replacement": None},
    }
    path = state_path(state_dir, job["comparison_key"])
    write_json(path, job, create=True)
    return public_job(job)


def create_manual_url_job(request_text, *, state_dir):
    """Create a comparison state for users who paste Sheets URLs themselves."""
    return create_job_from_request(parse_comparison_request(request_text), state_dir=state_dir)


def public_job(job):
    """Return only state useful to the user-facing workflow, never raw card content."""
    status = "waiting_urls" if job.get("entry_mode") == "manual_urls" and job["status"] == "waiting_cards" else job["status"]
    response = {
        "schema_version": 1,
        "kind": job["kind"],
        "comparison_key": job["comparison_key"],
        "status": status,
        "vin": job["vin"],
        "baseline": {"metadata": job["baseline_metadata"], "received": job["received"]["baseline"] is not None},
        "replacement": {"metadata": job["replacement_metadata"], "received": job["received"]["replacement"] is not None},
    }
    if job.get("entry_mode") == "manual_urls":
        response["next_action"] = (
            "请在一条新的 @ AD P99 机器人消息中粘贴缺少的最终 Sheets URL，并写 "
            f"Baseline 或 替换后及 #{job['comparison_key']}。"
        )
        return response
    response["baseline"]["asgard_command"] = job["asgard_commands"]["baseline"]
    response["replacement"]["asgard_command"] = job["asgard_commands"]["replacement"]
    response.update({
        "asgard_submission_order": [
            {
                "role": "baseline",
                "command": job["asgard_commands"]["baseline"],
                "send_as": "一条独立聊天消息",
                "before_next": "等待阿斯加德返回 Baseline 结果卡片。",
            },
            {
                "role": "replacement",
                "command": job["asgard_commands"]["replacement"],
                "send_as": "另一条独立聊天消息",
                "before_next": "等待阿斯加德返回替换后结果卡片。",
            },
        ],
        "next_action": (
            "阿斯加德每次只处理一条 fsdlog：先单独发送 Baseline 指令并等待结果卡片，再单独发送 "
            "替换后指令。将两张结果卡片或最终 Sheets 链接以 Baseline、替换后标记回复给 AD P99 机器人。"
        ),
    })
    return response


def infer_role(label):
    if not isinstance(label, str) or not label.strip():
        raise InputError("请用 Baseline 或 替换后 标记这张结果卡片。")
    text = label.casefold()
    matches = [role for role, markers in ROLE_MARKERS.items() if any(marker.casefold() in text for marker in markers)]
    if len(matches) != 1:
        raise InputError("卡片标记必须唯一指向 Baseline 或 替换后。")
    return matches[0]


def parse_attachment_context(text):
    """Read the explicit role and comparison key from one incoming reply event."""
    if not isinstance(text, str) or not text.strip():
        raise InputError("请在卡片回复中写明 comparison_key 和 Baseline 或 替换后。")
    keys = {match.group(0).casefold() for match in ATTACHMENT_KEY_PATTERN.finditer(text)}
    if len(keys) != 1:
        raise InputError("卡片回复必须含且只含一个 16 位 comparison_key。")
    return {"comparison_key": keys.pop(), "role": infer_role(text)}


def card_time_range(document):
    """Extract exactly one visible analysis window from an incoming Asgard card."""
    ranges = set()

    def scan(value):
        if isinstance(value, str):
            for match in TIME_RANGE_PATTERN.finditer(value):
                start_at, _ = parse_time_token(match.group("start"))
                end_at, _ = parse_time_token(match.group("end"))
                ranges.add((start_at, end_at))
            # Interactive-card payloads may be JSON encoded inside a message content string.
            if value.lstrip().startswith(("{", "[")):
                try:
                    scan(json.loads(value))
                except (ValueError, RecursionError):
                    pass
        elif isinstance(value, dict):
            for item in value.values():
                scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    scan(document)
    if len(ranges) != 1:
        raise InputError("卡片中没有唯一的分析时间段；请附上 comparison_key 和 Baseline 或 替换后标记。")
    return ranges.pop()


def role_from_card_time(job, document):
    card_start, card_end = card_time_range(document)
    matches = []
    for role in ("baseline", "replacement"):
        metadata = job[f"{role}_metadata"]
        start_at, _ = parse_time_token(metadata["start"])
        end_at, _ = parse_time_token(metadata["end"])
        if (start_at, end_at) == (card_start, card_end):
            matches.append(role)
    if len(matches) != 1:
        raise InputError("卡片时间段与该对比任务不匹配；请核对 VIN/时段，或使用正确的 comparison_key。")
    return matches[0]


def discover_card_target(state_dir, card_document):
    """Find one unfilled job slot by card time; state lookup never scans chat history."""
    card_start, card_end = card_time_range(card_document)
    root = Path(state_dir).resolve()
    if not root.is_dir():
        raise InputError("状态目录不存在，无法匹配卡片。")
    matches = []
    for path in root.glob("*.json"):
        if KEY_PATTERN.fullmatch(path.stem) is None:
            continue
        try:
            _, job = load_job(root, path.stem)
        except InputError:
            continue
        if job.get("status") == "completed":
            continue
        for role in ("baseline", "replacement"):
            metadata = job[f"{role}_metadata"]
            start_at, _ = parse_time_token(metadata["start"])
            end_at, _ = parse_time_token(metadata["end"])
            if (start_at, end_at) == (card_start, card_end) and job["received"].get(role) is None:
                matches.append({"comparison_key": job["comparison_key"], "role": role})
    if len(matches) != 1:
        raise InputError("卡片无法唯一匹配待处理对比任务；请在回复中附上 comparison_key 和角色标记。")
    return matches[0]


def validated_sheet_url(url):
    if not isinstance(url, str):
        raise InputError("结果卡片没有提供在线表格链接。")
    checked_url(url)
    path = url.split("?", 1)[0]
    if SHEET_PATH_PATTERN.fullmatch(re.sub(r"^https?://[^/]+", "", path)) is None:
        raise InputError("查看结果链接不是可读取的飞书 Sheets 或 Wiki 表格链接。")
    return url


def sheet_urls_from_text(text):
    """Extract distinct final Sheets URLs from copied chat text, with or without ?sheet=<id>."""
    if not isinstance(text, str):
        raise InputError("URL 输入必须是文本。")
    found = []
    for match in URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip("，。；、;,.）)]}】》")
        try:
            candidate = validated_sheet_url(candidate)
        except InputError:
            continue
        if candidate not in found:
            found.append(candidate)
    if not found:
        raise InputError("未找到可读取的飞书 Sheets 链接。")
    return found


def labeled_sheet_urls(text):
    """Read manual URLs from one message; each URL must have an explicit role on its line."""
    found = []
    for line in text.splitlines():
        try:
            urls = sheet_urls_from_text(line)
        except InputError:
            continue
        if not urls:
            continue
        if len(urls) != 1:
            raise InputError("每行只能提供一个最终 Sheets 链接，并注明 Baseline 或 替换后。")
        found.append({"role": infer_role(line), "sheet_url": urls[0]})
    if not found:
        raise InputError("未找到可读取的飞书 Sheets 链接。")
    if len({item["role"] for item in found}) != len(found):
        raise InputError("同一条消息中同一角色只能提供一个 Sheets 链接。")
    return found


def sheet_url_from_card(document):
    inspected = inspect_card(document)
    candidates = []
    for button in inspected["result_buttons"]:
        for url in button["urls"]:
            try:
                value = validated_sheet_url(url)
            except InputError:
                continue
            if value not in candidates:
                candidates.append(value)
    if len(candidates) != 1:
        raise InputError("卡片中没有唯一的可读取 Sheets 链接；请回复对应的最终 Sheets 链接。")
    return candidates[0]


def attach_result(state_dir, comparison_key, *, role, sheet_url=None, card_document=None, label=None):
    if (sheet_url is None) == (card_document is None):
        raise InputError("每次只能提供一张卡片或一个最终 Sheets 链接。")
    path, job = load_job(state_dir, comparison_key)
    label_role = infer_role(label) if label else None
    card_role = None
    if card_document is not None:
        try:
            card_role = role_from_card_time(job, card_document)
        except InputError as exc:
            # A forwarded card may retain its button but omit the visible time range.
            # Explicit user role labels remain the approved fallback in that case.
            if label_role is None or not str(exc).startswith("卡片中没有唯一"):
                raise
    candidates = {item for item in (label_role, card_role) if item is not None}
    if role == "auto":
        if len(candidates) != 1:
            raise InputError("无法从卡片时间或回复标记唯一确定角色。")
        role = candidates.pop()
    if role not in ("baseline", "replacement"):
        raise InputError("角色必须是 baseline、replacement 或 auto。")
    if candidates and candidates != {role}:
        raise InputError("回复标记与卡片时间段指向的角色不一致。")
    url = validated_sheet_url(sheet_url) if sheet_url is not None else sheet_url_from_card(card_document)
    if job["status"] == "completed":
        raise InputError("该对比任务已完成；请创建新任务进行新的比较。")
    if job["received"].get(role) is not None:
        raise InputError(f"{role} 已收到结果，拒绝用后到卡片覆盖。")
    other_role = "replacement" if role == "baseline" else "baseline"
    other = job["received"].get(other_role)
    if other and other.get("sheet_url") == url:
        raise InputError("Baseline 与替换后不能使用同一个 Sheets 链接。")
    job["received"][role] = {"sheet_url": url, "received_at": datetime.now(timezone.utc).isoformat()}
    job["status"] = "ready_to_analyze" if all(job["received"].values()) else "waiting_cards"
    write_json(path, job)
    return public_job(job)


def attach_card_automatically(state_dir, card_document):
    """Attach one incoming card in any order when its visible time maps to one pending job."""
    target = discover_card_target(state_dir, card_document)
    return attach_result(state_dir, target["comparison_key"], role=target["role"], card_document=card_document)


def ingest_card_event(state_dir, card_document, *, identity, executable=None, read_fn=None):
    """Process one current card event and run the comparison when it completes a pair."""
    job = attach_card_automatically(state_dir, card_document)
    response = {
        "schema_version": 1,
        "kind": "ad_p99_card_ingest",
        "comparison_key": job["comparison_key"],
        "status": "waiting_urls" if job.get("entry_mode") == "manual_urls" and job["status"] == "waiting_cards" else job["status"],
        "received": {
            "baseline": job["baseline"]["received"],
            "replacement": job["replacement"]["received"],
        },
    }
    if job["status"] == "ready_to_analyze":
        response["comparison"] = run_job(state_dir, job["comparison_key"], identity=identity,
                                           executable=executable, read_fn=read_fn)
        response["status"] = "completed"
    return response


def ingest_manual_url_message(state_dir, text, *, identity, executable=None, read_fn=None):
    """Handle one user @ message containing one or two labelled Sheets URLs."""
    urls = sheet_urls_from_text(text)
    try:
        request = parse_comparison_request(text)
    except InputError:
        request = None
    if request is not None:
        attachments = labeled_sheet_urls(text)
        job = create_job_from_request(request, state_dir=state_dir)
        key = job["comparison_key"]
    else:
        try:
            context = parse_attachment_context(text)
        except InputError:
            context = None
        if context is None:
            if len(urls) != 2:
                raise InputError("请同时提供 VIN、两个时段和一个带角色 URL，或直接提供两个 Sheets URL 进行快捷对比。")
            try:
                labelled = labeled_sheet_urls(text)
            except InputError:
                labelled = []
            if len(labelled) == 2:
                by_role = {item["role"]: item["sheet_url"] for item in labelled}
                if len(by_role) != 2:
                    raise InputError("两个 URL 的角色标签重复；请使用 Baseline 和 替换后。")
                urls = [by_role["baseline"], by_role["replacement"]]
            return quick_compare_urls(urls, identity=identity, executable=executable, read_fn=read_fn)
        key = context["comparison_key"]
        attachments = labeled_sheet_urls(text)
        if len(attachments) != 1 or attachments[0]["role"] != context["role"]:
            raise InputError("补充 URL 时只提供一个链接，且其角色必须与 comparison_key 标记一致。")
    for item in attachments:
        job = attach_result(state_dir, key, role=item["role"], sheet_url=item["sheet_url"])
    response = {
        "schema_version": 1,
        "kind": "ad_p99_manual_url_ingest",
        "comparison_key": key,
        "status": "waiting_urls" if job.get("entry_mode") == "manual_urls" and job["status"] == "waiting_cards" else job["status"],
        "received": {
            "baseline": job["baseline"]["received"],
            "replacement": job["replacement"]["received"],
        },
    }
    if job["status"] == "ready_to_analyze":
        response["comparison"] = run_job(state_dir, key, identity=identity,
                                           executable=executable, read_fn=read_fn)
        response["status"] = "completed"
    return response


def quick_compare_urls(urls, *, identity, executable=None, read_fn=None):
    """Compare two user-designated result tables when VIN/time context was not supplied."""
    if not isinstance(urls, list) or len(urls) != 2 or urls[0] == urls[1]:
        raise InputError("快捷对比需要两个不同的 Sheets URL。")
    if identity not in ("bot", "user"):
        raise InputError("在线读取必须指定 identity 为 bot 或 user。")
    if read_fn is None:
        from lark_source import read_online
        read_fn = read_online
    placeholder = {"vin": "用户未提供", "start": "用户未提供", "end": "用户未提供"}
    baseline = read_fn(urls[0], identity=identity, executable=executable, metadata=placeholder)
    replacement = read_fn(urls[1], identity=identity, executable=executable, metadata=placeholder)
    result = compare_results(baseline, replacement,
                             baseline_metadata=placeholder, replacement_metadata=placeholder)
    result["vin"] = None
    result["baseline"]["time_range"] = None
    result["replacement"]["time_range"] = None
    result["context_verification"] = {
        "status": "user_unverified",
        "message": "未提供 VIN 和时段；按用户给定的 URL 顺序计算 URL 2 − URL 1。",
    }
    return result


def run_job(state_dir, comparison_key, *, identity, executable=None, read_fn=None):
    path, job = load_job(state_dir, comparison_key)
    if not all(job["received"].values()):
        raise InputError("两张结果尚未齐全，不能开始对比。")
    if identity not in ("bot", "user"):
        raise InputError("在线读取必须指定 identity 为 bot 或 user。")
    if read_fn is None:
        from lark_source import read_online
        read_fn = read_online
    baseline = read_fn(job["received"]["baseline"]["sheet_url"], identity=identity,
                       executable=executable, metadata=job["baseline_metadata"])
    replacement = read_fn(job["received"]["replacement"]["sheet_url"], identity=identity,
                          executable=executable, metadata=job["replacement_metadata"])
    result = compare_results(baseline, replacement,
                             baseline_metadata=job["baseline_metadata"],
                             replacement_metadata=job["replacement_metadata"])
    job["status"] = "completed"
    job["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(path, job)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="AD P99 半自动双卡片对比；不发送阿斯加德消息。")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="解析用户输入并创建对比任务")
    create.add_argument("--request", type=Path, required=True, help="VIN 与两个时间段的复制文本")
    create.add_argument("--state-dir", type=Path, required=True, help="公司批准的任务状态目录")
    create.add_argument("--asgard-platform", required=True, help="阿斯加德指令中的平台参数，如 perf-m100-ultra")
    attach = commands.add_parser("attach", help="接收带角色标记的一张结果卡片或最终 Sheets 链接")
    attach.add_argument("--comparison-key", required=True)
    attach.add_argument("--state-dir", type=Path, required=True)
    attach.add_argument("--role", choices=("baseline", "replacement", "auto"), required=True)
    attach.add_argument("--label", help="--role auto 时的用户文字，如 Baseline 或 替换后")
    source = attach.add_mutually_exclusive_group(required=True)
    source.add_argument("--sheet-url")
    source.add_argument("--card-json", type=Path, help="仅用于由获准事件处理器暂存的卡片 JSON")
    run = commands.add_parser("run", help="两张结果齐全后读取在线表格并计算对比")
    run.add_argument("--comparison-key", required=True)
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--identity", choices=("bot", "user"), required=True)
    run.add_argument("--lark-cli")
    resolve = commands.add_parser("resolve-card", help="按卡片时间匹配一条待处理对比任务，不读取聊天历史")
    resolve.add_argument("--state-dir", type=Path, required=True)
    resolve.add_argument("--card-json", type=Path, required=True)
    ingest = commands.add_parser("ingest-card", help="处理当前卡片；配对完整时自动运行对比")
    ingest.add_argument("--state-dir", type=Path, required=True)
    ingest.add_argument("--card-json", type=Path, required=True)
    ingest.add_argument("--identity", choices=("bot", "user"), required=True)
    ingest.add_argument("--lark-cli")
    manual = commands.add_parser("ingest-urls", help="处理当前消息中的手动 Sheets URL；完整时自动对比")
    manual.add_argument("--state-dir", type=Path, required=True)
    manual.add_argument("--message", type=Path, required=True, help="包含请求上下文和带角色 URL 的当前消息")
    manual.add_argument("--identity", choices=("bot", "user"), required=True)
    manual.add_argument("--lark-cli")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_job(read_local(args.request).decode("utf-8-sig"), state_dir=args.state_dir,
                                asgard_platform=args.asgard_platform)
            code = 0
        elif args.command == "attach":
            card = json.loads(read_local(args.card_json).decode("utf-8-sig")) if args.card_json else None
            result = attach_result(args.state_dir, args.comparison_key, role=args.role,
                                   sheet_url=args.sheet_url, card_document=card, label=args.label)
            code = 0
        elif args.command == "resolve-card":
            result = discover_card_target(args.state_dir,
                                          json.loads(read_local(args.card_json).decode("utf-8-sig")))
            code = 0
        elif args.command == "ingest-card":
            result = ingest_card_event(args.state_dir,
                                       json.loads(read_local(args.card_json).decode("utf-8-sig")),
                                       identity=args.identity, executable=args.lark_cli)
            code = {"completed": 0, "waiting_cards": 0}[result["status"]]
        elif args.command == "ingest-urls":
            result = ingest_manual_url_message(args.state_dir, read_local(args.message).decode("utf-8-sig"),
                                                identity=args.identity, executable=args.lark_cli)
            code = {"completed": 0, "waiting_urls": 0, "ok": 0, "partial": 2, "failed": 1}[result["status"]]
        else:
            result = run_job(args.state_dir, args.comparison_key, identity=args.identity,
                             executable=args.lark_cli)
            code = {"ok": 0, "partial": 2, "failed": 1}[result["status"]]
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return code
    except (InputError, OSError, ValueError, json.JSONDecodeError) as exc:
        message = str(exc) if isinstance(exc, InputError) else f"半自动对比失败（{type(exc).__name__}）。"
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
