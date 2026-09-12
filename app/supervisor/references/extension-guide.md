# Supervisor 扩展约定

新增业务 Skill 不直接接入飞书通道。它向 Supervisor 注册一条路由契约和一个显式执行器。

每个扩展至少声明 route、priority、entry_skill、trigger、state_key、idempotency_key 和 fallback：

    route: 唯一的 kebab-case 路由名
    priority: 与其他路由的优先级
    entry_skill: 被调用的业务 Skill 名称
    trigger: 当前事件可确定判断的条件
    state_key: 用于隔离和加锁的字段
    idempotency_key: 默认 message_id 或业务任务 ID
    fallback: 无法唯一匹配时的用户提示

路由条件必须能由当前事件、受控状态和明确配置判断；不能扫描聊天历史或让模型猜测。两个扩展同时
满足时，Supervisor 不按猜测选择：优先级相同则返回歧义并停止；不同优先级必须在契约中说明原因。

注册步骤：

1. 为扩展添加路由谓词和正反例测试。
2. 声明状态键、生命周期、锁和事件去重规则。
3. 在 Supervisor 中登记执行器到业务 Skill 的调用方式。
4. 添加与已有扩展冲突的测试后，才允许接入飞书入口。

当前三个扩展是 uroad-log-analysis、asgard-dispatch 和 ad-p99-analysis。后续如增加帧率、
报告写入或其他分析，只新增自己的契约、谓词、执行器和测试，不修改其他业务 Skill 的飞书入口。
