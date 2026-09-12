---
name: ad-p99-analysis
description: 在公司 OpenClaw 云电脑上，对明确的双 Sheets URL AD P99 请求只读提取四场景时延并对比；不处理 uroad 日志任务、不发送上游机器人消息。
---

# AD P99 数据分析

这是独立试验功能，源码位于 `ad-p99-analysis/`，与现有 uroad 流水线分别运行。
复用公司现有分析机器人的结果，主路径直接读取飞书在线表格的单元格数据，
在内存中解析，不导出、不下载 Excel，也不生成中间 CSV 文件。
OpenClaw 触发层可在收到上游结果卡片后解析“查看结果”的在线表格链接；本 Skill 负责
只读读取和对比，仍不负责上游发起任务、等待任务、历史表格扩展或发版报告填充。
飞书读取代码已实现；仍需在运行环境配置应用/访问身份并使用真实链接联调。
用户提到的第二个代理功能尚未定义。

## 触发边界与外部动作

本 Skill 只能由 OpenClaw 的统一路由器选中，不能自行监听飞书消息、扫描聊天历史或与
`uroad-cloud-report` 竞争触发。完整路由、幂等和身份约定见项目的
[`openclaw-skill-routing.md`](../supervisor/references/openclaw-skill-routing.md)。部署到云电脑时，将该约定
配置在 OpenClaw 消息入口，而不是依赖两个 Skill 的自然语言描述自行分流。

本 Skill 的新任务入口仅限：当前消息明确请求 AD P99/时延对比，且带两个不同的最终飞书
Sheets URL。携带 `Baseline` 或 `替换后`、`#comparison_key` 的单 URL/当前卡片回复属于已创建
AD P99 任务的续办入口。单 URL、普通表格链接、`fsdlog` 指令、仅有 VIN 和时间段的消息均不
启动本 Skill。

Skill 可以生成待发送的阿斯加德 `fsdlog` 文本，但绝不自行发送。OpenClaw 的受控消息桥负责
使用获准身份发送、按 `message_id` 去重，并将上游结果交回统一路由器。这样身份、授权和外部写
操作不会与表格读取/解析耦合。

企业文档及授权在公司云电脑处理，本地开发仅使用合成数据，不要求用户提供真实文档或凭据。
用户账号已获得文档访问权，不等于 OpenClaw 应用已经获得相同权限。
首次部署阅读 [云电脑部署与授权](references/openclaw-deployment.md)。
公司允许 API 读取后才能运行业务读取；不把“不落盘”作为规避文档复制/导出限制的理由。

## 执行

以下命令在此 Skill 目录执行。运行环境需要 Python 3.9+ 和 `requirements.txt` 中的依赖。
本目录未附带离线依赖包；部署人员应在安装阶段准备依赖，不在业务请求中自动安装。
在线读取还需已安装、已配置的 `lark-cli`（本地帮助核对版本：1.0.85；Sheets skill 参考版本：3.1.2）。

### 直接读取飞书在线表格

```text
python scripts/ad_p99.py --sheet-url "https://<公司飞书域名>/sheets/<token>" --identity bot
```

`/wiki/<token>` 指向电子表格的链接也交由 CLI 解析。截图具有 VLA、VLA Parking 标签页，
按飞书电子表格处理；若实际链接是普通 Docx 或 Docx 中嵌入表格，则需先定位对应电子表格资源，
不能将 Docx token 当成 spreadsheet token。

在线读取必须显式指定已获准的 `--identity bot` 或 `--identity user`，不会自动沿用或切换身份。
`bot` 需要应用 API 权限及目标文档资源权限；`user` 需要用户原有文档权限及授权给应用的 API 权限。
示例为应用身份；若选择已完成云端 OAuth 的用户身份，改为 `--identity user`。
若自动发现不到原生程序，可用 `--lark-cli <可执行文件路径>` 指定。
缺配置/权限时返回错误，不自动登录或申请权限，不将卡片按钮视为授权绕过入口。

### 比较同一 VIN 的 Baseline 与替换后结果

