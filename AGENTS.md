<!-- bmad:context -->
<!-- Verified 2026-09-11 against the pre-Git working tree; no commit exists yet. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## Uroad Log Analyzer

面向 OpenClaw/飞书的车辆日志智能分析应用开发项目，核心由 Python Skills、确定性路由和自包含 HTML 报告组成。当前 uroad 核心基线位于 `app/uroad-cloud-report/`；Supervisor 和 AD P99 分别是路由核心与可选实验扩展。系统设计、部署和运维资料位于 `docs/`。

## Policy

- 不得提交真实 `settings.json`、凭据、登录二维码、车辆日志、真实 VIN 报告或运行目录；仅提交示例配置和合成测试数据。
- 公开仓库不得出现公司服务域名、真实接口地址或内部网络拓扑；部署端点和下载白名单只允许通过环境变量注入。
- 不得直接修改 `app/uroad-cloud-report/vendor/` 中的离线运行时；升级时同步更新来源、许可证、完整性清单和验证记录。

## Where things are

- 当前 uroad 主应用：`app/uroad-cloud-report/`
- 消息分类与路由核心：`app/supervisor/`
- AD P99 可选实验能力：`app/ad-p99-analysis/`
- 架构、功能、开发、部署、运维、排障和交接：`docs/`
- 本地配置从各 Skill 的 `settings.json.example` 创建；真实配置不得进入 Git。

## Running and verifying

- 本机 `python` 指向不可用的 Windows Store 占位程序；使用 `uv run --no-cache python`。
- Windows 运行 uroad 时间相关测试时显式加入 `tzdata`；Linux 通常使用系统时区数据库。
- 生产离线运行时只支持文档列出的 Linux x86-64/glibc 与 CPython 版本；Windows 测试通过不代表生产运行时已验收。
- 真实云接口、飞书附件发送和 OpenClaw 端到端验证需要已授权环境；单元测试不得被表述为线上验收。

## Conventions that differ from defaults

- 从 OpenClaw 工作区运行 uroad 主入口并显式设置 `OPENCLAW_WORKSPACE`；不得从 Skill 源码目录直接运行。
- 同一报告的 uroad 与 CAN 数据必须来自同一环境；系统错误不得触发 prod/test 回退。
- 唯一对外交付物是校验通过的 HTML；内部 JSON、上传成功或文件存在均不等于用户已收到报告。
- 报告补发只重试交付，不重新下载或重新分析。

## Known pitfalls

- `app/supervisor/` 当前仅实现确定性路由分类；不要把设计文档中的执行器、状态存储和阿斯加德桥描述为已落地。
- `app/ad-p99-analysis/` 尚未完成真实公司表格和 OpenClaw 端到端验收，且当前 uroad 基线明确未启用 AD 链路。
- 版本号、保留期限和环境行为只以当前代码与 `VERSION` 为准；历史文档不得反向覆盖当前事实。

<!-- /bmad:context -->
