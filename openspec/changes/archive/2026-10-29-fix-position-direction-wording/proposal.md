# Proposal

## Why

用户指出 SHORT 持仓显示“空仓 0.04”会被误认为没有仓位，且摘要中的管理者内部 ID 多余。

## What Changes

- 持仓、挂单方向统一为“多头/空头”，工具说明保持一致。
- 精简文本省略管理者内部 ID，shared 使用已有中文“授权共管”；结构化身份和权限保持不变。
- 更新文档及现有格式回归，按既定流程归档并发布 Git/Docker，服务器由用户更新。

## Capabilities

### Modified Capabilities

- `decision-state-context`: 明确方向文案和精简文本的管理信息展示。

## Impact

仅影响文本格式、工具说明和文档，不改变持仓数量、价格、交易动作、权限或历史记录。
