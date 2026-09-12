---
name: uroad-cloud-report
description: 按 VIN 和时间范围下载 uroad 与必需的 CAN 数据，完成分析，并生成单个自包含交互式 HTML 报告。
---

# uroad 云端分析报告

这个 Skill 用于把 uroad 日志与同时间段 CAN 数据放在一起交叉核对，形成可追溯、
基于证据的分析结论。

为适配 8 GB OpenClaw 主机，任意时间范围都会按固定十五分钟切片顺序处理。每个切片固定并发
两条受限通道：一条下载并分析 uroad，另一条获取并流式解析同环境 CAN。CAN 只保留
三个目标信号，逐文件处理并清理压缩源，不生成 ZIP 或 ASC。两条通道都成功并汇合后才写入切片证据，
而时间切片之间始终顺序执行；任一通道失败时会取消或等待同切片的另一条通道，清理两边工作区，且不会生成临时 HTML。
长时间范围因此会按切片数量增加运行时间。正常退出及可捕获的异常会清理中间文件；若发生 OOM、宿主强制结束、主机重启或其他不可捕获终止，可能留下中间文件，后续依靠状态核验和维护清理处理。

当飞书或 OpenClaw 消息出现“日志分析”诉求，且内容包含 VIN、开始时间和结束时间时，默认就应走本 Skill，也就是 **uroad 主链路**。
不要把普通“日志分析”误路由到任何 AD 相关流程。

可将触发原则理解为：

- **默认**：`日志分析` / `uroad日志分析` / `uroad 日志分析` → `uroad-cloud-report`
- **同样按本 Skill 处理**：用户只给出 `VIN + 时间范围 + 日志分析`，即使没有写出 `uroad`
- **禁止**：因为历史 AD 相关 Skill、旧提示词或旧记忆，把“日志分析”优先解释成 AD 分析
- 当前部署以 **uroad 日志分析** 作为主链路；AD 相关链路已下线，不作为当前实例可见触发目标

即可调用本 Skill。例如：

```text
请分析 uroad 日志，VIN: LXXXXXXXXXXXXXXX，时间：2026-08-18 15:07:24 - 2026-08-18 15:08:24
帮我排查这辆车的 uroad 云端日志。VIN 是 LXXXXXXXXXXXXXXX，开始时间 2026-08-18 15:07:24，结束时间 2026-08-18 15:08:24。
DEM0VEH1CLE000001 2026-09-04 11:22-11:26 日志分析
DEM0VEH1CLE000001 2026-09-04 11:22-11:26 uroad日志分析
```

如果 VIN、开始时间或结束时间缺失、格式不清楚或时间范围有歧义，请用自然、协作的
方式向同事补问对应信息。用户无需了解或提供报告输出目录。

特别说明：像“`<VIN> <时间范围> 日志分析`”这种短句，应优先视为已表达完成的 uroad 分析意图，
不要求用户额外补充“这是 uroad”几个字；只有当消息里出现明确 AD/P99 关键词时，才切换解释。

信息完整后，应显式固定 **OpenClaw 工作区** 再执行完整流程；不要在 Skill 源码目录 `/workspace/skills/uroad-cloud-report` 下运行主入口，也不要只依赖 `cd /workspace`。主入口和后续校验都优先读取 `OPENCLAW_WORKSPACE`，因此命令里要一并固定该变量。当前部署示例工作区是 `/workspace`；如果更换主机或实例，应先确认实际工作区，再把下面示例中的 `/workspace` 替换为对应路径：

```text
cd /workspace && OPENCLAW_WORKSPACE=/workspace python /workspace/skills/uroad-cloud-report/scripts/run_uroad_pipeline.py --vin <VIN> --start "YYYY-MM-DD HH:MM:SS" --end "YYYY-MM-DD HH:MM:SS" --cloud-env auto
```

如需固定环境，可把最后的 `--cloud-env auto` 改成 `--cloud-env prod` 或 `--cloud-env test`。不要把 `auto|prod|test` 原样复制进 shell，以免被解释成管道或无效参数。

如果误在 Skill 源码目录中执行，主入口会因工作区保护直接失败；这类失败应视为调用方式错误，立即改到确认过的工作区根目录重跑，而不是向用户报告业务分析失败。

## 回复与等待方式

这是一条需要持续运行到报告完成的前台命令。请在触发请求的同一次任务中等待主入口退出，
不要把流水线改成无人接管的后台任务。如果执行工具返回可继续等待的会话标识，请持续等待同一个
执行会话，直到取得退出码和主入口最终 JSON；读取 `run.json` 只能用于故障诊断，不能用一次新的
状态查询代替对原执行会话的等待，也不要在发送阶段状态后结束当前任务。执行工具超时不等于主进程已经结束；在确认原进程状态前不要重复启动同一时间窗分析，以免生成重复运行或重复下载。

