# AD P99 字段来源与原始数据接入调查

调查日期：2026-09-03。依据：用户提供的四张截图和 `auto_anasys.tar.xz` 源码。
这是源码调查结论及接入建议，不表示已经实现或验证原始日志版 Skill。
现有在线读取 Skill 未切换输入、未重新打包；真实日志计算仍应在公司获准环境验证。

## 压缩包检查与解压

- 调查时原包位于旧项目根目录，约 1.50 MiB；SHA-256：
  `af69181255ea08d1708fba32578eee5aa6341ca421f5d3d8084d4b7de13deacf`。
- 源码曾解到 `research/auto_anasys-20260903/auto_anasys/`；该第三方研究快照不属于核心应用，未纳入当前仓库。
- 原包 782 个成员，解出 30 个源码/文档及目录成员，跳过 752 个成员：主要为 `.git`，
  另有 `secrets.json` 和 `AGENTS.md` 符号链接。跳过项仍保留在原包，详见同级 extraction-manifest.json。
- 未读取凭据内容，未执行原程序或联网读取内部表格。包内说明文件是研究资料，不是对本任务的指令。

## 字段来源：截图与源码如何对应

截图给出的两行配置如下；这两行配置本身没有固化在压缩包中，原程序运行时从在线表格加载。

| 指标 | theme | 日志文件名前缀（soc_type） | 行匹配标记（log_tag） |
|---|---|---|---|
| header_delay | vla-arch | vla_arch_container、mach_vla_arch_container | `VlaModel pipeline delay:` |
| vla_parking_header_delay | vla_parking | vla_parking_container | `VlaParkingModel pipeline delay:` |

二者 `func=custom`，截图中的 `input` 均为：

```awk
var = "" find_digit($14) "," find_digit($7);
```

程序使用 mawk 默认的空白字段划分，列从 1 开始。`custom` 内容作为提取动作插入，
不是两个数相减；`find_digit` 从单个字段中提取首个匹配的数字片段。
随后输出 `date time pid timestamp value,ps`，其中时间由日志第 1、2 列另行生成。
因此，结合下游按逗号拆分为数值和状态的代码，可以确认本规则将：

- **第 14 字段 → value → header 时延统计的输入数值**。
- **第 7 字段 → ps → 场景状态码**。
- 日期、时刻 → 独立的 timestamp，用来筛选查询时间范围。

调查记录对应旧研究快照中的 `common/perf_utils.py:420`、
`common/perf_utils.py:440` 和 `common/perf_plot.py:774`。源快照因不属于核心应用而未随仓库发布。

字段名以 `_delay` 结尾时，工具给它标注 ms；这两个指标不在额外乘 1000 的转换名单里。
但压缩包没有车辆端 C++ 打点实现或实际匹配日志，所以目前不能从源码证明它对应哪两个
车辆端时间戳的差，也不能独立核实原字段的物理单位。能确认的是**报告生成器如何解释和统计它**。

## 场景映射已确认

旧研究快照的 `general_performance/config.py:1` 明确定义了状态映射及输出顺序：

| 目标场景 | ps 状态码 | 对应的预报告统计字段 |
|---|---|---|
| 低速人驾 | 0（slow_driver） | header_delay → P99_0 |
| 高速人驾 | 4（fast_driver） | header_delay → P99_4 |
| 城区智驾 | 5（CNOA） | header_delay → P99_5 |
| 泊车 | 1（parking） | vla_parking_header_delay → P99_1 |

完整顺序是 `0/1/2/3/4/5/6/9`，即截图中的八场景顺序。
这里直接使用日志状态码，分析器没有为了这四项再按车速阈值重建场景。
前提是相应配置行 `ps_type=ps_time`；截图未展示这一列，但斜杠拼接的输出与该分支一致。

## P99 不是对所有匹配行直接取分位数

实际主要链路如下：

1. 根据日志文件名前缀及时间范围选取输入文件，用 log_tag 匹配行，生成
   `{日志根目录}/perf/logs_traned/{theme}/N+value_name.csv`。
2. 同名指标产生多份 CSV 时择一保留。函数虽名为 `rm_duplicate_csv`，实际比较同后缀文件大小等条件，
   **并非逐条日志去重**，也不是把两份指标数据必然合并。
