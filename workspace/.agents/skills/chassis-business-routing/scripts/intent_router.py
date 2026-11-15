#!/usr/bin/env python3
"""Side-effect-free natural-language router for chassis business requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
from natural_input import normalize_input, normalize_query_input
from semantic_input import apply_interpretation, apply_local_range_interpretation, has_unbound_clocks
UROAD_SCRIPTS = SCRIPT_ROOT.parents[1] / "uroad-cloud-report" / "scripts"
AD_SCRIPTS = SCRIPT_ROOT.parents[1] / "ad-p99-analysis" / "scripts"
sys.path[:0] = [str(UROAD_SCRIPTS), str(AD_SCRIPTS)]
from parse_time_input import BEIJING, parse_time_range
from ad_p99 import InputError, parse_comparison_request, TIME_RANGE_PATTERN

VIN = re.compile(r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])", re.I)
SHEETS = re.compile(r"https://[^\s<>\"'，；。！？]+\.feishu\.cn/sheets/[A-Za-z0-9]+(?:\?[^\s<>\"'，；。！？]*)?", re.I)
UROAD_INTENT = re.compile(r"(?:日志|\blog\b|uroad|云端日志)", re.I)
LATENCY_INTENT = re.compile(r"(?:时延|延迟|AD\s*P99|P99\s*(?:时延|延迟))", re.I)
WALKTHROUGH_NEW_INTENT = re.compile(r"走查测试\s*new(?![A-Za-z0-9_])", re.I)


def _execution_text(text: str) -> tuple[str, str | None]:
    """Select intent without editing the original input passed to parsers.

    Mentioning a workflow while discussing its implementation is not an
    instruction to create a vehicle analysis. This gate deliberately does not
    infer missing business inputs or bind a follow-up to any historical task.
    """
    names = r"(?:走查测试\s*new(?![A-Za-z0-9_])|走查测试|时延分析|时延对比|延迟分析|日志分析|日志排查|Uroad|AD\s*P99)"
    if re.search(r"(?:不要|暂不|无需|不用|禁止).{0,6}(?:创建任务|启动任务|执行任务|真正执行|实际执行|取数|下载|发消息|查询日志)", text):
        return "", "用户要求只检查或讨论，不执行取数任务。"
    # Suppress each explicitly declined workflow, allowing an affirmative
    # request for another workflow in the same message to retain its route.
    selected = re.sub(
        rf"(?:不要|不用|无需|别|不是|不做|不执行|不启动|不提交)\s*(?:(?:执行|启动|运行|进行|做|提交)\s*)?{names}",
        "", text, flags=re.I)
    if selected != text and not re.search(names, selected, re.I):
        return "", "用户未要求启动业务任务。"
    execution_evidence = re.sub(
        rf"(?:没有|尚未|还没|仍未|未能|没能|未|没)\s*(?:启动|提交|执行|运行|发起)\s*{names}",
        "", selected, flags=re.I)
    execute = re.search(rf"(?:重新执行|重新运行|重新提交|重跑|启动|执行|运行|提交|发起|做一次|做个|进行)\s*(?:一次|本次|这次|新的)?\s*{names}(?!时|的(?:逻辑|规则|流程|实现))", execution_evidence, re.I)
    repository_work = re.search(
        r'(?:上传|推送|提交).{0,32}(?:GitLab|GitHub|代码仓库)|'
        r'(?:GitLab|GitHub).{0,24}(?:上传|仓库|推送)|'
        r'(?:打包|导出|整理).{0,16}(?:源码|代码|部署包)', selected, re.I)
    if repository_work and not execute:
        return "", "这是代码仓库交付或整理，不创建车辆分析任务。"
    system_subject = re.search(r"(?:skill|路由|触发条件|误触发|处理逻辑|执行逻辑|链路|接口|解析器|解析耗时|代码|功能|部署|方案|规则|实现)", selected, re.I)
    maintenance = re.search(r"(?:梳理|检查|排查|修复|优化|修改|变更|设计|实现|开发|重构|完善|审查|审计|评审|讨论)", selected)
    if re.search(rf"在进行\s*{names}时", selected, re.I) and re.search(r"(?:应该|应当|默认|明白吗)", selected):
        return "", "这是业务规则说明，不创建分析任务。"
    if system_subject and maintenance and not execute:
        return "", "这是业务链路维护或方案讨论，不创建分析任务。"
    if not execute and re.search(r"(?:失败原因|历史请求|测试用例|复现输入|原始请求|以下.{0,4}示例|例如[:：]|举例[:：])", selected):
        return "", "这是历史请求或示例说明，不创建分析任务。"
    # A failure description may contain “启动时延分析”; that substring is
    # reported state, not an affirmative command to start a new analysis.
    reported_failure = re.search(r"(?:任务|报告).{0,8}(?:失效|未启动|未提交)|(?:没有|尚未|还没|仍未)\s*(?:启动|提交|执行).{0,16}(?:走查|时延|延迟|日志|AD\s*P99)", selected, re.I)
    if reported_failure and not execute:
        return "", "这是已有任务的失败或未启动反馈，不自动创建新任务。"
    if not execute and re.search(r"(?:任务|报告).{0,8}(?:尚未|还没|仍未|未生成|失败|进度|进展)|(?:当前|刚才|本次|这次).{0,12}(?:进度|进展|任务编号)|(?:取消|停止|监控|查询进度|查看进度).{0,8}(?:任务|分析|走查)", selected):
        return "", "这是已有任务的状态、控制或追问，不自动创建新任务。"
    consult = re.search(
        rf"(?:怎么用|如何使用|使用说明|有什么区别|有什么问题|为什么|为啥|原因是什么|进度如何|到哪一步|是否完成|什么时候完成)|"
        rf"(?:介绍|解释|说明|讲解).{{0,16}}{names}|"
        rf"(?:本次|这次|刚才|之前|上次).{{0,16}}(?:失败|超时|尚未|仍在|未完成|未生成|报错)", selected, re.I)
    # A new, complete request may include an additional analysis question.
    # Keep that case on the standard-report-first route; question-only/status
    # requests must not become new jobs merely because they quote old inputs.
    direct = re.match(rf"\s*(?:@\S+\s*)?(?:请|帮我|请帮我)?\s*{names}(?:\s|[:：，,])", selected, re.I)
    complete_request = bool(direct and VIN.search(selected) and re.search(r"20\d{2}[-年/.]\d", selected))
    if not execute and consult and not complete_request:
        return "", "这是业务咨询、状态或追问，不自动创建新任务。"
    return selected, None


def _urls(text: str) -> list[str]:
    result: list[str] = []
    for match in SHEETS.finditer(text):
        value = match.group(0).rstrip("，,。.;；:：)）]】")
        if value not in result:
            result.append(value)
    return result


AD_SAME_DAY_RANGE = re.compile(
    r"(?P<date>20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}(?:日)?)\s*"
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*(?:到|至|~|～|-|—)\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)"
)


def _canonical_date(value: str) -> str:
    match = re.fullmatch(r"(?P<year>20\d{2})[-年/.](?P<month>\d{1,2})[-月/.](?P<day>\d{1,2})(?:日)?", value)
    if not match:
        return value
    return f"{int(match.group('year')):04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"


def _expand_ad_same_day_ranges(text: str) -> str:
    """Make the date explicit at both ends for AD's two-window parser."""
    def replace(match: re.Match[str]) -> str:
        date = _canonical_date(match.group("date"))
        return f"{date} {match.group('start')} - {date} {match.group('end')}"
    return AD_SAME_DAY_RANGE.sub(replace, text)


