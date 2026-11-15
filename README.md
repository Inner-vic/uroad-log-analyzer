# 日志分析 · Uroad Log Analyzer

[实习项目总览](https://github.com/Inner-vic/internship-projects) · [使用与验证](docs/project-guide.md)

把车辆日志请求变成同窗联合分析与可离线阅读的 HTML 证据报告。

## 解决的问题

车辆问题排查需要把自然语言中的 VIN 和时间，转换为可信的数据查询，再关联 Uroad 处理过程、CAN 信号、异常事件和报告交付。只给出一段日志或一张图，难以说明结论来自哪里，也难以安全地继续追问。

项目把请求、来源、解析、报告和送达串成独立任务，保留同一时间范围内的证据关系。

## 实现了什么

- 从同一车辆和完整时间窗获取 Uroad/CAN 数据，保留来源环境与下载清单。
- 通过流式 CAN、分批 Parquet 和任务内排序去重处理日志，生成可关联的时序数据。
- 生成自包含 HTML 报告，展示耗时、帧率、原因事件、车辆信号与帧级摘录。
- 保存队列、领取、计算和交付状态，支持标准报告后的同任务追问。

## 报告展示

![原报告渲染器生成的交互图表区域；顶部明确标注合成数据](https://raw.githubusercontent.com/Inner-vic/internship-projects/main/assets/uroad-report-demo.jpg)

上图由本仓库的报告渲染器生成，使用[公开合成输入](https://github.com/Inner-vic/internship-projects/blob/main/demos/uroad-analysis.synthetic.json)，只展示报告呈现与交互能力，不代表真实车辆或性能测试结果。生成脚本见[展示材料工具](https://github.com/Inner-vic/internship-projects/blob/main/tools/create_demonstrations.py)。

## 实现架构

![日志分析的主要模块、执行顺序和关键边界](https://raw.githubusercontent.com/Inner-vic/internship-projects/main/assets/architecture/uroad-log-analyzer.svg)

## 关键工程设计

### 1. 来源身份贯穿整条链路

请求正文与可信入站上下文分开保存，任务按来源消息做幂等。每项任务独立持有输入、下载、报告和追问数据；相同车辆与时间的新请求仍有自己的任务身份，避免按“最近报告”串用数据。

### 2. 用流式和批处理控制日志开销

Uroad 与 CAN 分支并行，CAN 采用流式解析，Parquet 备用链路按批处理并在任务内排序、去重。来源回执和内容摘要保留下来，报告中的序列能够追溯到实际输入。

### 3. 计算完成与报告送达分别建模

报告要先通过内容与工件校验，原生附件发送还要匹配来源、文件摘要和真实回执。交付失败只补发已经校验的报告；附加问题在标准报告之后，基于原任务保留数据回答。

## 实现与验证范围

仓库包含完整业务、报告模板和离线测试；实际取数与发送依赖获准的车云和消息平台。源码同步时的测试及环境边界见[公开验证说明](https://github.com/Inner-vic/uroad-log-analyzer/blob/main/PUBLICATION.md)。合成展示页没有执行真实查询、调度或消息发送。

## 源码导读

| 源码 | 关注的实现 |
| --- | --- |
| [workspace/.agents/skills/uroad-cloud-report/scripts/task_registry.py](workspace/.agents/skills/uroad-cloud-report/scripts/task_registry.py) | 持久注册与来源幂等 |
| [workspace/.agents/skills/uroad-cloud-report/scripts/scheduler.py](workspace/.agents/skills/uroad-cloud-report/scripts/scheduler.py) | 领取、调度与公平队列 |
| [workspace/.agents/skills/uroad-cloud-report/scripts/stream_can_signals.py](workspace/.agents/skills/uroad-cloud-report/scripts/stream_can_signals.py) | 流式 CAN 解析 |
| [workspace/.agents/skills/uroad-cloud-report/scripts/build_html_report.py](workspace/.agents/skills/uroad-cloud-report/scripts/build_html_report.py) | 离线交互报告渲染 |

## 项目资料

使用步骤、依赖和本地验证见[使用与验证](docs/project-guide.md)；源码来源与验证范围见[发布说明](https://github.com/Inner-vic/uroad-log-analyzer/blob/main/PUBLICATION.md)。本页用于介绍实习阶段的实现与设计，具体结论以所链接的代码和证据为准。

[走查测试](https://github.com/Inner-vic/walkthrough-test)展示双窗规则走查，[时延分析](https://github.com/Inner-vic/ad-p99-analysis)展示上游查询与性能对比；各自保留任务与运行边界。