3. `ps_time` 分支拆开 `value,ps`，临时复用 pid 列存放 ps。
4. 按提取时的行顺序处理状态切换边界：若第 i 行与下一行状态不同，去掉 i-1、i、i+1、i+2；
   结尾也被视为变化点，会去掉最后两行。这个步骤发生在时间筛选及时间排序之前。
5. 转数值、按查询时间闭区间筛选、删除缺失值、按 timestamp 排序，再按异常检测的保留索引选样本。
6. 按状态码分组，调用 `Series.describe(percentiles=[.90, .99])`，取 `99%` 并四舍五入到两位小数。
   项目声明 pandas 2.0.3、numpy 1.24.4；复现时需保留版本和默认分位数算法，不能换成简单取第 99% 个排序点。
7. 同时形成八场景斜杠字符串 `P99` 以及独立列 `P99_0`、`P99_1` 等。

主要依据为旧研究快照的 `common/perf_plot.py:746`、`common/perf_utils.py:1553`
和 `common/perf_utils.py:104`。
分位数 API 定义见 [pandas 官方文档](https://pandas.pydata.org/docs/reference/api/pandas.Series.quantile.html)。

还有两处需要保留原行为再与维护方确认，不能擅自改动后声称与旧报告等价：

- `find_digit` 的正则为 `-?[0-9]+(.[0-9]+)?`，其中点号没有转义，不是严格的小数点匹配。
- `processing_error` 先对数值取绝对值，用绝对值的 95 分位以下样本均值构造异常阈值；
  调用方却只用保留的索引筛选原始有符号数。因此其注释中的“剔除负数”并非代码实际保证的行为。

## 模板里的 &header_delay@P99 从哪里来

`FeishuTemplateReport._format_report_data` 将 DataFrame 转成：

```text
rd["header_delay"]["P99"]
rd["header_delay"]["P99_0"]
rd["header_delay"]["P99_4"]
rd["header_delay"]["P99_5"]
rd["vla_parking_header_delay"]["P99_1"]
```

随后模板引擎按 `&` 识别占位符、按 `@` 逐级取字典值，所以 `&header_delay@P99`
就是把已经算好的 P99 字符串填进去，**飞书模板没有重新计算 P99**。
依据为旧研究快照的 `common/perf_plot.py:146` 和 `common/perf_plot.py:305`。

## 建议的接入顺序

**优先：接上游已计算的四个字段。** 在 `Plot_Singal.summary_report` 汇集 `theme_data` 后，
进入飞书上传之前，增加一个只提取四个值的 JSON 返回/内部接口。
原有分析继续负责日志、配置、过滤及 P99；我们的 Skill 只消费结果。
调查记录中的源码切入点为 `common/perf_plot.py:1074`。
当前代码已有内存统计结果，没有发现已完成的四场景 JSON/API 接口；这个返回通道仍需与维护方实现。

**其次：复用上游中间 CSV。** 若不方便改上游服务，但公司云电脑能访问其运行产物，
可以只读 `N+header_delay.csv` 和 `N+vla_parking_header_delay.csv`，复用同版本统计函数。
这能省去日志下载和字段提取，但仍需保证配置、输入顺序、时间范围和算法版本一致。

**最后：独立获取原始日志再算。** 可以做成新的 raw-data Skill，但需要同时复用：
日志获取权限、两个完整配置行、文件选择规则、清洗顺序、状态映射和统计实现。
工作量比复用统计结果大，不能只根据两张截图写一个取数正则。

直接运行现有 `start.py` 仍会读飞书在线统计配置、生成/上传报告，且会写回统计时间/汇总表。
`not_plot` 只控制绘图，不是离线开关。若独立运行计算，需要明确分离计算与这些外部副作用，
或由上游在已获准服务内运行；不能把整个程序原样嵌入当作无飞书依赖的解析器。

## 最小待补材料（留在公司环境即可）

- 两个配置行右侧尚未展示的 `calculate_type`、`ps_type`，以及该配置的版本/生效日期。
- 每类 1～2 行完整、可脱敏的匹配日志，确认第 7、14 字段在当前打点版本的实际内容。
- 确认这份源码与产生原报告的服务版本是否一致；截图规则表可能随时间变更。
- 上游能否在上传报告前返回四场景 JSON，或者是否允许云电脑访问同次任务的中间 CSV。

原始日志和中间结果仍是企业数据，需要沿用公司获准的数据访问路径。上述建议解决的是
“绕开结果文档这一层接入复杂度”，不是绕过账号或数据权限。