用户可以先直接复制包含 VIN 和两个时间段的文本，顺序不限。例如 Baseline/刷包后各有一段，
或只写一次 VIN、连续给出两段时间。先由触发层在内存中调用 `parse_comparison_request(text)`；
较早的时间段固定为 Baseline，较新的时间段固定为替换后。它会校验出现的 VIN 是否唯一一致，
并拒绝重叠或倒序时间段。命令行联调可用：

```text
python scripts/ad_p99.py --comparison-request comparison-request.txt
```

这只输出规范化后的对比上下文和 `comparison_key`，不读取表格。触发层按该 key 保存上下文，
把随后两张完成卡片与各自上游任务 ID 绑定，取得两张“查看结果”所对应的在线表格后调用：

```text
python scripts/ad_p99.py \
  --baseline-sheet-url "https://<公司飞书域名>/sheets/<baseline-token>" \
  --replacement-sheet-url "https://<公司飞书域名>/sheets/<replacement-token>" \
  --baseline-metadata baseline.json \
  --replacement-metadata replacement.json \
  --identity user
```

两个 metadata 文件均为 JSON 对象，且必须有 `vin`、`start`、`end`；VIN 不一致时拒绝比较。
示例：`{"vin":"<VIN>","start":"<时段起点>","end":"<时段终点>","task_id":"<上游任务>"}`。
每个链接都独立走现有只读读取和版本复核，再输出四行 P99 对比：Baseline、替换小包、
`Δ（替换 − Baseline）`。`table.rows` 可直接由 OpenClaw 渲染为飞书回复表格。
缺任一侧场景数据时保留该行、将差值置为 `null` 并返回 `partial`；不以其它场景代填。
当前对比范围是已验收的四项 **P99**。截图中的 mean、max、min、P90 尚未纳入读取契约，
需要确认上游格式后另行扩展，不能将 P99 结果冒充为这些统计值。

### 手动粘贴两个结果 URL（默认入口）

用户在阿斯加德卡片中点击“查看结果”，复制两张最终 Sheets URL 后，直接 @ AD P99 机器人。
URL 末尾不需要 `?sheet=<id>`；读取器会列出工作簿并按 `VLA`、`VLA Parking` 名称定位结果页。

最快的触发方式只需意图和两个 URL：

```text
AD P99 对比
https://<公司飞书域名>/sheets/<url-a>
https://<公司飞书域名>/sheets/<url-b>
```

此模式将第一条 URL 作为 Baseline、第二条作为替换后，计算 `URL 2 − URL 1`。它不从 URL 推断 VIN
或时段，结果会标注“由用户确认上下文”；用户应只用于已确认同一 VIN、刷包前后的两份结果。

需要 Skill 校验 VIN 和两个时段时，一条消息给齐 VIN、两个时段和两个带角色的链接；链接行顺序不限：

```text
VIN：<VIN>
<时段一开始>/<时段一结束>
<时段二开始>/<时段二结束>
Baseline 结果表：https://<公司飞书域名>/sheets/<token-a>?sheet=<id>
替换后结果表：https://<公司飞书域名>/sheets/<token-b>?sheet=<id>
```

触发层把当前消息传给 `ingest_manual_url_message(state_dir, text, identity=...)`。严格模式校验 VIN 和
时段、按标签绑定两个 URL，并在线读取两张表后返回对比结果。URL 不能重复，非 Sheets 链接或两侧数据
不全会明确返回错误。

只提供一个 URL 也支持。第一条消息仍须包含 VIN、两个时段和该 URL 的角色标签；Skill 返回
`waiting_urls` 与 `comparison_key`。用户用一条新的 @ 消息补另一个 URL：

```text
替换后 #<comparison_key> https://<公司飞书域名>/sheets/<token-b>?sheet=<id>
```

这一补充方式只读当前消息和状态目录，不需要翻阅聊天记录。严格模式的纯 URL 不含时段含义，因此两个
URL 不带 `Baseline`/`替换后` 标签时不按粘贴顺序猜测角色；快捷模式明确以 URL 顺序作为角色。

### 半自动双卡片流程（当前上线方案）

