# 部署与回退

## 新工作区

1. 运行 `tools/check_repository.py` 校验提交文件、源码 SHA、语法与敏感信息。
2. 运行 `tools/prepare_workspace.py --workspace <新绝对目录> --base-python <CPython3.13.exe>`。目标必须不存在；工具复制源码、创建独立 venv 和私有样例，不注册或启动任何服务。
3. 按 requirements 在该受控解释器中准备固定包，或由真实业务入口按白名单检查自愈。提前安装可使用 `python -I -m pip install --no-deps --only-binary=:all: -r <requirements.txt>`，通过组织获准制品源准备。不要混入走查依赖。
4. 按配置文档准备车云身份、CC Connect 原生发送、飞书会话来源和 Codex 追问环境。运行离线测试，核对宿主实际采用独立模式还是共享 Supervisor。
5. 独立模式使用 `tools/Install-Services.ps1 -Workspace <目录> -PythonExecutable <独立python.exe>` 注册任务服务和每日清理。脚本拒绝覆盖已有同名任务，不立即启动。共享宿主不注册第二套服务，由宿主接入本 Skill。
6. 将 workspace/AGENTS.md 与 Skill 兼容接入 CC Connect。未完成身份、进程和来源验收前不接通业务入口。使用获准真实请求完成首次线上验收并记录证据。

候选目录准备不表示已具备车云/飞书权限；脚本自检也不能证明真实报告可发送。首次线上验收需确认完整时间范围、Uroad/CAN 同环境、完整报告校验、精确来源原生附件和有效回执、附加问题与保留期策略。

## 升级

版本源码和 requirements 一起审查。保存旧代码、任务状态、报告和来源/下载回执，不用新版本覆盖正在执行的目录。先暂停新领取并等待所属进程结束；已有任务按原版本和绑定恢复。业务重试不得覆盖旧失败与进度证据。

## 回退

恢复明确备份的入口、源码和任务 action；保留新版本任务及诊断，不通过移走 CAN 索引破坏其精确路径绑定。不要递归删除共享输出、身份或日志。未知回执、来源或 lease 不自动接管；计算完成但交付失败只补发原校验报告。
