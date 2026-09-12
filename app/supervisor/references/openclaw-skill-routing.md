# OpenClaw 双 Skill 路由与身份契约

适用对象：`uroad-cloud-report` 与 `ad-p99-analysis`。OpenClaw 的飞书入口是唯一的
消息消费者；两个 Skill 不各自订阅消息，也不扫描会话历史。

## 路由规则

所有规则都要求用户在当前消息中 @ OpenClaw 机器人。路由器对每个消息事件只返回一个结果：
`asgard-dispatch`、`ad-p99-comparison`、`ad-p99-continuation`、`uroad-log-analysis` 或
`no-match`。未命中只给出补充格式，不发起查询、下载或消息发送。

优先级如下：

1. 明确写有“时延分析”，且恰好包含两条语法完整、同一 VIN、非重叠的 `fsdlog` 指令：
   交给 `asgard-dispatch`，将两条指令拆成两条独立飞书消息。
2. `Baseline` / `替换后`、`#comparison_key`，并且有一个结果 URL 或当前事件是一张回复卡片：
   只恢复该 AD P99 对比任务。
3. 两个不同的飞书 Sheets URL，且文字明确写有 `AD P99`、`P99 时延`、`时延对比`、
   `对比`、`比较`、`Baseline` 或 `替换后`：运行 AD P99 双表对比。
4. 无 Sheets URL，明确出现 `uroad` 或 `云端日志`，并有 `日志`、`log`、`分析`或`排查`，
   恰好一个 VIN、恰好两个时间戳：运行单时段 uroad 云端日志分析。
5. 其他情况：不运行任何 Skill。

只有第 1 条的严格格式可以派发阿斯加德。其他 `fsdlog ...` 都不作为两个本地 Skill 的入站触发。
单个 Sheets URL 也不会自动启动 AD P99：用户必须补齐另一条 URL，或携带任务键和角色。

推荐的派发格式如下；“时延分析”可写在首行或末尾：

```text
@uroad 日志分析助手 AD P99 时延分析
fsdlog <VIN>, <起点 1>, <终点 1>, <platform>
fsdlog <VIN>, <起点 2>, <终点 2>, <platform>
```

派发完成后，OpenClaw 在同一 `chat_id + sender_open_id` 下保存一条待结果任务。用户从两张
阿斯加德卡片各自点击“查看结果”并复制最终 Sheets URL 后，回复前台助手：

```text
Baseline: https://li.feishu.cn/sheets/<baseline>
替换后: https://li.feishu.cn/sheets/<replacement>
```

路由器只有确认当前用户恰有一条待结果任务、并且两个 URL 都带上述角色标签时，才将其交给
AD P99 Skill。URL 标签将其绑定到先前派发的两个时间段，避免用户粘贴顺序或两次任务并发时串单。

示例：

```text
@机器人 AD P99 对比
https://li.feishu.cn/sheets/<baseline>
https://li.feishu.cn/sheets/<replacement>
```

```text
@机器人 请分析 uroad 日志，VIN: <VIN>，
时间：2026-08-30 16:54:00 - 2026-08-30 17:24:00
```

## OpenClaw 与 Skill 的职责

OpenClaw 负责接收事件、提取 @ / 当前卡片 / `message_id`、调用路由器、保存最小状态、执行
外部写操作并回复原会话。每条事件只能由路由器选择的一个执行器处理。

`ad-p99-analysis` 只负责在已选中后读取两份最终 Sheets URL、提取四项 P99 并计算
`替换后 − Baseline`。它可以生成两条阿斯加德指令，但不发送消息、不调用阿斯加德、
不点击卡片，也不订阅飞书事件。

`uroad-cloud-report` 只负责已选中后的日志和 CAN 分析及报告生成。它不解析 Sheets URL，
不接管 AD P99 的 `comparison_key`。

阿斯加德发送属于 OpenClaw 的 `asgard-bridge` 动作，而不是 uroad 或 AD P99 Skill 的功能。
只有上面的严格派发请求才允许桥接。桥接调用 `parse_asgard_dispatch` 后得到两条命令，逐条使用
获准用户身份发送到受控的阿斯加德会话；每条 `fsdlog` 必须是独立飞书消息，绝不能拼接发送。
该动作需用 `message_id` 或已保存的派发键去重，不能因飞书重投而重复发送。

两条消息均由飞书 API 成功接受后，桥接将任务状态置为 `waiting_result_urls`；这只表示命令已发出，
不代表阿斯加德已分析完成。URL 到齐前不调用 AD P99 的表格读取。两个 URL 到齐后，OpenClaw 调用
AD P99 的带角色双 URL 入口，使用派发时保存的 VIN、两个时间段和 `comparison_key` 完成对比。

### 自动获取结果 URL

优先由 `asgard-bridge` 自动获取。桥接在每条 `fsdlog` 发送成功后，限定在受控的阿斯加德
私聊 `chat_id` 中读取**发送时刻之后**的新消息；只接受经配置确认属于阿斯加德的结果卡片。
它从卡片原始 JSON 的“查看结果”按钮中提取唯一的飞书 Sheets URL，并要求卡片显示的 VIN/起止时间
与该派发任务的角色匹配。第一张卡片保存为 `baseline` 或 `replacement`，另一张同理；两张均
匹配后立即调用 AD P99。

这不是全量会话扫描：查询范围必须同时受 `chat_id`、阿斯加德发送者、发出命令的时间和待处理
`comparison_key` 限制，并且每个消息 ID 只能处理一次。轮询应有最长期限和退避；到期、缺少读取
scope、卡片没有可读的 Sheets URL、URL 非 Sheets 或时间无法唯一匹配时停止自动化，任务改为
`waiting_manual_urls`。此时才请用户手动点击卡片并按带角色 URL 格式补充。

## 状态、幂等与隔离

路由和执行状态至少持久化：`message_id`、`chat_id`、`sender_open_id`、`skill`、
`job_key`、`status`、`created_at`。

- 收到重复 `message_id` 时返回此前结果，不再次运行或发送。
- AD P99 对比状态按 `chat_id + sender_open_id + comparison_key` 隔离。
- 同一 `comparison_key` 的写入和最终计算使用锁，避免两张卡片同时到达导致重复对比。
- 等待 URL/卡片的任务设置有限期；到期后要求用户创建新对比，不能让模糊的新消息接管旧任务。
- 每次路由保存规则编号、候选 Skill 和拒绝原因，供误触发排查。

## 用户身份与离职交接

`lark-cli --as user` 使用的是某一位员工授权给飞书应用的用户令牌；它不能共享、转移或在
员工离职后继续代表该员工。人员变更时，由新职责人使用自己的企业账号在同一 OpenClaw 运行环境
完成公司批准的 OAuth 登录；旧账号在交接后撤销授权。新账号还需拥有阿斯加德会话权限、
目标 Sheets 的阅读权限和应用所需最小 scope。

如果业务要求无人值守，应由阿斯加德团队提供服务账号、API 或受控应用身份，不应依赖离职员工的
个人 `--as user` 登录态。OpenClaw 不保存、复制或转移个人浏览器 Cookie、密码或令牌。