默认忽略普通进度，不向飞书发送“正在跑”“当前处理第几片”或“我继续盯着”等中间消息。
主入口成功退出后，只发送一次最终回复：按附件交付流程把 HTML 附件和一句简短说明放在同一条
消息中。主入口失败、被中断或达到 OpenClaw/执行工具的最长运行时间时，也请在同一次任务中
收尾，只发送一次简短、明确的失败结果，并说明安全的失败阶段或错误原因；不要承诺已经脱离
当前任务继续观察。用户主动询问进度时可以据实回复，但回复后仍需继续等待原执行会话并负责
最终交付。

## 离线 Python 运行时

不要单独检查或安装 Python 包，也不要执行依赖准备命令。信息完整后始终直接运行上面的
`scripts/run_uroad_pipeline.py` 主入口。主入口会在创建报告目录、下载 uroad 日志和 CAN 数据前：

1. 校验 VIN、开始时间和结束时间。
2. 检查本 Skill 的本地 `settings.json` 和固定环境映射配置。
3. 在独立 Python 进程中检查现有 `zstandard==0.25.0` 的模块来源和压缩/解压往返；通过则直接复用。
4. 否则根据解释器、ABI 和平台选择 Skill 内置的只读运行时，校验固定清单、许可证及每个已安装文件的
   SHA-256；清单保留构建时审核过的源 wheel 名称和 SHA-256 作为来源记录（部署包不包含 wheel 文件）。
   确认导入来源位于所选目录后，才通过 `PYTHONPATH` 传给子进程。

离线运行时仅支持 CPython 3.9–3.14、Linux x86-64、glibc（manylinux_2_17 兼容）。ARM64、
musl/Alpine、PyPy 或其他 Python 版本不会尝试联网、编译或修改系统环境，而会在业务请求和报告目录
创建前返回实际运行环境及支持矩阵。出现“清单/来源/wheel/许可证/文件完整性校验失败”或“模块来源、
版本或压缩往返校验失败”时，说明部署包缺失或被修改：立即停止并让部署维护人员用原始发布 ZIP
重新部署，不要绕过校验。出现“不受支持”时，应更换为支持矩阵内的主机；增加平台或变更版本需先确认。

部署 ZIP 解压后，可在 Linux 目标主机上用当前解释器执行离线冒烟验证：

```text
cd /workspace && OPENCLAW_WORKSPACE=/workspace python /workspace/skills/uroad-cloud-report/scripts/verify_offline_zstandard.py
```

命令会选择当前解释器对应的内置运行时，重新校验清单、许可证和运行时文件，确认原生后端来源并完成
压缩/解压往返。发布前应分别在 CPython 3.9–3.14 的无网络 Linux x86-64/glibc 环境执行该命令。

流程会从 `OPENCLAW_WORKSPACE` 指定的工作区创建唯一的
`outputs/uroad-report-runs/<run-id>` 目录；如果未配置 `OPENCLAW_WORKSPACE`，固定使用 `/workspace` 作为工作区根目录，
不再回退到当前目录。重复请求会分别保留，不覆盖已有结果。本 Skill 无需安装外部
CAN 下载 Skill。首次使用时复制 `settings.json.example` 为 `settings.json`，填写非空 `username`；
`python_path` 和 `last_used` 可继续保留以兼容既有配置，但不会改变固定环境映射请求。

每次新请求通过依赖与配置校验后，主入口会在创建本次目录前顺手整理可识别的历史运行目录。
**当前文档只确认到本地代码实现仍是：成功结果超过 24 小时删除 `analysis.json`、`can-signals.json` 和 `manifest.json`，保留 HTML 与 `run.json`；成功结果超过 7 天或超出最近 20 份、失败或中断结果超过 3 天时删除整个历史运行目录。** 如果云端实际部署已改成 48 小时保留、带追问保护，必须先核对实际脚本与维护逻辑，再统一更新此处与相关引用文档；不要只改文档数字。

在未核对云端脚本前，用户追问时应先按 VIN、时间窗和已有 `run.json` / HTML 路径查找是否存在对应运行记录，优先复用现有报告与保留证据，而不是立即重新下载并重跑完整分析。状态为 `running`、目录名不符合本 Skill 运行规则、`run.json` 缺失或不可读、符号链接以及本次尚未创建的任务都不会被清理。整理失败只记录在新任务 `run.json.housekeeping`，不会阻止本次报告运行。

在共享窗口里，如果同一时间附近出现多条日志分析消息，必须把每条消息当作独立请求处理：不要从邻近消息借 VIN、时间范围或结论；如果用户没有明确说“继续上一个/复用上次结果”，就不要把新消息并入已有运行。对于与已有请求完全相同或明显重复的输入，优先按 VIN + 时间窗检查是否已经存在可复用的报告，再决定是否重跑。

