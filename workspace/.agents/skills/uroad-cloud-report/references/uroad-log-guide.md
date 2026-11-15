# uroad 日志逐行解读与耗时计算指南

> **适用项目**：`uroad`（infer_expert_pc 进程）  
> **日志框架**：glog（`glog_vc_mod/logging.h`）  
> **前提**：`log.concise: false`（不开日志精简，所有 INFO/WARNING/ERROR 均输出）  
> **不可见日志**：`VLOG(1)`、`VLOG(2)` 不打印（需设置 `--v=1` 或 `--v=2` 才会输出）

---

## 一、日志格式说明

glog 每行日志格式：
```
LMMDD HH:MM:SS.ffffff PID TID file:line] message
```
- `L` = 级别字母（`I`=INFO, `W`=WARNING, `E`=ERROR）
- `MMDD` = 月日
- `HH:MM:SS.ffffff` = 时:分:秒.微秒
- `PID` = 进程 ID，`TID` = 线程 ID

示例：
```
I0623 16:22:38.123456 39732 39740 uroad_pre_node.cpp:1015] [uroad] UroadPreNode::run() timing(ms) ...
```

---

## 二、启动阶段日志（一次性，按执行顺序）

### 2.1 UroadPreNode 构造

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| 1 | INFO | `[uroad] UroadPreNode construct name: UroadPreNode` | 节点构造开始 |
| 2 | INFO | `[uroad] UroadPreNode: speed_gate enable=1 range=[0,120] kph inhibit=2s resume=1s` | 超速门限配置 |
| 3 | INFO | `[uroad] UroadPreNode: perception_gate enable=1 silent_states=[...] mask=0x...` | 感知状态门限 |
| 4 | INFO | `[uroad] UroadPreNode: apa_gate enable=1` | 泊车门限 |
| 5 | INFO | `[uroad] UroadPreNode: gear_gate enable=1` | 挡位门限 |
| 6 | INFO | `UroadPreNode: cfgwd vehProject=... wheel_base=...` | 车辆配置字解析（如有） |
| 7 | INFO | `[uroad] UroadPreNode: 回放模式=OFF` | 运行模式 |
| 8 | INFO | `[uroad] UroadPreNode: platform=AVM` | 平台类型 |
| 9 | INFO | `[uroad] UroadPreNode: infer_frame_interval=1` | 推理间隔 |
| 10 | INFO | `[uroad] UroadPreNode: avm_lidar_context_size=3` | 雷达上下文帧数 |

**可能出现的异常：**

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| ERROR | `[uroad] UroadPreNode: Failed to get CPU device!` | 设备获取失败，无法初始化 |
| WARNING | `[uroad] UroadPreNode: 公共配置加载失败，使用默认值. reason: ...` | YAML 解析错误 |
| WARNING | `[uroad] UroadPreNode: 前处理配置加载失败，使用默认值. reason: ...` | 前处理参数文件异常 |
| WARNING | `[uroad] UroadPreNode: perception_gate.silent_states[i]=v 超出 [0,15]，忽略` | 配置值非法 |

---

### 2.2 UroadPreNode 启动相机/雷达

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| 11 | INFO | `[uroad] Camera listeners: 1` | 相机监听注册完成 |
| 12 | INFO | `[uroad] UroadPreNode (AVM): Attempting to use XLIDAR CONAN PACKAGE (factory pattern)` | 开始初始化 EDSG 雷达 |
| 13 | INFO | `[uroad] UroadPreNode (AVM): Got EDSG factory, type: edsg` | 工厂获取成功 |
| 14 | INFO | `[uroad] UroadPreNode (AVM): Lidar receiver started successfully (xlidar EDSG)` | 雷达启动成功 |

**可能出现的异常：**

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| ERROR | `[uroad] UroadPreNode (AVM): EDSG factory not available in xlidar package` | xlidar 包无 EDSG 支持 |
| ERROR | `[uroad] UroadPreNode (AVM): Failed to create lidar receiver` | 接收器创建失败 |
| ERROR | `[uroad] UroadPreNode (AVM): Failed to start lidar receiver` | 接收器启动失败 |

