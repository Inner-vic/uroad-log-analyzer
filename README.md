# Uroad 日志分析

从获准车云来源获取同一 VIN、完整时间范围的 Uroad 与 CAN 日志，进行解析、关联、报告生成和内容校验，通过 CC Connect 原生会话交付 HTML 标准报告。报告完成后可在同一来源会话内对保留数据继续提问。

源码版本：`2026.11.03.8`。本项目在 [项目列表](https://github.com/Inner-vic?tab=repositories) 独立维护，与走查测试分开版本、部署、任务数据和故障恢复。

## 能力

- 自然语言 VIN 与时间解析，默认北京时间，保留原文、固定接收时间和可验证的规范化依据；无日期且无法确认的普通范围先询问。
- 标准报告优先。首条消息的附加问题原样入队，在报告交付后基于本任务数据回答。
- 完整时间窗一次取数，不按时间切片；Uroad / CAN 分支并行，逐步骤计时和超时控制，失败后排空所属进程。
- auto 模式先查询 Prod，成功为空或缺少必要来源时按完整报告口径回退 Test，同报告来源保持同环境。鉴权、网络及响应异常不当作空数据。
- 流式 CAN 解析；Parquet 备用链路按批读取、SQLite 排序和完整唯一点去重，保存来源清单、逐文件 SHA 和下载回执。
- 持久任务队列、来源消息幂等、跨进程锁、按提问者公平调度；计算状态与交付状态分别记录。
- 原生 HTML 附件只送回精确来源会话，完成发送且具有绑定回执才记为已送达。交付失败仅补发已有校验报告。
- 每任务数据默认保留 14 天；只读状态查询、同任务追问和严格绑定的 CAN 失败恢复。

## 目录

```text
README.md / docs/              部署、配置、架构、运维与验收
examples/                      无真实凭据的设置和环境样例
tools/                         源码校验、候选准备、调度安装与离线测试
workspace/AGENTS.md             CC Connect 入口契约
workspace/.agents/skills/
  uroad-cloud-report/           完整业务源码、模板、信号定义、测试与 Skill
  chassis-business-routing/    公共意图与时间规范化依赖
  ad-p99-analysis/scripts/     公共路由所需纯解析模块，无 AD P99 执行服务
SOURCE-PROVENANCE.json          原始生产源码 SHA 与导出差异
SOURCE-MANIFEST.json            Uroad 版本源码清单
REPOSITORY-MANIFEST.json        提交文件清单
```

不包含真实 settings、飞书来源标识、任务运行目录、车辆日志、报告、身份凭据或第三方二进制。

## 快速开始

目标环境按当前 Windows 受控安装准备 CPython 3.13 x64。依赖固定为 [requirements.txt](workspace/.agents/skills/uroad-cloud-report/requirements.txt) 中的五项；业务入口会以隔离探针检查并只修复该白名单。不能根据普通模式 import 结果推断业务依赖缺失，也不根据聊天内容安装额外包。

```powershell
python -B -X utf8 tools/check_repository.py
& $Python313 -B -X utf8 tools/prepare_workspace.py --workspace $NewWorkspace --base-python $Python313
```

该工具仅准备全新目录和独立 venv，生成私有配置样例，不注册/启动任务、不初始化授权、不接入业务。按 [部署说明](docs/deployment.md) 准备固定依赖、CC Connect、车云身份、任务服务和保留期清理。已有环境先兼容合入，禁止整目录覆盖。

## 使用

用户在飞书提交自然语言请求，无需命令。以下 VIN 为合成测试值：

```text
日志分析 DEM0VEH1CLEX00001，2026-11-03 09:53 至 10:10
请在标准报告后分析这个时段的相关异常。
```

在已部署工作区使用受控解释器调用；上下文必须原样来自同一真实入站消息：

```powershell
& $ControlledPython -B -X utf8 .agents/skills/uroad-cloud-report/scripts/run_uroad_task.py `
  --vin $Vin --raw-time-expression $OriginalText --received-at $ReceivedAt `
  --ccconnect-context $ContextHeader --question $AdditionalQuestion
```

没有附加问题时省略 `--question`。默认真实落盘入队并唤起独立 worker，`queued` 不代表已经完成解析或送达。状态查询使用 `task_status.py` 的原话题唯一绑定或显式任务编号，不进入附加分析队列。

追问入口 `followup_answer.py` 只验证来源、任务和队列；`ready_for_codex` 后由 Codex 在本任务的数据范围内分析并完成问题，不能跨任务查找最近报告。详情见 [运维](docs/operations.md)。

## 验证

```powershell
& $ControlledPython -B -X utf8 tools/run_tests.py
```

默认运行技能根目录及 `tests/` 的离线用例。GitLab CI 运行静态完整性检查和纯输入解析回归；Windows 原生调度/进程回收与新环境首单仍须单独验收。验证记录见 [validation-results.json](docs/validation-results.json)。

## 维护资料

- [架构和数据流](docs/architecture.md)
- [配置与外部依赖](docs/configuration.md)
- [部署与回退](docs/deployment.md)
- [状态、授权、追问和恢复](docs/operations.md)
- [版本变更](CHANGELOG.md)
- [提交约定](CONTRIBUTING.md)

本仓库提供实际 Skill 及独立调度实现。源目录中的共享 Supervisor 文档作为 [接入约定](docs/upstream-supervisor-contract.md) 保留，Supervisor 宿主不在本项目中；采用共享宿主的部署应由该宿主管理入口和全局执行槽，不能同时接通两套调度。

飞书接入说明：[CC Connect 原生附件与阿斯加德边界](docs/lark-asgard-integration.md)。Uroad 不需要向阿斯加德派发任务，不能把其他项目的 User OAuth 配置当作其发送前置条件。

## GitLab 自动校验状态

当前仅保留**手动流水线**：在 GitLab 的 Build → Pipelines → Run pipeline 明确启动。Push、合并请求和文档提交不自动执行，避免 Runner 镜像下载失败反复发送 Failed 通知。测试命令和检查步骤完整保留。

公司 Runner 当前无法从 Docker Hub 拉取 Python 镜像，尚未运行到测试；需要维护人员配置获准内部镜像或修复镜像源后再手动验证，成功后另行恢复自动触发。历史 Failed 记录保留，不能当作测试失败或线上业务任务失败；暂停触发不代表 CI 已通过。

公开副本的来源、示例配置与验证范围见 [发布说明](PUBLICATION.md)。
