# 开发指南

## 1. 本地环境

- Git；
- [uv](https://docs.astral.sh/uv/)；
- CPython 3.9～3.14；建议本地和 CI 使用 3.12；
- 真实线上联调另需获授权的 OpenClaw、车云与飞书环境。

Windows 自带的 `python.exe` 可能只是 Microsoft Store 占位程序。本仓库统一通过 `uv run --no-cache python` 执行。

## 2. 目录

```text
app/
  uroad-cloud-report/   当前核心应用
  supervisor/           路由核心原型
  ad-p99-analysis/      可选实验能力
docs/                   当前文档
scripts/verify.py       统一验证入口
```

本机根目录中被 `.gitignore` 排除的历史代码、运行数据和制品不是开发入口。

## 3. 配置

从示例创建本地文件：

```powershell
Copy-Item app/uroad-cloud-report/settings.json.example app/uroad-cloud-report/settings.json
```

填写配置后不要执行 `git add -f`。查询端点通过 `UROAD_PROD_ORIGIN`、`UROAD_TEST_ORIGIN` 和下载域名白名单环境变量注入；真实值只能保存在受控主机或秘密管理系统中。AD P99 使用外部 `lark-cli` 身份，接任者必须以自己的账号重新授权。

## 4. 测试

统一运行：

```powershell
uv run --no-cache --with pytest --with tzdata --with openpyxl==3.1.5 python scripts/verify.py
```

`tzdata` 是 Windows 运行 `Asia/Shanghai` 测试的开发依赖；目标 Linux 通常使用系统时区数据库。测试不访问真实车云或飞书。

## 5. 修改路径

- 请求、查重、状态或 worker：先读 `task_registry.py`、`task_worker.py` 及其测试；
- 环境回退、切片和清理：先读 `run_uroad_pipeline.py`、`process_time_chunks.py`；
- 报告字段或交互：同时检查 `build_html_report.py`、模板、artifact 校验和脱敏边界；
- 路由规则：同时修改 Supervisor 测试，确保一条消息最多命中一个业务路由；
- AD P99：保持表格只读，不新增导出、下载或隐式身份切换。

## 6. 完成标准

提交前至少确认：

1. 相关测试先失败后通过；
2. 统一验证通过；
3. `git diff --check` 无格式问题；
4. 没有真实配置、VIN、公司域名、运行目录或缓存进入暂存区；
5. 受影响的功能/设计/部署/运维文档已同步；
6. 变更被描述为“代码完成、测试完成、部署完成、验收完成”中的准确层级。
