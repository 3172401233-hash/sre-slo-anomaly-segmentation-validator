# SLO 异常分段验证器

对 SLO 错误率的突然变化做**离线规则分段**：把固定窗口里混在一起的多种原因
拆成互不重叠的区间，并为每个区间给出异常类型与证据，区分流量下降、采样
漂移、采集缺失和真实错误增长，同时输出无法归因的区间。

## 数据范围（重要）

本工具**只分析离线聚合数据**：

- 输入必须是已经落盘的、按固定时间步长聚合好的桶数据（错误率、请求量、
  采样率、采集器状态、依赖标识、时间窗口）。
- 工具不连接任何线上服务、监控系统、指标网关或采集器，不做实时拉取、
  告警下发或写回操作。
- 工具不会修复缺失数据，也不会对采集缺口做插值；缺失槽位只做归因标记。

## 异常类型与判定优先级

每个时间桶按以下顺序取**第一个命中的规则**，因此边界桶只会归属一个类型，
不会双重归属；相同类型的相邻桶合并为一个分段，分段首尾相接、完整覆盖窗口：

| 优先级 | 类型 (`segment_type`) | 判定条件 | 是否可行动 |
| --- | --- | --- | --- |
| 1 | `collection_missing` | 槽位无数据，或该槽位 `collector_up=false` | 是（排查采集链路） |
| 2 | `sample_drift` | 采样率相对基线下降超过 `sample_drift_ratio` | 是（核对采样配置） |
| 3 | `traffic_drop` | 错误率升高，同时请求量跌破流量基线下限 | 是（排查上游/容量） |
| 4 | `real_error_growth` | 错误率升高且流量、采样均正常 | 是（排查服务退化） |
| 5 | `normal` | 错误率未超过增长阈值 | 否 |
| 6 | `unattributable` | 错误率疑似异常，但缺少健康全采样基线，无法判断 | 否（需人工复核） |

“错误率升高”需要同时满足：

- `error_rate >= 基线错误率 × error_growth_ratio`
- `error_rate - 基线错误率 >= min_error_rate_delta`（避免基线接近 0 时的噪声放大）

采样漂移与流量下降同时出现时归为 `sample_drift`，但流量信号会记录在证据的
`other_signals` 中；采集器宕机槽位即使带有高错误率也只归为
`collection_missing`。

## 基线

每个依赖独立计算基线，取该依赖下**采集器正常且全采样**桶的中位数：

- `baseline.error_rate`：基线错误率
- `baseline.request_count`：基线请求量
- `baseline.sample_rate`：基线采样率

若不存在任何健康全采样桶，则所有有效桶标记为 `unattributable`，不会猜测
基线或强行归类为真实错误增长。

## 输入

桶记录为字典列表（或直接构造 `Bucket`），字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timestamp` | ISO-8601 字符串 / epoch 秒 / `datetime` | 桶起始时间，须与窗口和步长对齐 |
| `error_rate` | float，`[0, 1]` | 该桶聚合错误率 |
| `request_count` | float，`>= 0` | 该桶聚合请求量 |
| `sample_rate` | float，`(0, 1]`，默认 `1.0` | 采样比例，`1.0` 表示全采样 |
| `collector_up` | bool，默认 `true` | 采集器在该桶是否全程健康 |
| `dependency` | str，默认 `"default"` | 依赖/服务标识，基线按依赖隔离 |

窗口参数为半开区间 `[window_start, window_end)`，窗口长度必须是
`bucket_interval_seconds` 的整数倍；窗口内缺失的槽位按采集缺失处理。
越界或重复的桶会直接抛出 `ValueError`。

## 输出

`AnalysisResult`（可用 `to_dict()` 序列化）包含：

- `segments`：覆盖整个窗口的全部分段，每个分段含类型、起止时间、桶数和
  `evidence`（基线、观测极值、命中原因等）。
- `actionable_segments`：可行动异常（采集缺失、采样漂移、流量下降、真实
  错误增长四类）。
- `unattributable_segments`：无法归因、需要人工复核的区间。

## 快速使用

```python
from slo_segmentation import analyze, Thresholds

buckets = [
    {"timestamp": "2026-01-01T00:00:00Z", "error_rate": 0.01,
     "request_count": 1000},
    {"timestamp": "2026-01-01T00:05:00Z", "error_rate": 0.12,
     "request_count": 180},
    # ... 离线聚合桶
]

result = analyze(
    buckets,
    window_start="2026-01-01T00:00:00Z",
    window_end="2026-01-01T01:00:00Z",
    bucket_interval_seconds=300,
    thresholds=Thresholds(
        sample_drift_ratio=0.2,
        traffic_drop_ratio=0.5,
        error_growth_ratio=2.0,
        min_error_rate_delta=0.01,
    ),
)

for segment in result.actionable_segments:
    print(segment.segment_type.value, segment.start, segment.end,
          segment.evidence)
```

## 运行测试

```bash
python -m unittest
```

测试覆盖流量突降、采样变化、采集故障（缺槽与采集器宕机）、真实错误增长、
边界不双重归属、多依赖独立基线以及无法归因区间。

## 环境与依赖

- Python 3.8.2+（仅使用标准库：`dataclasses`、`enum`、`statistics`、
  `datetime` 等），无第三方依赖。
