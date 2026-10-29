# event-time-context Specification

## Purpose

为新闻、交易分析、策略摘要和短期记忆提供一致的事件时间依据，保留来源提供的日期和时区，避免将 UTC 事件时间直接当成本地时间，并区分预定公布、新闻发布时间及采集时间。

## Requirements

### Requirement: Event context preserves explicit source timezones
新闻提供给用户及模型的事件时间 SHALL 保留原始时间值，并提供包含日期及原偏移的可读说明，不统一转换为北京时间。缺失或不明确的时区 SHALL 不被猜测。预定事件时间、新闻发布时间和采集时间 SHALL 分别表达。

#### Scenario: UTC scheduled release
- **WHEN** 日历提供 2026-10-29T12:30:00+00:00
- **THEN** 上下文明确显示 2026-10-29 12:30 UTC，保留原字段，不把它显示成北京时间 12:30

#### Scenario: Unknown timezone
- **WHEN** 原文时间未提供时区
- **THEN** 原文保留且不会补入 UTC、北京时间或推定的偏移

### Requirement: Summarization retains event time meaning
新闻摘要、Agent 分析、策略摘要和记忆整理 SHALL 使用一致的时间规则：引用保留日期及时区，比较时间按明确偏移换算；预告和时间已到不证明实际数据已公布。时间规则更新 SHALL 使下一次正常新闻更新不复用旧规则摘要；历史记录不批量改写。

#### Scenario: GDP release is still upcoming
- **WHEN** 当前北京时间为 2026-10-29 13:00，GDP 预定时间为同日 12:30 UTC
- **THEN** 提供给分析的明确时间依据表明预定时刻尚未到达；摘要和记忆规则禁止将事件当作北京时间 12:30 已公布

#### Scenario: Offset crosses date or daylight saving
- **WHEN** 输入包含跨日或美东冬夏令时的明确偏移
- **THEN** 按对应瞬间比较，展示保留来源日期及偏移，不使用固定美东偏移