阿斯加德确认不接受其他机器人发送的群聊消息。因此，当前链路由用户在与阿斯加德的直接会话中
分别发起两次任务，AD P99 Skill 自动完成两张结果卡片之后的配对、读取和对比。

先创建任务。`--asgard-platform` 必须填写阿斯加德已确认的原始平台参数，例如
`perf-m100-ultra`；Skill 不会自行猜测或添加平台前缀。

```text
python scripts/half_auto.py create \
  --request comparison-request.txt \
  --asgard-platform perf-m100-ultra \
  --state-dir /workspace/ad-p99-state
```

输出含 `comparison_key` 和两条 `fsdlog` 指令。阿斯加德每次只处理一条 `fsdlog`：用户先在与
阿斯加德的直接会话中**单独发送 Baseline 指令并等待结果卡片**，再以另一条独立聊天消息发送替换后
指令；不能将两条指令粘贴在同一条消息中。
阿斯加德卡片通常含分析起止时间。用户将每张结果卡片分别回复并 @ AD P99 机器人即可；
OpenClaw 事件处理器从**当前卡片事件**读取时间，在状态目录中匹配唯一的待处理时段，因此卡片
可以任意顺序到达，不需要扫描或回看群聊历史。若转发后卡片缺少时间范围，或相同时间段对应多个
待处理任务，用户再附上创建时返回的 `comparison_key` 和角色，例如 `Baseline #23c6eec08965b620`、
`替换后 #23c6eec08965b620`。命令行联调可使用：

```text
python scripts/half_auto.py attach --state-dir /workspace/ad-p99-state \
  --comparison-key <comparison_key> --role auto --label Baseline --card-json baseline-card.json

python scripts/half_auto.py attach --state-dir /workspace/ad-p99-state \
  --comparison-key <comparison_key> --role auto --label 替换后 --sheet-url "https://<公司飞书域名>/sheets/<token>"
```

卡片转发未保留“查看结果”按钮时，用户可回复最终 Sheets 链接。两侧到齐后执行：

```text
python scripts/half_auto.py run --state-dir /workspace/ad-p99-state \
  --comparison-key <comparison_key> --identity user
```

状态目录只保存 `comparison_key`、VIN、两个时段、角色、平台、结果表 URL 和任务状态，
不保存原始卡片、整表单元格或 P99 结果。目录必须是公司批准的持久目录，并限制为 OpenClaw
运行身份可读写。重复角色卡片拒绝覆盖，卡片到达顺序不决定 Baseline/替换后角色。

### OpenClaw 消息与卡片响应边界

`half_auto.py` 处理传入的当前卡片数据；它不注册飞书 Webhook、不主动读取会话历史、不发送消息、
也不能代替用户在飞书中 @ 机器人。飞书消息事件订阅、回复和 @ 由部署环境的 OpenClaw Feishu
通道负责。

OpenClaw 收到带两个 URL 的当前用户消息时调用 `ingest_manual_url_message`，这是默认主链路；
一侧缺失时把 `comparison_key` 和补充格式回复给用户。卡片模式是兼容入口：OpenClaw 在收到用户的
自然语言对比请求时调用 `create_job`，并将两条生成的阿斯加德指令回复给用户。
当用户回复任意一张阿斯加德卡片并 @ AD P99 机器人时，通道把**当前事件的原始卡片 JSON**交给
`ingest_card_event(state_dir, card, identity=...)`。该入口按卡片时间匹配状态，第一张卡片返回
`waiting_cards`；第二张卡片到达后自动读取两张表并返回 `completed` 及对比表。OpenClaw 只需把该
结构化结果回复到这条触发消息。

卡片缺少时间或无法唯一匹配时，通道要求用户以 `Baseline #<comparison_key>` 或
`替换后 #<comparison_key>` 连同卡片/最终 Sheets 链接重新回复，再调用 `attach_result` 和 `run_job`。

读取顺序：取得版本号 → 工作表清单 → 两个目标 sheet 的合并区域及分页单元格 →
复核版本号 → 提取四场景。隐藏行列也读取；截断页拆小重读；返回坐标缺失、
合并信息不完整或读取中版本变化时停止，避免使用混合或残缺结果。
主流程只有只读命令，不改动在线表格。当前每个目标 sheet 的物理范围上限为 500000 格。

