# 系统设计

## 1. 设计原则

Uroad Log Analyzer 采用“平台交互 + 持久化任务 + 管道式分析”的应用结构。模型负责识别意图和生成用户可读回复；代码负责输入校验、状态、数据、报告和成功判定。

```text
Feishu / OpenClaw event
        │
        ▼
request normalization ──► task registry / deduplication
        │                         │
        │                         ▼
        └──────────────────► single task worker
                                  │
                                  ▼
                         15-minute sequential slices
                           ┌──────┴──────┐
                           ▼             ▼
                     uroad channel    CAN channel
                           └──────┬──────┘
                                  ▼
                         evidence merge / SQLite
                                  ▼
                          self-contained HTML
                                  ▼
                        artifact validation gate
                                  ▼
                         attachment + delivery ack
```

这张结构图是代码路径的摘要，不代表平台侧未入库的执行器已经实现。

## 2. 代码单元

| 单元 | 关键文件 | 职责 |
|---|---|---|
| 请求与任务 | `run_uroad_task.py`、`task_registry.py`、`task_worker.py` | 时间规范化、请求上下文、注册、查重、领取与恢复 |
| 主流水线 | `run_uroad_pipeline.py` | 配置预检、环境选择、执行锁、阶段编排和最终状态 |
| 切片处理 | `process_time_chunks.py` | 时间切片、双通道并发、失败收敛和证据合并 |
| uroad | `cheyun_uroad_adapter.py`、`parse_uroad.py` | 查询、下载和日志语义提取 |
| CAN | `cheyun_prod_client.py`、`stream_can_signals.py` | 获取、流式解压、白名单信号提取 |
| 报告 | `build_html_report.py`、`references/report-template.html` | 生成自包含交互报告 |
| 交付 | `validate_report_artifact.py`、`delivery_bridge.py` | 路径门禁、附件候选和发送结果回写 |
| 路由 | `app/supervisor/scripts/openclaw_skill_router.py` | 将消息确定性分类到一个业务路由 |
| AD P99 | `app/ad-p99-analysis/scripts/` | 只读表格解析和差异计算 |

## 3. 状态与权威边界

任务注册表管理排队和跨阶段状态；单次 `run.json` 管理该次流水线的阶段、错误和 artifact 元数据。群聊历史、模型记忆和文件是否存在都不能替代这两个状态源。

```text
queued → claimed → running → completed | no_data | system_error | cancelled
                           └────────────► delivery.pending → sent | failed
```

当前实现保留旧状态兼容，维护时不要在多个文件中分别推进同一状态。报告已完成但交付失败时，分析状态仍应保持完成。

## 4. 并发与资源

- `.pipeline-active.lock` 限制同一工作区只有一条重分析流水线；
- `.task-worker.lock` 限制 worker 单实例；
- 时间切片之间串行，同一切片只有 uroad/CAN 两条并发通道；
- 子进程默认超时 900 秒；
- CAN 数据逐文件处理并清理压缩源，不生成全量 ASC；
- 状态查询和报告补发不应占用重分析槽位。

## 5. 信任边界

| 边界 | 不可信输入 | 控制 |
|---|---|---|
| 用户 → OpenClaw | VIN、时间、URL、自然语言 | 格式校验、路由互斥、请求上下文隔离 |
| OpenClaw → 车云 | 环境、接口响应、下载地址 | 环境一致性、状态码/结构检查、host 白名单 |
| 压缩文件 → 解析器 | 文件名、路径、内容和大小 | 流式处理、路径限制、失败清理 |
| 分析 → HTML | 日志字段和文本 | 白名单数据、HTML 转义、自包含模板 |
| HTML → 飞书 | 本地路径与发送结果 | canonical path 门禁、类型校验、messageId 回执 |

## 6. 已知设计债务

- `task-registry.json` 仍需要更强的原子写入和长期裁剪策略；
- 残留锁的自动恢复需要在真实异常终止场景验证；
- Supervisor 只有路由分类器，没有生产执行器和持久化；
- AD P99 的 `comparison_key` 并发写入缺少进程锁和 TTL 清理；
- 平台侧真实附件上传、消息发送和最终通知入口不在当前仓库中。

这些事项属于待实现或待验证能力，不得在 README 或发布说明中描述为完成。