---

### 2.3 UroadPostNode 构造

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| 15 | INFO | `[uroad] UroadPostNode construct name: UroadPostNode` | 节点构造开始 |
| 16 | INFO | `[uroad] UroadPostNode: loaded postprocess params from common + postprocess yaml` | 参数加载成功 |
| 17 | INFO | `[uroad] UroadPostNode: BEVSplatter initialized` | BEV 重建器就绪 |
| 18 | INFO | `UroadPostNode: cfgwd enabled, vehProject=...` | 车辆配置字（如有） |
| 19 | INFO | `[uroad] UroadPostNode: pc_traj disabled, pc_postprocess_manager_ skipped` | 点云轨迹模块状态 |
| 20 | INFO | `[uroad] UroadPostNode: post_input publish enabled` | DDS 发布开关 |
| 21 | INFO | `[uroad] UroadPostNode: wheel_tra publish enabled` | DDS 发布开关 |
| 22 | INFO | `[uroad] eval_dump disabled` | 评测转储关闭 |

**可能出现的异常：**

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| WARNING | `[uroad] UroadPostNode: failed to load postprocess params from ...` | 后处理参数文件缺失 |
| WARNING | `[uroad] UroadPostNode: Failed to get CPU device for vis output` | 可视化设备获取失败 |

---

### 2.4 PostprocessManager 初始化

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| 23 | INFO | `PostprocessManager: Constructed` | 管理器构造 |
| 24 | INFO | `PostprocessManager: Initializing (filter_only=0)` | 初始化开始 |
| 25 | INFO | `PostprocessManager: DataManager created (odom_queue=...)` | 数据管理器创建 |
| 26 | INFO | `PostprocessManager: replay_mode_enabled=0` | 回放模式状态 |
| 27 | INFO | `PostprocessManager: DDS subscribing OdomDataForPCM` | 订阅里程计 |
| 28 | INFO | `PostprocessManager: IMUDataForPCM reader created successfully` | IMU 订阅成功 |
| 29 | INFO | `PostprocessManager: INSInfo reader created successfully` | INS 订阅成功 |
| 30 | INFO | `PostprocessManager: SusPreviewInfo writer created` | 悬架发布器创建 |
| 31 | INFO | `PostprocessManager: Creating PostNodeInput publisher...` | 开始创建 post_input 发布 |
| 32 | INFO | `PostprocessManager: PostNodeInput topic created successfully` | topic 创建成功 |
| 33 | INFO | `PostprocessManager: PostNodeInput writer created successfully` | writer 创建成功 |
| 34 | INFO | `PostprocessManager: post_input publish thread started` | 发布线程启动 |
| 35 | INFO | `PostprocessManager: WheelTraMsg writers created (N topics)` | wheel_tra 发布器创建 |
| 36 | INFO | `PostprocessManager: wheel_tra publish thread started` | 发布线程启动 |
| 37 | INFO | `PostprocessManager: DDS participant and odom reader initialized` | DDS 初始化完成 |
| 38 | INFO | `PostprocessManager: Trajectory generation thread started (10ms period)` | 轨迹生成线程启动 |
| 39 | INFO | `PostprocessManager: Initialized successfully (replay/live mode)` | 初始化完毕 |
| 40 | INFO | `  - DDS subscribed: OdomDataForPCM, IMUDataForPCM` | 订阅确认 |
| 41 | INFO | `  - Waiting for DDS messages...` | 等待数据到来 |

**可能出现的异常：**

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| ERROR | `PostprocessManager: Failed to create DDS participant` | DDS 底层异常 |
| ERROR | `PostprocessManager: failed to create OdomDataForPCM topic` | topic 创建失败 |
| ERROR | `PostprocessManager: failed to create odom reader` | reader 创建失败 |
| ERROR | `PostprocessManager: failed to create SusPreviewInfo topic/writer` | 悬架发布创建失败 |
| ERROR | `PostprocessManager: failed to create PostNodeInput topic/writer` | post_input 发布失败 |
| ERROR | `PostprocessManager: failed to create WheelTraMsg topic/writer` | wheel_tra 发布失败 |
| WARNING | `PostprocessManager: failed to create IMUDataForPCM topic` | IMU topic 失败（可降级） |
| WARNING | `PostprocessManager: failed to create INSInfo topic` | INS topic 失败（可降级） |

