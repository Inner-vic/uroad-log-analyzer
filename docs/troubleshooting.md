# 故障排查

先确定失败阶段，再行动。不要因为用户没有收到附件就默认整条分析失败。

| 现象 | 优先检查 | 安全处理 |
|---|---|---|
| 启动即拒绝工作区 | `OPENCLAW_WORKSPACE`、当前目录、Skill 路径 | 切到工作区根目录并显式设置变量；不要从源码目录运行 |
| `Asia/Shanghai` 不存在 | Windows 本地测试是否缺 `tzdata` | 使用统一 uv 验证命令；生产 Linux 检查系统时区数据 |
| 离线 zstandard 校验失败 | Python/ABI/平台、manifest、许可证和文件哈希 | 停止业务请求，用原始受控制品重新部署；不得绕过校验 |
| prod 无数据后没有回退 | prod 是否真的“请求成功且目标数据为空/不完整” | 认证、网络、5xx、结构错误不允许回退；修复系统错误 |
| uroad 有数据但无报告 | CAN 信号、切片完整性、解析阶段 | 记录缺失为 partial/system error；不得拼接 test 数据伪造完整报告 |
| 执行工具超时 | 原会话、pipeline 进程、`.pipeline-active.lock`、task registry | 继续等待或恢复原任务；确认结束前不要重复启动 |
| 任务长期 claimed/running | 更新时间、执行锁、worker 锁、进程 | 按当前代码的 stale 规则评估；残留锁不明时升级处理 |
| 报告生成但用户未收到 | artifact 校验、上传、消息发送、delivery 回执 | 只重试发送；没有 messageId/明确 ok 不得标记 sent |
| 用户收到 JSON | 实际发送入口是否绕过 artifact validator | 立即停止该入口，核查泄露范围，只允许发送合法 HTML |
| 磁盘持续增长 | 原始文件、失败 run、诊断证据、注册表大小 | 先 dry-run；活动或待交付任务不得清理 |
| AD P99 缺结果 | URL 角色、表格 revision、访问身份、外部 `lark-cli` | 保持只读；不要自动登录或把单 URL 当成新任务 |

## 建议诊断顺序

1. 记录 taskId/runId、用户可见现象和发生时间。
2. 读取任务注册表中的 execution、delivery、notification 状态。
3. 读取对应 `run.json` 的当前阶段和安全错误分类。
4. 核对进程与锁，不做重复启动。
5. 核对 artifact 的 canonical path、大小和哈希。
6. 只重试失败阶段。
7. 记录处置、验证结果和是否需要代码修复。

诊断输出对外分享前必须脱敏；不要将原始文件提交到 Git 或公开 Issue。

