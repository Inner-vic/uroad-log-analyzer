# uroad 云端日志自然语言解析与可视化展示需求文档

## 1. 需求目标

前端需要处理一份**混合云端日志**。这份日志里会同时包含多个应用、多个线程、多个模块的输出，`uroad` 只是其中一部分。

本需求不是让前端“原样展示 uroad 日志”，而是要求前端做到两件事：

1. **先从混合日志中识别并抽取 uroad 相关内容**
2. **把技术日志翻译成用户易懂的运行状态、问题说明、耗时结论和图表数据**

最终用户看到的应该是：

- “uroad 已正常启动”
- “第 125 帧前处理正常，但推理耗时偏高”
- “当前有连续跳帧，原因是挡位不是 D 挡”
- “最近 10 帧 FPS 下降到 7.8，性能异常”

而不是一大段原始日志文本。

---

## 2. 前端最终要交付什么

前端页面应把日志解析成**可读结论**，而不是仅提供关键词检索。

建议最终呈现 4 类信息：

### 2.1 总体运行结论

用自然语言概括当前日志中的 uroad 运行状态，例如：

- `uroad 已成功启动，运行期间共处理 286 帧，其中 260 帧正常，18 帧跳过，8 帧异常。`
- `uroad 启动成功，但运行中存在性能抖动，最近 FPS 最低为 7.8 Hz。`
- `uroad 未完整启动，PostprocessManager 初始化失败。`

### 2.2 时间线事件流

按时间顺序，把关键日志翻译成“事件卡片”或“运行时间线”：

- `16:22:37.998 uroad 前处理节点启动`
- `16:22:38.003 雷达接收器启动成功`
- `16:22:38.015 第 5 帧前处理完成，用时 14.96 ms`
- `16:22:38.095 第 5 帧进入后处理，推理耗时 80.15 ms`
- `16:22:38.103 第 5 帧后处理完成，用时 8.28 ms，端到端 103.39 ms`
- `16:22:38.500 检测到挡位抑制，后续帧被跳过`

### 2.3 图表化性能信息

至少支持：

- 前处理耗时曲线
- 推理耗时曲线
- 后处理耗时曲线
- 端到端耗时曲线
- FPS 趋势图
- 跳帧/异常数量统计图

### 2.4 问题说明区

把 ERROR / WARNING 翻译成适合非日志阅读者理解的描述，例如：

| 原始日志                                | 页面展示文案                                                 |
| --------------------------------------- | ------------------------------------------------------------ |
| `Expected 17 inputs, got 15`          | `后处理输入数量异常，当前帧推理结果不完整，无法继续处理。` |
| `Camera image is empty, skip frame=5` | `第 5 帧被跳过：相机图像为空。`                            |
| `trajectory_thread severe jitter`     | `后处理轨迹线程抖动严重，可能影响输出稳定性。`             |
| `gear_gate 抑制中(非D档)`             | `当前处于非 D 挡，uroad 暂停处理。`                        |

---

## 3. 页面展示风格要求

本需求要求展示风格偏**产品化说明**，而不是“日志分析工具风格”。

### 3.1 页面默认展示“结论”

用户进入页面后，第一屏应该优先看到：

- 当前是否正常运行
- 是否已启动成功
- 当前是否有异常
- 当前性能是否正常
- 最近跳帧原因是什么

不应第一屏直接展示大量原始日志行。

### 3.2 原始日志只能作为辅助信息

原始日志建议放到二级区域，例如：

- “查看原始日志”
- “展开原文”
- “定位日志上下文”

原始日志不是主内容，主内容应是**翻译后的业务语义**。

### 3.3 统一用“人话”表达

页面文案应避免直接把日志 message 原封不动暴露给用户。

例如：

- 不推荐：`perception_gate 抑制中(state=3)`
- 推荐：`当前感知状态不满足运行条件，uroad 暂停处理。`
- 不推荐：`too few pts after Phase-2`
- 推荐：`点云有效点数不足，本帧无法继续处理。`

---

## 4. 日志来源特点与解析前提

### 4.1 日志是混合的

云端原始日志中可能同时包含：

- uroad
- 其他模型服务
- 系统服务
- 中间件
- 设备驱动
- 其他应用输出

因此前端必须先完成：