---

### 2.5 子模块初始化

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| 42 | INFO | `[PointCloudProcessor] voxelization.use_sparse_output=true` | 体素化配置 |
| 43 | INFO | `[PointCloudProcessor] Loaded preprocess config from ...` | 前处理配置 |
| 44 | INFO | `PointCloudProcessor: Lidar extrinsic loaded from ...` | 雷达外参 |
| 45 | INFO | `[ImageProcessor] Loaded preprocess config: crop=[...]` | 图像裁剪配置 |
| 46 | INFO | `[ImageProcessor] Camera calibration loaded from ...` | 相机标定 |
| 47 | INFO | `PostprocessManager: Updated wheel positions - wheelbase=... ...` | 轮距参数 |
| 48 | INFO | `DdsListener: initialized (OdomDataForPCM / ...)` | DDS 监听器就绪 |

---

## 三、正常运行阶段日志（每帧重复）

### 3.1 UroadPreNode::run() — 一帧正常流程

每帧**必定出现**的日志（按顺序）：

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| ① | INFO | `[uroad] UroadPreNode::run() frame=5 \| speed=32.1kph \| gear=D \| func_id=1 \| perception_state=0` | 帧头信息 |
| ② | INFO | `[uroad] UroadPreNode::run() timing(ms) frame=5 \| cam_fetch=0.12 \| traj_gen=1.05 \| img_proc=3.21 \| deskew_vox=8.43 \| send=2.15 \| TOTAL=14.96 \| img_ts=1719148958123 \| cloud_pts=28456` | **前处理计时摘要** |

**各字段详解**：

| 字段 | 含义 | 典型值 |
|------|------|--------|
| `frame` | 帧序号（递增） | 0, 1, 2, ... |
| `speed` | 当前车速 (kph) | 0~120 |
| `gear` | 挡位 | D/R/P/N |
| `cam_fetch` | 从 CameraDataBus 取图耗时 | 0.1~0.5 ms |
| `traj_gen` | 轨迹生成（前后段分割）耗时 | 0.5~2 ms |
| `img_proc` | 图像缩放裁剪耗时 | 2~5 ms |
| `deskew_vox` | 点云去畸变 + shuffle + 体素化耗时 | 5~15 ms |
| `send` | SendTensors（setInputTensor memcpy）耗时 | 1~3 ms |
| `TOTAL` | **前处理总耗时** | 10~25 ms |
| `img_ts` | 图像时间戳 (ms) | 用于跨节点匹配 |
| `cloud_pts` | 有效点云数 | 10000~50000 |

---

### 3.2 UroadPostNode::run() — 一帧正常流程

每帧**必定出现**的日志（按顺序）：

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| ③ | INFO | `[uroad] UroadPostNode run frame=5 inputs=17` | 帧头（输入数量应为17） |
| ④ | INFO | `[uroad] UroadPostNode::run() timing(ms) frame=5 \| extract_pt=0.08 \| extract_infer=0.15 \| bev_splatting=3.21 \| reconstruct=0.45 \| pc_traj=1.12 \| postprocess=2.86 \| pub_input=0.23 \| pub_wheel=0.18 \| TOTAL=8.28` | **后处理计时摘要** |

**各字段详解**：

| 字段 | 含义 | 典型值 |
|------|------|--------|
| `frame` | 帧序号（与 PreNode 一一对应） | 同上 |
| `extract_pt` | 提取透传数据（轨迹/时间戳）耗时 | 0.05~0.2 ms |
| `extract_infer` | 提取推理输出（means3D/opacity/semantics）耗时 | 0.1~0.5 ms |
| `bev_splatting` | BEV 视图重建（OpenMP 并行）耗时 | 2~5 ms |
| `reconstruct` | 轨迹重建耗时 | 0.2~1 ms |
| `pc_traj` | 点云轨迹检测耗时 | 0.5~2 ms |
| `postprocess` | 后处理管线（Kalman + 融合 + 滤波）耗时 | 1~4 ms |
| `pub_input` | PostNodeInput DDS 入队耗时 | 0.1~0.5 ms |
| `pub_wheel` | WheelTraMsg DDS 入队耗时 | 0.1~0.5 ms |
| `TOTAL` | **后处理总耗时** | 5~15 ms |