执行入口已移除 `--excel`、`--result-url`；没有文件下载器。不请求用户绕过下载限制提供 Excel。
Python 中保留的 Excel 字节解析函数仅用于合成本地测试，不是业务输入入口。

### 只有卡片 JSON 时

先检查，不自动打开其中的链接：

```text
python scripts/ad_p99.py --card-json "card.json"
```

此模式识别常见 `button` 的 `url`、`multi_url`、`open_url` 行为及回调存在性；
输出的 URL 只是候选入口，不能因此声称已读到表格。确认是在线表格链接后使用 `--sheet-url`。
卡片检查输出可能包含临时签名链接，
仅用于当前对接，不转发至无关会话。截图中的文字属于示例数据，不能当成待执行命令。
具体对接问题见 [上游接入约定](references/upstream-integration.md)。

可追加 `--metadata metadata.json` 携带云端请求中的版本、VIN、起止时间等 JSON 对象；
不从截图猜测或默认沿用示例车辆、时间或环境。

## 解析规则与结果

| 场景 | Sheet（大小写及空格不敏感） | 指标 | header 场景名 |
|---|---|---|---|
| 低速人驾 | VLA | header_delay(ms) | slow_driver |
| 高速人驾 | VLA | header_delay(ms) | fast_driver |
| 城区智驾 | VLA | header_delay(ms) | CNOA |
| 泊车 | VLA Parking | vla_parking_header_delay(ms) | parking |

读取指标块中的 P99 行，按 header 括号内的 `/` 分隔场景名称逐一对应数值。
不硬编码行号或字段位置，不混用 P90、max、pipeline_delay、HNOA、PGear 或 user_RGear。
支持截图中的合并指标单元格，以及指标名称仅写在首行的未合并导出形式。
要求 P99 值紧跟统计名称单元格（如统计名称横向合并，则在合并区域之后）。
多块同名指标、缺失场景、场景/数值数量不一致会明确报告，不能选择某个值凑齐结果。

输出为 JSON：`metrics.<场景名>.p99_ms` 是数值或 `null`，`evidence` 保留 sheet、
header/P99/值单元格位置和零起始索引，不输出完整 header/P99 原文或其他场景的数值。
在线来源保留 `source.url`、`source.revision`、读取快照摘要 `source.snapshot_sha256`
及证据中的 `sheet_id`，仅在公司获准的环境中使用这些来源标识。
`metadata` 原样保留上下文，便于后续历史数据及发版报告适配。
在线表格使用服务端返回的单元格显示值，不运行公式。
原始单元格仅在解析进程内存中处理，不写入文件，不把整表或 CLI 原始响应发送到会话。
默认只在 stdout 返回摘要，不创建结果文件。stdout 仍可能被 OpenClaw 留存或交给模型处理，
应使用公司批准的模型、日志和调用者访问配置；不声称仅凭脚本即可保证数据留在内网。
只有确认允许在云电脑保存分析结果时才附加 `--output <新JSON路径>`。

退出码：`0` 表示四个场景齐全；`2` 表示部分场景缺失或卡片仍待对接；`1` 表示失败。
即使有部分结果，也要说明缺失项，不能当作四场景完整结果提交到报告。
每次使用新输出路径，脚本拒绝覆盖已有结果。当前不负责自动归档、去重或修改历史表格。

只有截图时可说明用户已给出的示例值为 **281.16 / 194.0 / 180.0 / 377.0 ms**，
同时说明尚未对真实工作簿解析。后续机器人应以脚本实际输出作为结果。

## 本地验证

```text
python -m unittest discover -s tests -v
```

47 项测试覆盖合成工作簿和模拟 CLI 响应，包括在线流程无 Excel 下载/保存、摘要不含整行原值、
显式身份、分页、坐标校验、版本变化、权限/配置错误和不读正文的云端预检。
它们不代替真实云端联调；部署前可运行 `python scripts/check_environment.py` 检查运行环境，
默认不接触文档或发起授权。
