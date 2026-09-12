---
name: supervisor
description: OpenClaw 飞书统一 Supervisor：按已注册的路由契约判断、调度和关联业务 Skill，拒绝不完整或歧义请求。
---

# Supervisor

此 Skill 是飞书事件的唯一 Supervisor。部署时，OpenClaw Feishu 通道只将当前用户事件交给本 Skill；
所有业务 Skill 都是由本 Skill 显式调用的能力，不能各自订阅、扫描或自动匹配同一条飞书消息。

每条事件先调用 scripts/openclaw_skill_router.py 的 route_message，并只执行一个已注册路由结果。
当前注册了 uroad、阿斯加德派发和 AD P99；这些只是首批扩展，不构成 Supervisor 的名称或边界。

| 路由 | 条件 | 后续动作 |
|---|---|---|
| asgard-dispatch | @ 机器人、时延分析、两条同 VIN 且非重叠的完整 fsdlog | 用受控的 asgard-bridge 分两条消息以用户身份发送；保存 comparison_key，等待结果 URL。 |
| ad-p99-continuation | 角色、comparison_key、单 URL 或当前结果卡片回复 | 将当前结果绑定到待处理的 AD P99 任务。 |
| ad-p99-comparison | 两条带 AD P99/对比意图的 Sheets URL；或同一待处理任务中带 Baseline、替换后标签的两个 URL | 调用 ad-p99-analysis 读取并对比。 |
| uroad-log-analysis | 明确 uroad/云端日志请求、一个 VIN、一个时间段且无 Sheets URL | 调用 uroad-cloud-report。 |
| no-match | 其他输入 | 不运行业务 Skill；只说明所需格式。 |

fsdlog 本身不是 uroad 或 AD P99 的业务输入。只有严格的双命令时延分析格式才可派发阿斯加德；
其他 fsdlog、单 URL、模糊 VIN/时间段或未 @ 机器人的消息均不执行。

## 结果 URL 自动获取

asgard-bridge 在任务发送后，受限地读取配置中阿斯加德私聊的新增消息，验证发送者、VIN、时间段、
消息 ID 与卡片“查看结果”中的唯一 Sheets URL。两侧 URL 到齐前保持等待，不调用 AD P99。

若消息读取权限、卡片 JSON、唯一 URL 或时段匹配任一项失败，转为 waiting_manual_urls，请用户回复：

    Baseline: https://li.feishu.cn/sheets/<baseline>
    替换后: https://li.feishu.cn/sheets/<replacement>

每个扩展自行声明状态键。Supervisor 统一按 message_id 去重，并对相同状态键的更新和最终运行
加互斥锁。新增扩展按 references/extension-guide.md 注册；完整状态、身份与降级规则见
references/openclaw-skill-routing.md。

## 身份与职责

外部写操作只由 OpenClaw 的 asgard-bridge 执行，使用当前获准的 lark-cli --as user 登录态。
业务 Skill 不发送阿斯加德消息、不订阅会话，也不管理个人 OAuth 凭据。新负责人需以自己的账号重新
授权；不能转移离职员工的令牌。