---

### 3.3 每 10 帧出现的 FPS 日志

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| ⑤ | **WARNING** | `[uroad] UroadPostNode FPSlast10frames: 9.85 Hz` | 最近 10 帧平均帧率 |

> ⚠️ 这是 **WARNING 级别**（即使 `log.concise: true` 也可见）。正常值约 10 Hz。

---

### 3.4 PostprocessManager 周期日志（10ms 轨迹线程）

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| ⑥ | INFO | `PostprocessManager: transform+target+process cost: 2.35ms` | 轨迹处理子管线单次耗时 |
| ⑦ | INFO | `PostprocessManager: PublishWheelTraToDDS timestamp_ms=...` | wheel_tra 发布 |
| ⑧ | INFO | `[SusPreviewInfo] : time=... RoadType=...` | 悬架预览发布（按频率节流） |
| ⑨ | INFO | `PostprocessManager: post_input published frame=... front_xy=... back_xy=...` | 每 10 帧打印一次 |
| ⑩ | INFO | `PostprocessManager: wheel_tra published writer_index=... FL=...` | 每 10 条消息打印一次 |

---

### 3.5 雷达数据回调日志

| 级别 | 日志内容 | 说明 |
|------|----------|------|
| INFO | `[uroad] UroadPreNode (AVM): Receiving data from XLIDAR EDSG receiver (callback #50 packet size=... bytes)` | 每 50 帧打印一次，确认雷达链路正常 |

---

## 四、跳帧日志（某些条件不满足时出现）

当帧不满足处理条件时，PreNode 会跳过该帧，打印以下日志之一后 return：

| 级别 | 日志内容 | 触发条件 |
|------|----------|----------|
| INFO | `[uroad] UroadPreNode::run() 超速抑制中, skip frame=5` | 车速超出工作范围 |
| INFO | `[uroad] UroadPreNode::run() perception_gate 抑制中(state=3), skip frame=5` | 感知状态不满足 |
| INFO | `[uroad] UroadPreNode::run() apa_gate 抑制中(APA), skip frame=5` | 处于泊车模式 |
| INFO | `[uroad] UroadPreNode::run() gear_gate 抑制中(非D档), skip frame=5` | 非前进挡 |
| WARNING | `[uroad] UroadPreNode::run() Camera image is empty, skip frame=5` | 相机无图 |
| WARNING | `[uroad] UroadPreNode::run() trajectory too few pts=12 at img_ts=..., skip frame=5` | 轨迹点太少 |
| WARNING | `[uroad] UroadPreNode::run() Image processing failed, skip frame=5` | 图像处理失败 |
| WARNING | `[uroad] UroadPreNode::run() too few pts after Phase-2: 50, skip frame=5` | 体素化后点数不足 |

**跳帧时不会出现 timing 日志和后续 PostNode 日志。**

---

## 五、运行时异常日志

### 5.1 PostNode ERROR（推理输出异常 → 该帧处理终止）

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| ERROR | `UroadPostNode: Expected 17 inputs, got 15` | EDS 输入数不对（图配置问题） |
| ERROR | `UroadPostNode: Failed to extract pass-through data` | 透传数据解码失败 |
| ERROR | `UroadPostNode: Failed to extract inference output` | 推理输出解码失败 |
| ERROR | `UroadPostNode: Failed to run BEV splatting` | BEV 重建异常 |

