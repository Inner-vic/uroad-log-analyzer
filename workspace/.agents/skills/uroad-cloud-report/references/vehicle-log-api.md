# 车云日志下载接口说明

## 固定环境映射与来源

uroad 与 CAN 云端下载均通过本 Skill 自有的 `scripts/cheyun_prod_client.py` 实现；凭据由 `UROAD_SETTINGS_PATH` 或用户受保护的配置目录提供。

配置字不在日志、运行产物或发布包中保存或输出。不得把真实配置字发送到聊天或写入 Git。

目标接口：

- prod: `https://dip-data-msg-parsing-service.prod.k8s.chehejia.com`
- test/testtwo: `https://dip-data-msg-parsing-service.ontest.k8s.chehejia.com`
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

`success:true,data:null` 是正常空结果。业务错误、缺少成功标记、结构异常不能当作空结果回退环境。

uroad 默认选择 `AVM_Service-*.zst`。CAN 直接流式链路按确定性顺序读取；Parquet 备用链路冻结原查询记录顺序，每份文件按任务和序号隔离下载并验证哈希，不按重名文件归属猜测来源。
测试环境下载前应先做只查询预检，从 `downloadURL` / `downloadUrl` / `url` 中仅提取 `hostname` 与协议；日志和聊天中不要输出完整签名 URL。
CAN EEA3 标准报告解析保留配置的 `0x12A`、`0x108`、`0x106`。Parquet 标准入口为 `scripts/parquet_can_signals.py`，使用有界批量和任务内 SQLite 产生兼容信号合同，不再把全部报文转换成内存 ASC；旧转换脚本仅用于兼容诊断。
完整时间范围并行一个 uroad 获取/分析通道与一个 CAN 获取/解码通道；两者完成并校验后汇合。任一通道失败，先排空已拥有的进程再记终态，不发布未校验报告。已有输入、结果、来源凭据和诊断留在当前任务内供受控恢复，并按统一保留期清理。

不使用 getlog、Gateway、浏览器自动化或历史 direct Bearer 模式。