**从混合日志中抽取 uroad 子集**

再基于 uroad 子集做状态翻译和图表展示。

### 4.2 不能要求日志预清洗

前端不能假设后端已提前过滤出“纯 uroad 日志”。

第一版前端就应具备：

- 从完整日志中识别 uroad
- 忽略无关应用日志
- 在多线程交错场景下维持正确帧关联

### 4.3 抽取原则

抽取目标是**高准确率**。

要求：

- 宁可少量漏掉边缘日志，也不要把大量非 uroad 日志误识别进来
- 不能因为其他应用日志插入而打断 uroad 帧级状态分析

---

## 5. 前端需要识别哪些 uroad 日志

以下内容应被视为 uroad 相关：

- `[uroad] UroadPreNode ...`
- `[uroad] UroadPostNode ...`
- `PostprocessManager: ...`
- `[PointCloudProcessor] ...`
- `[ImageProcessor] ...`
- `DdsListener: ...`
- `[SusPreviewInfo] ...`
- `ExtractInferenceOutput: ...`
- `RunSplattingBEV: ...`
- `BEVSplatter failed: ...`

建议先按上述关键词筛出候选行，再做分类解析。

---

## 6. 前端要把哪些内容“翻译成人话”

这是本需求的核心。

前端不是只做字段提取，而是要把日志解析成以下 5 类易懂信息。

### 6.1 启动状态

把启动相关日志翻译成：

- `前处理节点已启动`
- `后处理节点已启动`
- `雷达接收器启动成功`
- `后处理管理器初始化成功`
- `uroad 启动完成`
- `uroad 启动失败：DDS 初始化异常`

### 6.2 帧处理过程

把一帧日志翻译成用户能读懂的过程说明，例如：

- `第 5 帧开始处理，车速 32.1 kph，挡位 D，感知状态正常。`
- `第 5 帧前处理完成：图像处理 3.21 ms，点云处理 8.43 ms，总耗时 14.96 ms。`
- `第 5 帧推理完成：推理阶段耗时 80.15 ms。`
- `第 5 帧后处理完成：BEV 重建 3.21 ms，后处理 2.86 ms，总耗时 8.28 ms。`
- `第 5 帧端到端总耗时 103.39 ms。`

### 6.3 跳帧原因

把 skip 日志翻译成清晰原因，例如：

- `第 12 帧被跳过：当前车速超出 uroad 工作范围。`
- `第 15 帧被跳过：当前感知状态不满足运行条件。`
- `第 18 帧被跳过：当前处于泊车模式。`
- `第 20 帧被跳过：当前不是 D 挡。`
- `第 21 帧被跳过：相机图像为空。`
- `第 22 帧被跳过：轨迹点数量不足。`
- `第 23 帧被跳过：图像处理失败。`
- `第 24 帧被跳过：点云有效点数不足。`

### 6.4 异常与风险

把异常日志翻译成面向问题定位的说明，例如：

- `第 35 帧后处理失败：收到的推理输入数量不正确。`
- `第 36 帧后处理失败：无法解析推理输出。`
- `BEV 重建失败，本帧无法生成有效结果。`
- `后处理轨迹线程出现严重抖动，可能导致输出不稳定。`
- `轨迹数据超时，当前输出可能存在延迟。`
- `发布队列已满，部分结果被丢弃。`

### 6.5 运行趋势

把离散日志汇总成整体趋势结论，例如：

- `最近 10 帧平均 FPS 为 9.85，运行基本正常。`
- `推理耗时持续高于 80 ms，当前瓶颈主要在推理阶段。`
- `前处理整体稳定，主要耗时集中在点云去畸变和体素化。`
- `最近 30 帧中有 12 帧因非 D 挡被跳过。`

---

## 7. 推荐页面结构

## 7.1 概览卡片区

建议至少展示以下卡片：

- `运行状态`：正常 / 异常 / 未启动完成
- `启动状态`：已完成 / 失败 / 部分完成
- `总帧数`
- `正常帧数`
- `跳帧数`
- `异常帧数`
- `平均端到端耗时`
- `最新 FPS`
- `当前主要问题`

其中“当前主要问题”应直接显示结论，例如：

- `当前主要问题：连续跳帧，原因是非 D 挡`
- `当前主要问题：推理耗时偏高`
- `当前主要问题：PostprocessManager 队列满，出现丢帧`

