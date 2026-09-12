# Uroad Log Analyzer 文档中心

本文档集面向应用开发、部署运维和项目交接。当前事实优先级如下：

1. `app/*/VERSION`、当前代码和自动化测试；
2. 各应用单元的 `SKILL.md` 与 `references/` 行为契约；
3. 本目录中的设计与操作文档；
4. 版本来源和历史记录。

规划文档不能证明功能已经实现，单元测试也不能证明线上平台已经完成闭环验收。

| 场景 | 文档 | 回答的问题 |
|---|---|---|
| 功能与研发对齐 | [功能说明](functional-spec.md) | 应用做什么、不做什么、完成到什么程度 |
| 代码设计 | [系统设计](architecture.md) | 模块如何协作、数据和状态由谁负责 |
| 本地开发 | [开发指南](development.md) | 如何搭环境、运行测试和安全修改 |
| 发布上线 | [部署指南](deployment.md) | 部署什么、如何验证、如何回滚 |
| 日常值守 | [运维手册](operations.md) | 看什么指标、怎么清理、如何恢复 |
| 故障处理 | [故障排查](troubleshooting.md) | 根据现象定位阶段并采取安全动作 |
| 人员交接 | [交接手册](handover.md) | 接手者要拿到什么、如何证明能独立维护 |
| 版本审计 | [版本来源](release-provenance.md) | 当前源码从哪里来、原始制品如何核验 |
| 仓库整备 | [目录整理说明](repository-cleanup.md) | 哪些本地材料不属于 Git、何时可以物理删除 |

安全和责任文件位于仓库根目录：[`SECURITY.md`](../SECURITY.md)、[`MAINTAINERS.md`](../MAINTAINERS.md)、[`CONTRIBUTING.md`](../CONTRIBUTING.md)。
