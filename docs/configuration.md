# 配置与依赖

| 项目 | 约定 |
| --- | --- |
| Python | 当前 Windows 受控安装使用 CPython 3.13 x64；另建独立 venv，不复用走查的 CPython 3.11 环境 |
| Python 包 | `zstandard==0.25.0`、`pyarrow==25.0.1`、`openpyxl==3.1.5`、`et-xmlfile==2.0.0`、`tzdata==2026.4`；以 requirements 和脚本白名单同时一致为准 |
| CC Connect | 获准的 `cc-connect-context.exe`，须支持同步原生附件发送确认；本仓库不打包二进制或飞书授权 |
| 车云 | 获准 `username`，私有 settings 独立保存；生产/测试与下载主机严格按代码白名单核对 |
| Codex CLI | 只在附加问题或后续追问时使用，登录与策略由宿主环境准备 |
| CAN 信号定义 | 版本内 `references/can-signals.json`，与解析结果和来源共同核验 |

`settings.json.example` 是无真实身份的样例。私有配置默认位于当前执行账户的受控 secrets 目录，也可由 `UROAD_SETTINGS_PATH` 明确指定；配置内容不得发送到聊天或加入 Git。不要把同名样例直接当作有效授权。

环境变量：

| 变量 | 含义 |
| --- | --- |
| `CC_CONNECT_WORKSPACE` | 当前任务工作区根目录，明确设置以避免跨工作区定位 |
| `UROAD_SETTINGS_PATH` | 本业务私有车云配置的绝对路径 |
| `UROAD_CC_CONNECT_BIN` | 实际 `cc-connect-context.exe` 路径；默认工作区 `build/` |
| `UROAD_CC_CONNECT_PROJECT` | 多 CC Connect 项目时固定原生 CLI 实际运行项目名；单项目可省略 |
| `UROAD_CODEX_BIN` | 已登录的 Codex CLI 路径；也可由受控 PATH 解析 |
| `UROAD_MAX_HEAVY_JOBS` | 独立模式默认 2，共享宿主应由其统一执行槽管理 |
| `UROAD_MIN_FREE_GB` | 默认 20，低于门槛不接纳新重型执行 |
| `UROAD_RETENTION_DAYS` | 默认 14，登记和清理必须使用一致保留期 |

环境样例仅设置当前 PowerShell 进程；独立服务和计划任务必须使用同一受控账户及对应环境。变更后按目标服务机制重新加载并核对，不能以交互式终端的变量推断调度进程已收到配置。

新任务由受控入口在隔离模式检查和修复固定白名单，只装二进制 wheel、固定版本并禁止额外依赖解析。只有入口真实返回修复失败才向用户报告业务部署错误。包内旧 Linux vendor 验证代码保留历史兼容接口，本仓库不分发 vendor 二进制；当前 Windows 环境使用受控解释器的固定依赖。