## 7.2 运行时间线

按时间顺序展示关键事件，事件文案必须为自然语言。

事件类型包括：

- 启动成功/失败
- 帧开始处理
- 帧前处理完成
- 帧后处理完成
- 跳帧
- ERROR
- WARNING
- FPS 波动
- 退出

## 7.3 帧级详情表

表格展示可以保留技术字段，但要补一个“状态说明”列。

建议字段：

| 字段          | 说明               |
| ------------- | ------------------ |
| frame         | 帧号               |
| frame_status  | 正常 / 跳过 / 异常 |
| human_summary | 人话总结           |
| speed_kph     | 车速               |
| gear          | 挡位               |
| pre_total_ms  | 前处理             |
| infer_ms      | 推理               |
| post_total_ms | 后处理             |
| e2e_ms        | 端到端             |
| skip_reason   | 跳帧原因编码       |
| error_reason  | 异常原因编码       |

其中 `human_summary` 例如：

- `正常完成，推理耗时偏高`
- `已跳过：当前不是 D 挡`
- `后处理失败：推理输出解析失败`

## 7.4 性能图表区

建议提供：

1. 帧耗时堆叠图或折线图
2. FPS 折线图
3. 跳帧原因分布图
4. 异常类型分布图

## 7.5 原始日志辅助区

仅用于：

- 查看原文
- 跳转定位
- 开发联调

该区域不应替代上面的自然语言结论区。

---

## 8. 关键日志到页面文案的映射要求

以下是前端必须支持的“日志翻译”规则。

## 8.1 启动阶段

| 日志特征                                         | 页面文案                                           |
| ------------------------------------------------ | -------------------------------------------------- |
| `UroadPreNode construct`                       | `uroad 前处理节点已创建。`                       |
| `speed_gate enable=`                           | `已加载速度门限配置。`                           |
| `perception_gate enable=`                      | `已加载感知状态门限配置。`                       |
| `apa_gate enable=1`                            | `已加载泊车抑制配置。`                           |
| `gear_gate enable=1`                           | `已加载挡位抑制配置。`                           |
| `platform=AVM`                                 | `当前运行平台为 AVM。`                           |
| `infer_frame_interval=`                        | `已加载推理帧间隔配置。`                         |
| `Camera listeners:`                            | `相机监听已就绪。`                               |
| `Lidar receiver started successfully`          | `雷达接收器启动成功。`                           |
| `UroadPostNode construct`                      | `uroad 后处理节点已创建。`                       |
| `BEVSplatter initialized`                      | `BEV 重建模块初始化成功。`                       |
| `PostprocessManager: Initialized successfully` | `后处理管理器初始化成功，uroad 已具备运行条件。` |

## 8.2 启动异常

| 日志特征                             | 页面文案                                             |
| ------------------------------------ | ---------------------------------------------------- |
| `Failed to get CPU device`         | `启动失败：无法获取 CPU 设备。`                    |
| `公共配置加载失败`                 | `启动告警：公共配置加载失败，当前使用默认配置。`   |
| `前处理配置加载失败`               | `启动告警：前处理配置加载失败，当前使用默认配置。` |
| `EDSG factory not available`       | `启动失败：雷达工厂不可用。`                       |
| `Failed to create lidar receiver`  | `启动失败：无法创建雷达接收器。`                   |
| `Failed to start lidar receiver`   | `启动失败：雷达接收器启动失败。`                   |
| `failed to create DDS participant` | `启动失败：DDS 参与者创建失败。`                   |
| `failed to create .* topic`        | `启动失败：DDS Topic 创建失败。`                   |
| `failed to create .* writer`       | `启动失败：DDS Writer 创建失败。`                  |
| `failed to create .* reader`       | `启动失败：DDS Reader 创建失败。`                  |

## 8.3 正常帧运行

### PreNode 帧头

原始日志：

```text
[uroad] UroadPreNode::run() frame=5 | speed=32.1kph | gear=D | func_id=1 | perception_state=0
```

建议翻译：

`第 5 帧开始处理，当前车速 32.1 kph，挡位 D，感知状态正常。`

### PreNode timing

原始日志：

