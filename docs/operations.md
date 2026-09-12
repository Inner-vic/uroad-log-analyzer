# 运维手册

## 1. 运行资产

默认工作区：`/workspace`。关键位置：

```text
/workspace/skills/uroad-cloud-report/          应用代码和本地配置
/workspace/outputs/uroad-report-runs/          任务注册表与运行目录
  task-registry.json
  .pipeline-active.lock
  .task-worker.lock
  <run-id>/run.json
  <run-id>/analysis/
  <run-id>/report/
```

## 2. 每日巡检

| 检查 | 正常信号 | 异常动作 |
|---|---|---|
| 磁盘 | 输出目录有足够余量，原始下载物未长期滞留 | 找到占用最大的 run，按状态与期限处理，不手工通配删除 |
| 任务 | queued/claimed/running 持续推进 | 核对锁、进程、阶段和更新时间；确认原进程前不得重启同请求 |
| 接口 | prod/test 请求返回结构有效 | 区分认证、网络、5xx、无数据；系统错误不得当作无数据回退 |
| 报告 | HTML 存在且校验通过 | 查看 `run.json` 的生成阶段和 artifact 路径 |
| 交付 | `delivery.status=sent` 且有消息回执 | 只重试交付，不重跑分析 |
| 清理 | 运行中和待交付任务受保护 | 先 dry-run，核对目标工作区和受保护状态 |

## 3. 保留策略

当前 `2026.09.07.1` 代码口径：

- 成功运行的诊断 JSON：24 小时；
- 成功 HTML 与 `run.json`：7 天或最近 20 份；
- 失败/中断运行：3 天；
- 待交付/待通知保护：最多 75 分钟、最多 5 次尝试；
- 原始下载物：成功后默认清理。

任何数字变更都必须先改代码和测试，再同步本文；历史文档中的 48 小时方案不属于当前事实。

清理前先执行：

```bash
cd /workspace
OPENCLAW_WORKSPACE=/workspace \
python /workspace/skills/uroad-cloud-report/scripts/retention_cleanup.py \
  --workspace /workspace \
  --dry-run
```

确认输出只指向 `/workspace/outputs/uroad-report-runs` 后，再按脚本帮助执行正式清理。不要编写临时通配删除命令。

## 4. 任务处置原则

- 执行工具超时不代表 pipeline 已结束；先检查原会话、进程、锁和任务状态。
- 相同 VIN/时间窗请求先查询可复用报告，不立即重跑。
- `no_data` 仅在约 120 秒内防抖，之后允许重新查询。
- 报告已完成但附件失败：调用补发候选，只重试附件。
- OOM、主机重启或强制终止后可能有中间文件；先确认状态和锁，再决定恢复或清理。

## 5. 备份与恢复

至少保留：当前及上一版本发布包与 SHA、配置恢复方式、任务注册表备份策略、最近一次脱敏验收记录。真实配置应由秘密管理系统恢复，不从 Git 或个人聊天记录恢复。

恢复演练至少每季度一次：在隔离工作区部署上一版本、恢复非生产配置、验证离线运行时、运行合成测试并确认能够回滚。

## 6. 升级条件

下列情况立即升级给对应负责人：凭据泄露、跨用户报告串联、错误环境取数、报告发送了内部 JSON、任务注册表损坏、重复重任务造成资源风险、无法确认当前线上版本。

联系人和替补角色维护在 [`MAINTAINERS.md`](../MAINTAINERS.md)。

