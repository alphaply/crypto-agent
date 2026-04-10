# 成本估算（v1.0）

本项目成本主要来自 LLM API。v1.0 支持在后台维护模型单价，并自动按 token 用量估算。

## 1. 成本计算公式

设：

- $P_{in}$：输入单价（USD / 1M tokens）
- $P_{out}$：输出单价（USD / 1M tokens）
- $T_{in}$：输入 token 数
- $T_{out}$：输出 token 数

单次调用成本：

$$
Cost = \frac{T_{in}}{10^6} \cdot P_{in} + \frac{T_{out}}{10^6} \cdot P_{out}
$$

## 2. 日成本估算方法

日成本约为：

$$
DailyCost \approx CallsPerDay \times AvgCallCost
$$

其中：

- `CallsPerDay` 受模式和运行周期影响。
- `AvgCallCost` 受模型、提示词长度、上下文长度影响。

## 3. 模式与调用频率参考

| 模式 | 典型频率 | 典型日调用数（单配置） |
| :--- | :--- | :--- |
| STRATEGY | 60 分钟 | 24 |
| REAL | 15 分钟 | 96 |
| SPOT_DCA | 1d 或 1w | 1 或 1/7 |

## 4. v1.0 定价管理说明

- 定价来源：`pricing.json` + 数据库表 `model_pricing`。
- 后台支持新增、修改、删除模型定价。
- 删除或更新后会自动回写 `pricing.json`，重启不丢失。

## 5. 降本建议

1. 优先减少高频 `REAL` 配置数量。
2. 缩短提示词冗余文本，减少上下文长度。
3. 将高成本模型用于关键配置，其他配置用中低价模型。
4. 按周复盘 Stats 页面，淘汰无效配置。

## 6. 监控建议

- 每日关注：总 token、按模型成本、按配置成本。
- 每周关注：单配置收益/成本比，识别“高成本低贡献”配置。