```text
[uroad] UroadPreNode::run() timing(ms) frame=5 | cam_fetch=0.12 | traj_gen=1.05 | img_proc=3.21 | deskew_vox=8.43 | send=2.15 | TOTAL=14.96 | img_ts=1719148958123 | cloud_pts=28456
```

建议翻译：

`第 5 帧前处理完成，总耗时 14.96 ms，其中图像处理 3.21 ms，点云处理 8.43 ms，发送 Tensor 2.15 ms。`

### PostNode 帧头

原始日志：

```text
[uroad] UroadPostNode run frame=5 inputs=17
```

建议翻译：

`第 5 帧进入后处理阶段，推理输出已返回。`

### PostNode timing

原始日志：

```text
[uroad] UroadPostNode::run() timing(ms) frame=5 | extract_pt=0.08 | extract_infer=0.15 | bev_splatting=3.21 | reconstruct=0.45 | pc_traj=1.12 | postprocess=2.86 | pub_input=0.23 | pub_wheel=0.18 | TOTAL=8.28
```

建议翻译：

`第 5 帧后处理完成，总耗时 8.28 ms，其中 BEV 重建 3.21 ms，后处理 2.86 ms。`

### FPS

原始日志：

```text
[uroad] UroadPostNode FPSlast10frames: 9.85 Hz
```

建议翻译：

`最近 10 帧平均帧率为 9.85 Hz，运行基本正常。`

---

## 9. 跳帧日志翻译规则

| 日志特征                                 | 页面文案                                      |
| ---------------------------------------- | --------------------------------------------- |
| `超速抑制中, skip frame=`              | `该帧已跳过：当前车速超出 uroad 工作范围。` |
| `perception_gate 抑制中`               | `该帧已跳过：当前感知状态不满足运行条件。`  |
| `apa_gate 抑制中`                      | `该帧已跳过：当前处于泊车模式。`            |
| `gear_gate 抑制中`                     | `该帧已跳过：当前不是 D 挡。`               |
| `Camera image is empty, skip frame=`   | `该帧已跳过：相机图像为空。`                |
| `trajectory too few pts=`              | `该帧已跳过：轨迹点数量不足。`              |
| `Image processing failed, skip frame=` | `该帧已跳过：图像处理失败。`                |
| `too few pts after Phase-2`            | `该帧已跳过：点云有效点数不足。`            |

如果能提取 frame，则文案中应带帧号，例如：

`第 21 帧已跳过：相机图像为空。`

---

## 10. 异常日志翻译规则

## 10.1 帧级 ERROR

| 日志特征                                  | 页面文案                                       |
| ----------------------------------------- | ---------------------------------------------- |
| `Expected 17 inputs, got`               | `当前帧后处理失败：收到的输入数量不正确。`   |
| `Failed to extract pass-through data`   | `当前帧后处理失败：透传数据解析失败。`       |
| `Failed to extract inference output`    | `当前帧后处理失败：推理输出解析失败。`       |
| `Failed to run BEV splatting`           | `当前帧后处理失败：BEV 重建失败。`           |
| `lidar_timestamp decode failed`         | `当前帧处理失败：雷达时间戳解析失败。`       |
| `N_passthrough decode failed`           | `当前帧处理失败：透传点数解析失败。`         |
| `traj_meta decode failed`               | `当前帧处理失败：轨迹元数据解析失败。`       |
| `means3D decode failed`                 | `当前帧处理失败：推理输出关键字段解析失败。` |
| `RunSplattingBEV: N_back=0`             | `当前帧处理失败：后段轨迹为空。`             |
| `RunSplattingBEV: inference data empty` | `当前帧处理失败：推理结果为空。`             |
| `BEVSplatter failed:`                   | `当前帧处理失败：BEV 重建模块执行异常。`     |

## 10.2 WARNING

