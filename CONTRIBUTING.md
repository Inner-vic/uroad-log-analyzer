# 提交与维护

维护本项目时只修改其源码、版本、测试和文档，不跨项目改写走查、AD P99 或它们的任务状态。共享输入解析组件的变更需分别回归并明确同步，不以目录整体覆盖其他部署。

修改前阅读 workspace/AGENTS.md、Skill 和相应 references。来源、环境、时间边界、去重、完整证据、任务归属、保留期和送达凭据不可降级。新依赖需同时审查 requirements 和入口固定白名单；不得从用户正文推导安装内容。

运行 `tools/run_tests.py`，更新 `tools/update_manifests.py`，再运行 `tools/check_repository.py` 并审查 Git diff。原生产字节发生变更时需更新版本和来源说明，不只重算清单掩盖改动。清单用于完整性校验，不替代代码审查或签名。

不提交真实 settings、token、VIN、群/消息标识、下载 URL、原始日志、任务记录和报告。派生 CAN 解析的第三方 MIT 声明保留在 `references/third-party-attribution.md`，该声明不自动把整个项目授予同一许可证。
