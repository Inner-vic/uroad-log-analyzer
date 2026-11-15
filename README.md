# 日志分析 · Uroad Log Analyzer

根据同一车辆的 VIN 和完整时间范围，从获准车云来源获取 Uroad 与 CAN 日志，完成联合解析、生成 HTML 标准报告，并通过 CC Connect 交付回原始会话。

源码版本为 `2026.11.03.8`。本项目独立维护日志分析的代码、部署和任务数据；实际使用需要 Windows 运行环境、车云权限及飞书/CC Connect 接入。

## 输入、处理与交付

- **输入**：VIN、时间范围，以及可选的附加问题；未标注时区时按北京时间解析，无法确认的时间先补齐。
- **处理**：完整时间窗一次取数，Uroad/CAN 分支并行。同一报告保持同环境，查询异常与成功但无数据分别处理。
- **输出**：经内容与路径校验的 HTML 标准报告，以及任务、下载和交付的可追溯记录。
- **追问**：先交付标准报告，再基于本任务保留数据回答附加问题；默认保留期为 14 天。

在已接入的会话中可以发送以下请求。VIN 为合成示例，使用时替换为获准车辆及实际时间：

```text
日志分析 DEM0VEH1CLEX00001，2026-11-03 09:53 至 10:10
请在标准报告后分析这个时段的相关异常。
```

入站身份和来源消息由接入层提供，不能从正文猜测或手造。任务排队、计算完成和报告送达分别记录；交付失败时补发已有校验报告。

## 获取源码与校验

```powershell
git clone https://github.com/Inner-vic/uroad-log-analyzer.git
cd uroad-log-analyzer
python -B -X utf8 tools/check_repository.py
```

校验无需连接车云或飞书。真实运行要求 **Windows x64、CPython 3.13**，依赖固定在 [requirements.txt](workspace/.agents/skills/uroad-cloud-report/requirements.txt)。

## 新环境准备

以下为路径示例：将解释器路径替换为自己的 CPython 3.13，目标工作区必须尚不存在。

```powershell
python -B -X utf8 tools/prepare_workspace.py --workspace "D:/workspaces/uroad" --base-python "C:/Path/To/Python313/python.exe"
```

工具复制工作区源码、创建独立 venv 和私有配置样例；不安装业务依赖、不授权或启动任务。随后按[部署说明](docs/deployment.md)完成依赖、车云身份、原生附件发送和宿主接入，并验证首次完整交付。

独立部署与共享 Supervisor 接入应选择一套宿主/执行槽，避免重复调度。源码中仅包含共享宿主的[接入契约](docs/upstream-supervisor-contract.md)。

## 源码结构

| 路径 | 内容 |
| --- | --- |
| `workspace/.agents/skills/uroad-cloud-report/` | 主业务、报告模板、信号定义及测试 |
| `workspace/.agents/skills/chassis-business-routing/` | 公共意图和时间解析 |
| `workspace/AGENTS.md` | 消息来源、调度和交付约束 |
| `examples/`、`tools/` | 配置样例、候选准备、检查及调度安装 |
| `SOURCE-*.json`、`REPOSITORY-MANIFEST.json` | 源码来源和完整性清单 |

公共路由所需的其他业务解析模块不代表该业务的执行服务已安装。

## 测试与文档

安装固定依赖后，使用候选的独立解释器运行离线用例，例如：

```powershell
& "D:/workspaces/uroad/runtime/uroad-python/Scripts/python.exe" -B -X utf8 tools/run_tests.py --suite portable
```

默认不带 `--suite portable` 时运行完整离线组。GitHub 的 [Source checks](.github/workflows/source-checks.yml) 检查源码与清单；本次同步的离线验证范围见[发布说明](PUBLICATION.md)。真实车云取数、Windows 调度与首单交付需在自己的环境验收。

- [架构与数据流](docs/architecture.md)
- [配置与外部依赖](docs/configuration.md)
- [部署与回退](docs/deployment.md)
- [状态、追问和失败恢复](docs/operations.md)
- [飞书接入](docs/lark-asgard-integration.md)与[版本变更](CHANGELOG.md)

凭据、真实车辆日志、会话标识、运行状态和报告不进入源码仓库。