### 5.2 解码类 ERROR（在 ExtractPassThroughData / ExtractInferenceOutput 中）

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| ERROR | `UroadPostNode: lidar_timestamp decode failed, size=0` | 雷达时间戳丢失 |
| ERROR | `UroadPostNode: N_passthrough decode failed` | 点数字段丢失 |
| ERROR | `UroadPostNode: traj_meta decode failed, size=8` | 轨迹元数据异常 |
| ERROR | `ExtractInferenceOutput: means3D decode failed, edge=...` | 推理输出边解码失败 |
| ERROR | `RunSplattingBEV: N_back=0` | 无后段轨迹点 |
| ERROR | `RunSplattingBEV: inference data empty` | 推理数据为空 |
| ERROR | `BEVSplatter failed: ...` | BEV 异常（含 exception 信息） |

### 5.3 PostprocessManager 运行时 WARNING

| 级别 | 日志内容 | 含义 | 严重程度 |
|------|----------|------|----------|
| WARNING | `trajectory_thread severe jitter: interval=...us` | 轨迹线程严重抖动 | 高 |
| WARNING | `trajectory_thread overrun: skipped_ticks=2` | 轨迹线程超时跳拍 | 高 |
| WARNING | `PostprocessManager: Trajectory data timeout (...)` | 数据超时 | 中 |
| WARNING | `PostprocessManager: post_input queue full, dropping frame=...` | 队列满丢帧 | 中 |
| WARNING | `PostprocessManager: wheel_tra queue full, dropping writer_index=...` | 队列满丢消息 | 中 |
| WARNING | `PostprocessManager: No odom data yet, skipping publish` | 里程计未就绪 | 低（启动期正常） |
| WARNING | `PostprocessManager: Failed to publish SusPreviewInfo` | DDS 发布失败 | 中 |

### 5.4 数据一致性 WARNING

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| WARNING | `Back trajectory size mismatch: expected=200, got=196` | 后段轨迹长度异常 |
| WARNING | `Front trajectory size mismatch: expected=80, got=76` | 前段轨迹长度异常 |
| WARNING | `ReconstructFullTrajectory: inference_visual_heads_ size=3 < 5` | 视觉头数据不完整 |
| WARNING | `ExtractPcTrajectory: pc_traj_back_z too small, skip` | 点云 z 值数据不足 |

### 5.5 门限状态切换（运行时事件）

| 级别 | 日志内容 | 含义 |
|------|----------|------|
| WARNING | `[uroad] UroadPreNode: perception_gate 抑制, perception_state=3` | 进入抑制 |
| INFO | `[uroad] UroadPreNode: perception_gate 恢复, perception_state=0` | 恢复正常 |
| WARNING | `[uroad] UroadPreNode: apa_gate 抑制, ParkFunctionMode=2` | APA 抑制 |
| INFO | `[uroad] UroadPreNode: apa_gate 恢复, ParkFunctionMode=Invalid(0)` | APA 恢复 |
| WARNING | `[uroad] UroadPreNode: gear_gate 抑制, ActuGearShiftPos=R` | 非 D 挡 |
| INFO | `[uroad] UroadPreNode: gear_gate 恢复, D档` | 回到 D 挡 |
| WARNING | `[uroad] UroadPreNode: Invalid steering angle: nan` | 转向角数据异常 |
| WARNING | `[uroad] UroadPreNode (AVM): ConvertRawToPointCloud returned empty cloud` | 雷达空点云 |

---

## 六、退出阶段日志

| 序号 | 级别 | 日志内容 | 说明 |
|------|------|----------|------|
| 1 | INFO | `[uroad] Stop lidar` | 雷达停止 |
| 2 | INFO | `[uroad] UroadPreNode: AVM lidar receiver (xlidar) stopped` | EDSG 接收器停止 |
| 3 | INFO | `PostprocessManager: Trajectory generation thread stopped` | 轨迹线程退出 |
| 4 | INFO | `PostprocessManager: post_input publish thread stopped` | 发布线程退出 |
| 5 | INFO | `PostprocessManager: wheel_tra publish thread stopped` | 发布线程退出 |
| 6 | INFO | `PostprocessManager: Destructed` | 管理器析构 |
| 7 | INFO | `[uroad] UroadPostNode destruct` | 后处理节点析构 |
| 8 | INFO | `[uroad] UroadPreNode destruct` | 前处理节点析构 |

