# 变更记录

本文件记录可提交应用仓库的版本与结构变化。行为事实仍以对应版本代码、测试和 `VERSION` 为准。

## Unreleased

### Changed

- 将项目定位明确为车辆日志智能分析应用开发工程。
- 建立 `app/` 唯一源码区，避免当前基线、旧开发副本和发布归档混用。
- 重建 README、功能、设计、开发、部署、运维、排障和交接文档。
- 增加统一验证脚本、Git 忽略规则和敏感信息边界。

### Security

- 当前源码区不包含真实 `settings.json`、登录二维码、个人资料、运行数据或旧发布包。

## 2026.09.07.1 — 2026-09-07

- 从 OpenClaw 云端导出 uroad 核心基线。
- 原始归档 SHA-256：`EEB9F1393D7C25D8EA5216286443A4459ADE12C95AC505B909D8BABA7B8E0118`。
- 代码包含任务注册、worker、15 分钟切片、prod/test/auto 环境选择、HTML 校验和交付结果回写。
- 原始归档含真实配置，因此不进入源码仓库；详见 [`docs/release-provenance.md`](docs/release-provenance.md)。

