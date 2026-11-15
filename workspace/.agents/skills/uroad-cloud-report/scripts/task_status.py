"""Read-only, source-bound Uroad progress; never enqueue an analysis question."""
import argparse
import json
import re
from ccconnect_request_context import followup_identity_from_context
from followup_context import _authorize,_find_task
from task_registry import RegistryError,registry_path
from delivery_bridge import _valid_native_receipt


def status(context_header,job_id=None):
    identity=followup_identity_from_context(context_header)
    try:payload=json.loads(registry_path().read_text(encoding='utf-8'))
    except (OSError,ValueError):raise RegistryError('暂时无法读取任务状态，请稍后按原任务编号查询。') from None
    if not isinstance(payload,dict) or not isinstance(payload.get('tasks'),list):
        raise RegistryError('任务状态记录异常，未创建新的分析。')
    if job_id:
        if re.fullmatch(r'run-[A-Za-z0-9-]+',job_id) is None:raise RegistryError('任务编号格式无效。')
        task=_find_task(payload,job_id)
    else:
        thread=identity['thread_id']
        if not thread:raise RegistryError('请携带目标任务编号，或在原报告话题中查询；不会按最近任务猜测。')
        matches=[t for t in payload['tasks'] if isinstance(t,dict)
            and (t.get('context') or {}).get('requesterId')==identity['requester_id']
            and (t.get('context') or {}).get('chatId')==identity['chat_id']
            and (t.get('context') or {}).get('threadId')==thread]
        if len(matches)!=1:raise RegistryError('原话题中没有唯一对应任务，请携带目标任务编号。')
        task=matches[0]
    _authorize(task,requester_id=identity['requester_id'],chat_id=identity['chat_id'],thread_id=identity['thread_id'])
    execution=task.get('execution') or {};delivery=task.get('delivery') or {};context=task.get('context') or {}
    state=execution.get('status') or task.get('status') or 'unknown'
    source={'requesterId':context.get('requesterId'),'chatId':context.get('chatId'),
            'messageId':context.get('messageId'),'threadId':context.get('threadId') or ''}
    receipt=delivery.get('receipt')
    platform_id=delivery.get('messageId')
    delivered=bool(state=='completed' and delivery.get('status')=='sent' and (
        _valid_native_receipt(receipt) and receipt.get('runId')==task['runId'] and receipt.get('source')==source
        and receipt.get('payloadKind')=='report' or isinstance(platform_id,str)
        and re.fullmatch(r'om_[A-Za-z0-9_]+',platform_id) and platform_id!=source['messageId']))
    messages={'queued':'任务正在排队，尚未开始解析。','claimed':'执行服务已领取任务，正在准备处理。',
        'running':'正在处理日志和 CAN 数据，报告尚未生成。','no_data':'指定时段未查到生成标准报告所需的数据。',
        'partial_data':'本次数据不完整，未生成标准报告。','system_error':'日志分析未完成，请基于原任务诊断继续排查。',
        'cancelled':'任务已取消。'}
    if state=='completed':
        message='标准报告已发送至原会话。' if delivered else (
            '标准报告已生成，但发送失败；报告已保留，可单独恢复交付。' if delivery.get('status')=='delivery_failed'
            else '标准报告已生成，正在等待交付确认。')
    else:message=messages.get(state,'任务状态尚未明确，请按原任务编号核查。')
    value={'jobId':task['runId'],'status':state,'message':message,'delivered':delivered}
    if state=='queued' and type(task.get('queuePosition')) is int:value['queuePosition']=task['queuePosition']
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ccconnect-context',required=True);parser.add_argument('--job-id')
    args=parser.parse_args()
    try:print(json.dumps(status(args.ccconnect_context,args.job_id),ensure_ascii=False));return 0
    except (ValueError,RegistryError) as exc:print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