| 日志特征                               | 页面文案                                         |
| -------------------------------------- | ------------------------------------------------ |
| `trajectory_thread severe jitter`    | `后处理轨迹线程抖动严重，可能影响结果稳定性。` |
| `trajectory_thread overrun`          | `后处理轨迹线程执行超时，存在跳拍风险。`       |
| `Trajectory data timeout`            | `轨迹数据获取超时，当前输出可能延迟。`         |
| `post_input queue full`              | `后处理输入发布队列已满，部分帧被丢弃。`       |
| `wheel_tra queue full`               | `轮迹发布队列已满，部分消息被丢弃。`           |
| `No odom data yet, skipping publish` | `当前尚未收到里程计数据，部分发布被跳过。`     |
| `Failed to publish SusPreviewInfo`   | `悬架预览信息发布失败。`                       |
| `Back trajectory size mismatch`      | `后段轨迹长度异常，结果可能不完整。`           |
| `Front trajectory size mismatch`     | `前段轨迹长度异常，结果可能不完整。`           |
| `inference_visual_heads_ size=`      | `视觉头数据不完整，重建结果可能受影响。`       |
| `pc_traj_back_z too small`           | `点云轨迹高度信息不足，已跳过相关处理。`       |

---

## 11. 运行状态事件翻译规则

以下日志不一定是错误，但需要展示为“运行状态变化”：

| 日志特征                                        | 页面文案                               |
| ----------------------------------------------- | -------------------------------------- |
| `perception_gate 抑制`                        | `感知状态进入抑制，uroad 暂停处理。` |
| `perception_gate 恢复`                        | `感知状态恢复，uroad 恢复处理。`     |
| `apa_gate 抑制`                               | `进入泊车模式，uroad 暂停处理。`     |
| `apa_gate 恢复`                               | `已退出泊车模式，uroad 恢复处理。`   |
| `gear_gate 抑制`                              | `当前不是 D 挡，uroad 暂停处理。`    |
| `gear_gate 恢复`                              | `已恢复 D 挡，uroad 恢复处理。`      |
| `Invalid steering angle: nan`                 | `检测到异常转向角数据。`             |
| `ConvertRawToPointCloud returned empty cloud` | `当前雷达点云为空。`                 |

---

## 12. 耗时与图表数据要求

虽然页面主要展示自然语言，但前端仍需提取关键性能字段，供图表和结论生成使用。

## 12.1 前处理耗时

来源：

- `UroadPreNode::run() timing` 中的 `TOTAL`

页面可展示：

- 数值：`14.96 ms`
- 文案：`前处理耗时 14.96 ms`

## 12.2 后处理耗时

来源：

- `UroadPostNode::run() timing` 中的 `TOTAL`

页面可展示：

- 数值：`8.28 ms`
- 文案：`后处理耗时 8.28 ms`

## 12.3 推理耗时

计算方式：

```text
推理耗时 = PostNode 帧头时间 - PreNode timing 时间
```

页面可展示：

- 数值：`80.15 ms`
- 文案：`推理阶段耗时 80.15 ms`

## 12.4 端到端耗时

计算方式：

```text
端到端耗时 = 前处理 + 推理 + 后处理
```

页面可展示：

- 数值：`103.39 ms`
- 文案：`端到端总耗时 103.39 ms`

## 12.5 图表数据

前端应从日志中生成以下序列：

- `frame -> pre_total_ms`
- `frame -> infer_ms`
- `frame -> post_total_ms`
- `frame -> e2e_ms`
- `time/frame -> fps`
- `skip_reason -> count`
- `error_reason -> count`

---

## 13. 帧级人话总结生成要求

每一帧建议生成一条 `human_summary`，供列表或详情页直接使用。

示例规则：

### 13.1 正常帧

`第 5 帧处理完成，前处理 14.96 ms，推理 80.15 ms，后处理 8.28 ms，端到端 103.39 ms。`

如果推理明显偏高，还应支持增强描述：

`第 5 帧处理完成，但推理耗时偏高，是本帧主要耗时来源。`

### 13.2 跳帧

`第 12 帧已跳过：当前不是 D 挡。`

### 13.3 异常帧

`第 35 帧处理失败：推理输出解析失败。`

---

## 14. 总结文案生成要求

前端应能根据整段日志自动生成顶部总结。

示例：

### 14.1 正常运行总结

`uroad 已成功启动并持续运行。日志中共识别 286 帧，其中 260 帧正常完成，18 帧被跳过，8 帧处理异常。平均端到端耗时 98.6 ms，最新 FPS 为 9.85 Hz。`

### 14.2 性能异常总结

`uroad 已启动，但运行性能存在波动。推理耗时整体偏高，最近 10 帧 FPS 下降到 7.8 Hz。`

