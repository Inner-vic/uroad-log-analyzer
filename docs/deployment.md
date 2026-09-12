# 部署指南

## 1. 目标环境

uroad 主应用的离线发布物支持：

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux x86-64、glibc / manylinux_2_17 兼容 |
| Python | CPython 3.9～3.14 |
| 内存 | 8 GB 基线 |
| 工作区 | 默认 `/workspace`，必须可写 |
| Skill 路径 | `/workspace/skills/uroad-cloud-report` |
| 输出路径 | `/workspace/outputs/uroad-report-runs` |

ARM64、Alpine/musl、PyPy、Windows 生产运行或范围外 Python 不受当前离线包支持。

## 2. 发布内容

从 `app/uroad-cloud-report/` 发布以下内容：`SKILL.md`、`VERSION`、`scripts/`、`references/`、`vendor/`、`requirements.txt` 和 `settings.json.example`。

不得包含真实 `settings.json`、运行数据、报告、缓存、测试临时目录或仓库中的其他应用单元。

## 3. 部署步骤

1. 记录当前线上 Skill 版本、路径和可回滚制品哈希。
2. 从受审查的 commit/tag 生成唯一发布包，并记录 SHA-256。
3. 在隔离目录解压并核对发布清单，禁止直接覆盖真实配置。
4. 将代码部署到 `/workspace/skills/uroad-cloud-report`。
5. 从安全配置源恢复 `settings.json`，并注入 `UROAD_PROD_ORIGIN`、`UROAD_TEST_ORIGIN` 及对应下载域名白名单；这些值不得写入发布包。
6. 确认 `/workspace/outputs/uroad-report-runs` 可写且不位于 Skill 目录中。
7. 在目标主机验证离线运行时：

   ```bash
   cd /workspace
   OPENCLAW_WORKSPACE=/workspace \
   python /workspace/skills/uroad-cloud-report/scripts/verify_offline_zstandard.py
   ```

8. 执行本地测试、短时间窗真实冒烟、HTML 校验和附件真发送。
9. 核对机器人实际加载的 `skillPath`、`skillVersion` 与本次发布一致。
10. 记录验收 taskId/runId、结果和负责人，不提交运行文件。

## 4. 真实冒烟

```bash
cd /workspace
OPENCLAW_WORKSPACE=/workspace \
python /workspace/skills/uroad-cloud-report/scripts/run_uroad_pipeline.py \
  --vin <AUTHORIZED_TEST_VIN> \
  --start "YYYY-MM-DD HH:MM:SS" \
  --end "YYYY-MM-DD HH:MM:SS" \
  --cloud-env auto
```

成功标准不是“生成了文件”，而是流水线成功、artifact 校验通过、附件消息得到明确回执，且用户能下载并离线打开报告。

## 5. 回滚

1. 停止接收新的重分析任务。
2. 确认当前 worker/pipeline 状态，避免同时运行两个版本。
3. 恢复上一份已验证代码，不覆盖 `settings.json` 和 `outputs/`。
4. 重做离线运行时、短时间窗和附件交付验证。
5. 记录回滚原因、影响任务和后续修复计划。

Supervisor 与 AD P99 必须作为独立组件发布。没有真实 E2E 记录时，不得随 uroad 主应用默认启用。