---

## 七、正常 Pipeline 完整一帧日志示例

以下是 **一帧正常处理** 时应看到的完整日志序列（frame=5 为例）：

```log
I0623 16:22:38.000123 39732 39740 uroad_pre_node.cpp:811] [uroad] UroadPreNode::run() frame=5 | speed=32.1kph | gear=D | func_id=1 | perception_state=0
I0623 16:22:38.015089 39732 39740 uroad_pre_node.cpp:1015] [uroad] UroadPreNode::run() timing(ms) frame=5 | cam_fetch=0.12 | traj_gen=1.05 | img_proc=3.21 | deskew_vox=8.43 | send=2.15 | TOTAL=14.96 | img_ts=1719148958123 | cloud_pts=28456
I0623 16:22:38.095234 39732 39742 uroad_post_node.cpp:246] [uroad] UroadPostNode run frame=5 inputs=17
I0623 16:22:38.103456 39732 39742 uroad_post_node.cpp:384] [uroad] UroadPostNode::run() timing(ms) frame=5 | extract_pt=0.08 | extract_infer=0.15 | bev_splatting=3.21 | reconstruct=0.45 | pc_traj=1.12 | postprocess=2.86 | pub_input=0.23 | pub_wheel=0.18 | TOTAL=8.28
```

每 10 帧额外出现：
```log
W0623 16:22:38.103500 39732 39742 uroad_post_node.cpp:242] [uroad] UroadPostNode FPSlast10frames: 9.85 Hz
```

轨迹线程（10ms 周期，独立线程）穿插出现：
```log
I0623 16:22:38.010000 39732 39745 postprocess_manager.cpp:1486] [uroad] PostprocessManager: transform+target+process cost: 2.35ms
```

---

## 八、耗时计算方法

### 8.1 前处理耗时

**直接读取**：
```
前处理耗时 = PreNode timing 日志中的 TOTAL 字段
```

示例：`TOTAL=14.96` → 前处理耗时 = **14.96 ms**

细分：
- 图像处理耗时 = `img_proc`
- 点云处理耗时 = `deskew_vox`
- Tensor 发送耗时 = `send`

---

### 8.2 后处理耗时

**直接读取**：
```
后处理耗时 = PostNode timing 日志中的 TOTAL 字段
```

示例：`TOTAL=8.28` → 后处理耗时 = **8.28 ms**

细分：
- BEV 重建耗时 = `bev_splatting`
- 滤波+融合耗时 = `postprocess`
- DDS 发布耗时 = `pub_input` + `pub_wheel`

---

### 8.3 推理耗时（NPU）

**间接计算**：前处理结束到后处理开始之间的间隔即为推理（NPU 运算 + EDS 调度）耗时。

```
推理耗时 = T_post_start − T_pre_end
```

其中：
- `T_pre_end` = PreNode timing 日志的 glog 时间戳（该日志在 run() 末尾打印，标志前处理结束）
- `T_post_start` = PostNode `run frame=N inputs=17` 日志的 glog 时间戳（标志后处理开始）

**示例计算**：
```
T_pre_end  = 16:22:38.015089  (PreNode timing 日志时间)
T_post_start = 16:22:38.095234  (PostNode run frame= 日志时间)

推理耗时 = 095234 − 015089 = 80.145 ms
```

---

### 8.4 端到端帧耗时

```
端到端耗时 = 前处理 + 推理 + 后处理
           = PreNode.TOTAL + 推理耗时 + PostNode.TOTAL
```

或通过 FPS 估算：
```
平均帧耗时 ≈ 1000 / FPS (ms)
```

---

### 8.5 Python 解析脚本

