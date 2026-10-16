# news-polling-schedule Specification

## Purpose

为新闻流水线提供按北京时间固定时刻触发的轮询语义，使后台设置的间隔对应稳定的日历时间，避免上一轮启动时间和耗时累积成偏移，并让监控中的轮次开始时间可以用于验收。

## Requirements

### Requirement: News polling follows fixed Beijing time slots
新闻定时轮询 SHALL 根据配置间隔和北京时间每日零点计算固定触发时刻，不以最近一次开始或完成时间重新计时。此规则 SHALL 仅调整定时轮询，保留已有手动更新及公告流触发入口。

#### Scenario: Half hourly polling stays on the clock
- **WHEN** 新闻轮询间隔设为 1800 秒，轮询服务正常运行
- **THEN** 定时触发时刻为北京时间 12:30、13:00、13:30，单轮耗时或上轮报错不将下一轮推迟到 13:01 等时间

#### Scenario: Several ticks observe one slot
- **WHEN** 同一触发时刻被多个定时检查观察到
- **THEN** 同一槽位只产生一个逻辑定时轮次，不因重复检查多跑一轮

#### Scenario: A run fails before the next slot
- **WHEN** 12:30 的轮次超时并报错，服务在 13:00 正常执行定时检查
- **THEN** 13:00 仍按固定时刻触发，不从错误发生时刻重新计算 30 分钟