def _decision(route: str, status: str, *, message: str, missing: list[str] | None = None, normalized: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"route": route, "status": status, "message": message, "missing": missing or [], "normalized": normalized or {}}


def _audit(original, normalized, canonical=None):
    return {"original_text": original, "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "canonical_text": canonical or normalized["text"], "received_at": normalized["received_at"],
            "changes": normalized["changes"],
            **({"interpretation": normalized["interpretation"]} if "interpretation" in normalized else {})}


def route(text: str, received_at: datetime | None = None, *, interpretation=None) -> dict[str, Any]:
    original = text or ""
    text = original.strip()
    if not text:
        return _decision("none", "no_match", message="消息为空。")
    received = received_at or datetime.now(BEIJING)
    if received.tzinfo is None:
        received = received.replace(tzinfo=BEIJING)
    received = received.astimezone(BEIJING)
    intent_text, nonexecution = _execution_text(text)
    if nonexecution:
        decision = _decision("none", "no_match", message=nonexecution)
        decision["normalization"] = _audit(original, {"text": original, "received_at": received.isoformat(), "changes": []})
        return decision
    if re.search(r"走查测试\s*new[A-Za-z0-9_]", intent_text, re.I) and not WALKTHROUGH_NEW_INTENT.search(intent_text):
        return _decision("none", "no_match", message="走查入口名称不明确；不会退回其他走查链路。")
    if WALKTHROUGH_NEW_INTENT.search(intent_text):
        return _decision('none', 'no_match', message='走查测试New已暂时停用，未创建任务；请使用原“走查测试”入口。')
    if "走查测试" in intent_text:
        # Keep the standalone parser in its own process: both skills contain
        # similarly named modules, but must never share an import namespace.
        entry = SCRIPT_ROOT.parents[1] / "walkthrough-test" / "scripts" / "walkthrough_route.py"
        try:
            command = [sys.executable, "-X", "utf8", str(entry), "--text", original,
                       "--received-at", received.isoformat()]
            if interpretation is not None:
                command.extend(["--interpretation", json.dumps(interpretation, ensure_ascii=False)])
            result = subprocess.run(
                command,
                capture_output=True, text=True, encoding="utf-8", check=True,
                timeout=15, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            decision = json.loads(result.stdout)
            if (not isinstance(decision, dict) or decision.get('route') not in ('none', 'walkthrough-test')
                    or decision.get('status') not in ('ready', 'needs_input', 'no_match')
                    or not isinstance(decision.get('message'), str)
                    or not isinstance(decision.get('normalized', {}), dict)
                    or not isinstance(decision.get('missing', []), list)):
                raise ValueError('invalid walkthrough route decision')
            decision.setdefault("missing", [])
            decision.setdefault("normalized", {})
            return decision
        except (OSError, subprocess.SubprocessError, ValueError):
            return _decision("walkthrough-test", "unavailable", message="走查测试独立入口不可用，请检查部署；不会切换到其他业务。")
    explicit_log = re.search(r"(?:日志分析|日志排查|uroad)", intent_text, re.I)
    explicit_latency = re.search(r"(?:时延分析|时延对比|延迟分析|AD\s*P99|P99\s*(?:时延|延迟))", intent_text, re.I)
    log_head = re.match(r"\s*(?:@\S+\s*)?(?:请帮我|帮我|请)?\s*(?:(?:执行|运行|提交|发起)\s*)?(?:日志分析|日志排查|uroad)(?:\s|[:：，,])", intent_text, re.I)
    first_vin = VIN.search(intent_text)
    primary_log = bool(log_head and first_vin and (not explicit_latency or first_vin.start() < explicit_latency.start()))
    if LATENCY_INTENT.search(intent_text) and not primary_log and (not explicit_log or explicit_latency):
        temporal = (apply_local_range_interpretation(original, interpretation, received) if interpretation is not None
                    else normalize_query_input(original, received))
        audit = _audit(original, temporal)
        if temporal["needs_confirmation"]:
            decision = _decision("ad-p99", "needs_input", message=temporal["message"], missing=temporal["missing"])
            decision["normalization"] = audit
            return decision
        text = temporal["text"]
        urls = _urls(text)
        if len(urls) not in (0, 2):
            return _decision("ad-p99", "needs_input", message="时延对比请提供两份最终 Sheets，或不提供链接而让系统向阿斯加德发起双时段分析。", missing=["第二份 Sheets URL，或删除不完整的 URL"])
        try:
            expanded = _expand_ad_same_day_ranges(text)
            request = parse_comparison_request(expanded)
            if has_unbound_clocks(expanded, list(TIME_RANGE_PATTERN.finditer(expanded))):
                raise InputError("还识别到时段之外的时刻或精度，请明确两段准确的起止时间，不能自动忽略候选时刻。")
        except InputError as exc:
            decision = _decision("ad-p99", "needs_input", message=str(exc), missing=["同一 VIN 的两个不重叠时间段"])
            decision["normalization"] = audit
            return decision
        baseline = request["baseline_metadata"]
        replacement = request["replacement_metadata"]
        request["canonical_text"] = (
            f"时延分析 {request['vin']}，Baseline：{baseline['start']} - {baseline['end']}，"
            f"替换后：{replacement['start']} - {replacement['end']}"
        )
        request["normalization"] = _audit(original, temporal, request["canonical_text"])
        if not urls:
            return _decision("ad-p99-asgard", "ready", message="已识别双时段时延分析；将提交阿斯加德并在完成后回传报告。", normalized=request)
        identity = None
        bot_identity = bool(re.search(r"(?:机器人|bot)\s*身份", text, re.I))
        user_identity = bool(re.search(r"(?:用户|user)\s*身份", text, re.I))
        if bot_identity and user_identity:
            return _decision("ad-p99", "needs_input", message="表格读取身份同时指定了机器人和用户，请明确使用一种身份。", missing=["唯一的 Sheets 读取身份（bot 或 user）"])
        if bot_identity:
            identity = "bot"
        elif user_identity:
            identity = "user"
        if identity is None:
            return _decision("ad-p99", "needs_input", message="已识别两份结果表；请说明使用“机器人身份”还是“用户身份”读取表格。", missing=["Sheets 读取身份（bot 或 user）"])
        # URLs and time windows both retain source order.  Bind both via the
        # same roles so reversed chronological input cannot mix a URL with the
        # other window's metadata; explicit labels still override defaults.
        urls_by_role = dict(zip(request["input_roles"], urls))
        baseline_url = urls_by_role["baseline"]
        replacement_url = urls_by_role["replacement"]
        normalized = {
            **request,
            "baseline_sheet_url": baseline_url,
            "replacement_sheet_url": replacement_url,
            "identity": identity,
        }
        return _decision("ad-p99", "ready", message="已确定 Baseline/替换后角色，可执行 AD P99 对比。", normalized=normalized)
    if not UROAD_INTENT.search(intent_text):
        return _decision("none", "no_match", message="未命中日志分析或时延分析意图。")
    vins = sorted({item.upper() for item in VIN.findall(text)})
    if not vins:
        return _decision("uroad", "needs_input", message="已识别日志分析；请补充 17 位 VIN。", missing=["17 位 VIN"])
    if len(vins) > 1:
        return _decision("uroad", "needs_input", message="消息中包含多个 VIN，无法确定要分析哪一辆车。", missing=["唯一的 17 位 VIN"])
    try:
        parsed = parse_time_range(original, received, interpretation=interpretation)
    except ValueError as exc:
        temporal = (apply_local_range_interpretation(original, interpretation, received) if interpretation is not None
                    else normalize_query_input(original, received))
        decision = _decision("uroad", "needs_input", message=str(exc), missing=temporal["missing"] or ["完整起止时间，或起始时刻加持续时间/事件前后窗口"])
        decision["normalization"] = _audit(original, temporal)
        return decision
    return _decision("uroad", "ready", message="已识别并规范化日志分析参数。", normalized={"vin": vins[0], "start": parsed.start, "end": parsed.end, "raw_time_expression": parsed.original, "time_notice": parsed.notice, "received_at": parsed.received_at, "normalization": getattr(parsed, "normalization", None)})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True)
    parser.add_argument("--received-at", help="ISO 8601 message receipt time; defaults to current Beijing time")
    parser.add_argument("--interpretation", type=json.loads, help="AI time-span proposal JSON; always validated against original text")
    args = parser.parse_args()
    received = None
    if args.received_at:
        try:
            received = datetime.fromisoformat(args.received_at)
        except ValueError as exc:
            parser.error(f"--received-at 格式无效: {exc}")
    print(json.dumps(route(args.text, received, interpretation=args.interpretation), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