```python
#!/usr/bin/env python3
"""解析 uroad 日志，计算每帧前处理/推理/后处理耗时"""
import re, sys
from datetime import datetime

def parse_ts(line):
    """解析 glog 时间戳 → datetime"""
    m = re.match(r'[IWE](\d{4}) (\d{2}:\d{2}:\d{2}\.\d{6})', line)
    if not m:
        return None
    return datetime.strptime(f"2026{m.group(1)} {m.group(2)}", "%Y%m%d %H:%M:%S.%f")

def extract_field(line, field):
    """提取 key=value 中的数值"""
    m = re.search(rf'{field}=([\d.]+)', line)
    return float(m.group(1)) if m else None

def main(log_file):
    pre_data = {}   # frame -> (timestamp, total_ms)
    post_start = {} # frame -> timestamp
    post_data = {}  # frame -> (timestamp, total_ms)

    with open(log_file) as f:
        for line in f:
            if "UroadPreNode::run() timing" in line:
                ts = parse_ts(line)
                frame = extract_field(line, 'frame')
                total = extract_field(line, 'TOTAL')
                if frame is not None and ts:
                    pre_data[int(frame)] = (ts, total)

            elif "UroadPostNode run frame=" in line and "inputs=" in line:
                ts = parse_ts(line)
                frame = extract_field(line, 'frame')
                if frame is not None and ts:
                    post_start[int(frame)] = ts

            elif "UroadPostNode::run() timing" in line:
                ts = parse_ts(line)
                frame = extract_field(line, 'frame')
                total = extract_field(line, 'TOTAL')
                if frame is not None and ts:
                    post_data[int(frame)] = (ts, total)

    print(f"{'Frame':<7} {'Pre(ms)':<9} {'Infer(ms)':<10} {'Post(ms)':<9} {'E2E(ms)':<9}")
    print("-" * 45)

    frames = sorted(set(pre_data) & set(post_start) & set(post_data))
    for frame in frames:
        pre_ts, pre_total = pre_data[frame]
        post_ts = post_start[frame]
        _, post_total = post_data[frame]

        infer_ms = (post_ts - pre_ts).total_seconds() * 1000
        e2e = pre_total + infer_ms + post_total

        print(f"{frame:<7} {pre_total:<9.2f} {infer_ms:<10.2f} {post_total:<9.2f} {e2e:<9.2f}")

if __name__ == "__main__":
    main(sys.argv[1])
```

**使用方法**：
```bash
python3 parse_uroad_timing.py /path/to/uroad.log
```

---

### 8.6 快速 Shell 命令

```bash
# 提取前处理 TOTAL
grep "UroadPreNode::run() timing" uroad.log | grep -oP 'TOTAL=\K[\d.]+'

# 提取后处理 TOTAL
grep "UroadPostNode::run() timing" uroad.log | grep -oP 'TOTAL=\K[\d.]+'

# 提取 FPS
grep "FPSlast10frames" uroad.log | grep -oP '[\d.]+ Hz'

# 统计前处理平均耗时
grep "UroadPreNode::run() timing" uroad.log | grep -oP 'TOTAL=\K[\d.]+' | \
  awk '{sum+=$1; n++} END {printf "avg=%.2f ms (n=%d)\n", sum/n, n}'

# 统计后处理平均耗时
grep "UroadPostNode::run() timing" uroad.log | grep -oP 'TOTAL=\K[\d.]+' | \
  awk '{sum+=$1; n++} END {printf "avg=%.2f ms (n=%d)\n", sum/n, n}'
```

---

## 九、日志排查流程图

```
日志中是否有 "UroadPreNode::run() timing" ？
├── 否 → 检查是否有跳帧日志（"skip frame"/"抑制中"）
│         ├── 有 → 查看跳帧原因（速度/挡位/感知状态/空图/点数不足）
│         └── 无 → PreNode 未被 EDS 调度，检查启动日志是否有 ERROR
│
└── 是 → 检查是否有对应 "UroadPostNode::run() timing" ？
          ├── 否 → 推理失败或 PostNode ERROR
          │         └── 搜索 "LOG(ERROR)" 定位具体原因
          │
          └── 是 → ✅ 正常帧
                    ├── 计算推理耗时 = PostNode_start_ts − PreNode_timing_ts
                    ├── 检查 FPS 是否正常（~10 Hz）
                    └── 检查有无 WARNING（jitter/overrun/queue full）
```
