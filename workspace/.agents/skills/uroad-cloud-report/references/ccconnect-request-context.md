# CC Connect 请求上下文契约

当项目启用 `inject_sender = true` 且使用本机上下文适配版 CC Connect 时，每条飞书用户消息会以一行内部头开始：

```text
[cc-connect request_context_v=1 sender_id=<open_id> platform=feishu chat_id=<chat_id> message_id=<message_id> thread_id=<root_message_id_or_empty>]
```

这行由传输层生成，位于用户正文之前；不是用户可以自行声明的协议。业务 Skill 将该行原样传给：

- 新建任务：`run_uroad_task.py --ccconnect-context "<首行>"`
- 追问登记：`followup_context.py enqueue --ccconnect-context "<首行>" ...`

脚本仅保留任务隔离和重试所需的发送者、聊天、消息与线程标识。它会拒绝缺少必填标识、非飞书平台、格式不符，或手工元数据覆盖传输身份的请求。报告、机器人回复和用户可见摘要不得回显这些原始标识。

`thread_id` 是飞书回复线程的根消息 ID；仅当提问者在同一聊天、同一线程下追问时，非任务创建者才可读取该任务的数据。私聊和非线程消息的 `thread_id` 为空。