### 14.3 启动失败总结

`uroad 未完成启动。日志显示 DDS 初始化失败，后续未检测到正常帧处理。`

### 14.4 连续跳帧总结

`uroad 已启动，但当前持续跳帧。主要原因是非 D 挡或感知状态不满足运行条件。`

---

## 15. 数据结构建议

前端内部可以保留结构化字段，但页面输出必须以“自然语言 + 图表 + 状态”形式为主。

建议至少保留：

```ts
type UroadParseResult = {
  summary: {
    title: string
    description: string
    runningStatus: 'healthy' | 'warning' | 'error' | 'not_started'
    startupStatus: 'success' | 'partial' | 'failed' | 'unknown'
    totalFrames: number
    normalFrames: number
    skippedFrames: number
    errorFrames: number
    latestFps?: number
    avgE2EMs?: number
    mainIssue?: string
  }
  timeline: UroadTimelineEvent[]
  frames: UroadFrame[]
  charts: {
    preCostSeries: Array<{ frame: number; value: number }>
    inferCostSeries: Array<{ frame: number; value: number }>
    postCostSeries: Array<{ frame: number; value: number }>
    e2eCostSeries: Array<{ frame: number; value: number }>
    fpsSeries: Array<{ ts: string; value: number }>
    skipReasonStats: Array<{ reason: string; count: number }>
    errorReasonStats: Array<{ reason: string; count: number }>
  }
  rawMatchedLogs: string[]
}

type UroadTimelineEvent = {
  ts: string
  level: 'info' | 'warning' | 'error'
  category: 'startup' | 'frame' | 'skip' | 'warning' | 'error' | 'fps' | 'shutdown'
  title: string
  description: string
  raw?: string
}

type UroadFrame = {
  frame: number
  status: 'normal' | 'skipped' | 'error' | 'incomplete'
  humanSummary: string
  speedKph?: number
  gear?: string
  preTotalMs?: number
  inferMs?: number
  postTotalMs?: number
  e2eMs?: number
  skipReason?: string
  errorReason?: string
}
```

---

## 16. 验收标准

本需求的验收重点不是“能匹配出字段”，而是“能不能把日志变成用户一眼能看懂的运行说明”。

满足以下条件即可认为达标：

1. 能从混合云端日志中稳定抽取 `uroad` 子集。
2. 能把启动、运行、跳帧、异常、退出日志翻译成自然语言说明。
3. 能按帧生成易懂总结，而不是只显示原始字段。
4. 能生成顶部运行总结。
5. 能生成耗时和 FPS 图表数据。
6. 能在页面上明确告诉用户“发生了什么、问题在哪、是否影响运行”。
7. 原始日志只作为辅助，不是主展示内容。

---

## 17. 最小可用版本建议

### P0

- 混合日志中抽取 `uroad`
- 顶部总结
- 运行时间线
- 帧级人话总结
- 跳帧/异常翻译
- 前处理/推理/后处理/端到端耗时图

### P1

- FPS 趋势图
- 异常分类统计
- 跳帧原因统计
- 原始日志联动定位

### P2

- 自动识别“当前主要问题”
- 自动生成性能瓶颈结论
- 多段日志横向对比

---

## 18. 示例：页面应该展示成什么样

### 顶部总结示例

`uroad 已成功启动并正常运行。最近共识别 286 帧，其中 260 帧正常、18 帧跳过、8 帧异常。当前主要问题是推理耗时偏高，最新 FPS 为 9.85 Hz。`

### 帧详情示例

- `第 5 帧处理完成，前处理 14.96 ms，推理 80.15 ms，后处理 8.28 ms，端到端 103.39 ms。`
- `第 12 帧已跳过：当前不是 D 挡。`
- `第 35 帧处理失败：推理输出解析失败。`

### 事件流示例

- `16:22:37.998 uroad 前处理节点已创建`
- `16:22:38.003 雷达接收器启动成功`
- `16:22:38.015 第 5 帧前处理完成，总耗时 14.96 ms`
- `16:22:38.095 第 5 帧进入后处理阶段`
- `16:22:38.103 第 5 帧后处理完成，端到端总耗时 103.39 ms`
- `16:22:38.500 当前不是 D 挡，uroad 暂停处理`