流程支持三种运行模式：`auto`、`prod`、`test`。其中 `test` 对外显示为 `test`，调用车云接口时固定映射为 `testtwo`。
本 Skill 的 API 适配器查询 `log_fsdA_service` uroad 数据，并通过自有 `scripts/cheyun_prod_client.py`
获取同环境“报文数据”中的 `msg_xcu`，再由 `scripts/stream_can_signals.py` 流式解压 EEA3，只解码
`0x12A`、`0x108` 和 `0x106`。同一次报告中的 uroad 与 CAN 必须来自同一个环境，不能混用。
`auto` 模式先检查 `prod`，仅当 `prod` 的 HTTP 请求成功、响应结构有效、但目标数据为空或不完整时，
才允许整次报告回退到 `test/testtwo`；认证失败、网络失败、HTTP 5xx、响应结构异常、配置缺失或接口明确报错时不回退。
`prod` 与 `test` 强制固定环境，数据不足时直接失败。为了保证交叉核对完整，三个已配置 CAN 信号需要全部存在。
`manifest.json`、`analysis/analysis.json` 和 `run.json` 是内部诊断及阶段契约；
唯一最终报告按 `report/uroad-report_<VIN>_<开始时间>_<结束时间>.html` 命名，位于固定输出目录 `outputs/uroad-report-runs/<run-id>/report/` 下；时间使用
`YYYYMMDD-HHMMSS` 格式，例如
`uroad-report_DEM0VEH1CLE000001_20260821-150000_20260821-155959.html`。该 HTML 为自包含单文件，
内含样式、数据与交互逻辑，无需联网即可打开。仅当本次请求来自飞书时，报告请继续按照
[OpenClaw 附件交付流程](references/openclaw-attachment-delivery.md)：从成功结果的
`run.json.artifacts.report` 取得 HTML 路径，完成发送前复核。发送前先确认当前部署里**已经验证、已经授权**的附件交付入口；优先使用已验证可发送本地文件的方式，不把判断范围限制在单一回复工具或某一个 schema 形态。工具必须明确返回成功，并提供附件/file/media 标识或含附件证据的消息结果，任务才算完成；仅上传成功不代表消息已经发送。附件失败时保留报告和运行目录，只重试交付，不重新下载或重新分析。

交付状态回写：发送 HTML 后必须把实际 outbound 返回结果传给 `/workspace/skills/uroad-cloud-report/scripts/delivery_bridge.py` 中的 `record_delivery_result(run_id, result)`。只有结果包含 `messageId`/`message_id` 或明确 `ok: true` 才能记为 `sent`；仅有 `fileId`、上传成功或本地路径不能记为已送达。发送异常必须记录为 `failed`。用户说“重试发送”“补发刚才的报告”时，只调用 `retry_candidates()` 取得已有 `execution.status=completed` 且 `delivery.status in {pending, failed}` 的报告，再将其作为 `media` 重新发送，不得调用 `run_uroad_pipeline.py`。

主入口成功 JSON 会返回 `run` 字段。发送前使用该字段调用 Skill 内固定校验脚本：

```text
cd /workspace && OPENCLAW_WORKSPACE=/workspace python /workspace/skills/uroad-cloud-report/scripts/validate_report_artifact.py --run-json "<主入口返回的 run 路径>"
```

附件路径只采用校验脚本成功 JSON 中的 `report`。无需为查询状态、寻找、检查、复制或重命名报告
临时编写 Python 内联脚本，也不使用 heredoc；个性化文件名已经由主入口直接生成。校验脚本失败时
按交付失败处理并返回简短原因，不再尝试拼接临时脚本绕过校验。

如果路径校验失败、HTML 类型被通道或本地文件策略拒绝、发送报错，或没有得到明确
确认，请保留生成的 HTML 并友好说明具体交付问题，任务仍未完成。文件路径、Markdown
链接、`MEDIA:` 文本、工具输出或长篇文字总结都不会形成附件，也不适合作为纯文字替代。
如需更改飞书/OpenClaw 配置、调整安全策略、转换格式、改传云盘或发送到其他会话，
请先和用户确认。以上流程交付的最终产物就是 HTML 附件。

流程会在创建输出目录及发起任何网络请求前检查依赖和配置。遇到 uroad 无数据、
CAN 下载失败或缺少所需信号时，会安全停止且不生成 HTML 报告；错误信息会保护
已配置的用户名。成功后保留内部阶段契约和 HTML 报告；除非指定
`--keep-on-success`，否则删除下载的原始数据。

请把 `settings.json` 和凭据保留在本 Skill 的本地安全配置位置，发布包只包含示例配置；运行目录和报告建议由工作区
统一管理，并与 Skill 源码目录分开。一条信息完整、明确要求“请分析 uroad 日志”的
消息本身即授权执行本次云端查询，并将本次生成的 HTML 作为附件交付；信息缺失或
有歧义时先友好补问。安装或发布整个 Skill 仍需另行取得用户明确授权。
