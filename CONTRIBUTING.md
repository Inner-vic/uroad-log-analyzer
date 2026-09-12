# 贡献指南

## 开始之前

1. 阅读 [`AGENTS.md`](AGENTS.md) 和 [`docs/architecture.md`](docs/architecture.md)。
2. 确认要修改的是 `app/` 下的当前代码，不是本地被忽略的历史目录。
3. 不要复制真实配置、VIN、日志或报告作为测试 fixture。

## 开发流程

1. 从受保护主分支创建短生命周期分支。
2. 先补充或调整测试，再修改实现。
3. 运行统一验证：

   ```powershell
   uv run --no-cache --with pytest --with tzdata --with openpyxl==3.1.5 python scripts/verify.py
   ```

4. 更新受影响的功能、设计、部署或运维文档。
5. 检查 `git diff --check`、暂存文件和敏感信息。
6. 通过评审后合并；部署和代码合并是两个独立授权动作。

## 变更要求

- 修改运行状态、保留策略、环境回退或交付成功条件时，必须同时更新测试和对应文档。
- 修改 `vendor/` 时，必须附来源、许可证、支持矩阵、文件清单和目标 Linux 环境验证记录。
- 修复线上问题时记录 taskId/runId，但不得提交含真实标识的运行文件。
- 不把规划目标写成已交付能力；用“已实现、已测试、已部署、已验收”四个层级描述状态。

项目的分支规则、评审人和发布批准角色由维护团队在 [`MAINTAINERS.md`](MAINTAINERS.md) 中补全。

