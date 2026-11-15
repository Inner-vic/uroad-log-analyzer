# 共享任务生命周期

uroad 日志分析不再定义独立的入口调度语义；请求由 Supervisor 创建统一任务，并与 `latency-analysis` 共用全局串行执行槽。uroad 的业务内部 registry 仍保存其运行产物和交付状态，但必须使用 Supervisor task_id 关联父任务。

- 状态由 Supervisor 统一管理：`accepted`、`queued`、`running`、`waiting_external`、`extracting`、`reporting`、`completed`、`failed`、`cancelled`。
- 任务等待云端、文件或交付结果时仍占用执行槽，不与另一个业务任务并行。
- 重试只恢复当前 task，不重新创建任务或重复下载、分析、发送已经确认的资源。
- uroad handler 完成后应调用共享 TaskManager 的 `complete(task_id, ...)`；异常应写入 `failed`。
