# 车云日志下载接口说明

## 部署环境映射

uroad 与 CAN 云端下载均通过本 Skill 自有的 `scripts/cheyun_prod_client.py` 实现，并读取本 Skill 的本地 `settings.json`。

配置字不在日志、运行产物或发布包中保存或输出。复制 `settings.json.example` 为 `settings.json` 后填写 `username`；若缺少配置字，请联系接口维护方获取，不要把真实配置字发送到聊天或写入 Git。

公开仓库不保存公司域名。部署前通过环境变量注入目标接口与下载白名单：

- `UROAD_PROD_ORIGIN`：生产查询服务 HTTPS origin；
- `UROAD_TEST_ORIGIN`：测试查询服务 HTTPS origin；
- `UROAD_PROD_DOWNLOAD_HTTPS_HOSTS` / `UROAD_TEST_DOWNLOAD_HTTPS_HOSTS`：逗号分隔的 HTTPS 下载域名白名单；
- `UROAD_PROD_DOWNLOAD_HTTP_HOSTS` / `UROAD_TEST_DOWNLOAD_HTTP_HOSTS`：仅兼容受控内网旧服务，默认留空。

源码中的 `example.com` 只用于测试和安全占位，不能作为生产配置。真实值由受控部署环境注入，不写入 Git、报告或聊天。

固定协议约定：
- path: `/v1-0/msg-parsing/hu-log-files/pagination`
- uroad logClass: `log_fsdA_service`
- CAN “报文数据” logClass: `msg_xcu`

对外运行模式支持 `auto` / `prod` / `test`：
- `prod`：固定生产环境，不回退
- `test`：固定测试环境；调用接口时环境名固定映射为 `testtwo`
- `auto`：先查 `prod`，仅当 `prod` 请求成功且响应结构有效，但目标数据为空或不完整时，才整次回退到 `test/testtwo`

同一份报告中的 uroad 与 CAN 必须来自同一环境，不能混用。认证失败、网络失败、HTTP 5xx、响应结构异常、配置缺失或接口明确报错时，不允许自动回退。

## 参数

- `vin`
- `collectTimeMin`
- `collectTimeMax`
- `logClass=log_fsdA_service`（uroad）或 `msg_xcu`（CAN）
- `remoteMode=false`
- `pageNo` / `pageSize`
- `eeaPlatform=EEA3.0`

## 响应

```json
{
  "success": true,
  "data": {
    "total": 24,
    "list": [
      {
        "fileName": "AVM_Service-...zst",
        "fileKey": "...",
        "downloadURL": "http://...",
        "fileSize": 1028099,
        "collectTime": "2026-06-12 16:05:50",
        "receiveTime": "2026-06-12 16:06:30"
      }
    ]
  }
}
```

uroad 默认选择 `AVM_Service-*.zst`；CAN 按 `collectTime`、文件名和 `fileKey` 确定性排序，逐个下载、流式解压和删除。
测试环境下载前应先做只查询预检，从 `downloadURL` / `downloadUrl` / `url` 中仅提取 `hostname` 与协议；日志和聊天中不要输出完整签名 URL。
CAN EEA3 解析只保留配置的 `0x12A`、`0x108`、`0x106`，不创建 ZIP 或 ASC。
同一十五分钟切片固定并发一个 uroad 获取/分析通道与一个 CAN 获取/流式解码通道；两个结果汇合后才持久化证据。
切片之间不并发，任一通道失败都会清理双方中间文件并阻止 HTML 生成。

不使用 getlog、Gateway、浏览器自动化或历史 direct Bearer 模式。
