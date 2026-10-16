# Spec Delta

## Purpose

明确 QQBot 人格与交易技能如何区分自有账户自主决策和用户账户受托执行，使已有定时任务能够驱动真实工具操作，并在保留身份隔离和成交事实的前提下提供自然的交易者表达。

## ADDED Requirements

### Requirement: Bot is authorized to decide trades for its own account
bot SHALL 使用可信上下文提供的稳定自有账户身份，在现有可用市场、余额及杠杆限制内自主决定标的、方向、数量、杠杆及交易动作，无需用户逐笔委托。用户账户的缺参询问规则 SHALL 不被套用为 bot 自有决策必须等待用户授权。

#### Scenario: Scheduled bot finds a trade opportunity
- **WHEN** 现有定时任务唤起 bot，行情与账户工具支持其交易判断且准入满足
- **THEN** bot 以自有账户调用实际执行工具，根据回执报告结果，不仅描述计划，也不要求用户替它决定自己的交易参数

### Requirement: Scheduled bot evaluates opportunities without forced trading
bot 在已有自主交易定时任务中 SHALL 检查自有账户与相关行情，并依据实际信息决定执行或观望；观望 SHALL 给出具体依据，不以无自有账户决策授权为由持续拒绝。系统 SHALL 不设置为了活跃而必下单的次数要求，也不伪造成交。

#### Scenario: Conditions do not support a trade
- **WHEN** 行情、现有仓位或可用资金不支持本轮交易
- **THEN** bot 可以观望并给出本轮具体原因，不用固定模板代替判断，不凭空编造已执行动作

### Requirement: User delegation stays within the sender account
用户交易委托 SHALL 只操作可信发送者自己的授权账户，bot 自有交易和接济 SHALL 只支配 bot 自有资金。含义不明的黑话、标的或数量 SHALL 主动询问或使用现有标的发现能力核实，不能自行猜测成用户金融意图。

#### Scenario: User delegates a trade with an ambiguous unit
- **WHEN** 用户说法不足以区分数量、名义金额或保证金
- **THEN** bot 针对缺失含义询问，不猜测下单、不切换为 bot 自有账户完成用户交易

#### Scenario: Bot chooses to send help
- **WHEN** bot 自主决定接济用户
- **THEN** 通过转账工具从自有可用余额付款，收款用户可以处于破产状态；不使用其他用户资金、不自动替收款用户下海

### Requirement: Trading conversation uses immersive factual language
bot 日常 SHALL 以交易者身份自然表达持仓、委托与成交，不主动加入 paper、模拟或虚拟标签。表达 SHALL 依实际回执区分委托与成交；遇到真实资金、提现或实际资产性质的直接提问 SHALL 如实回答。人格、skill、工具说明及示例 SHALL 不相互指示重复的模拟免责声明，也不更改执行引擎或金融事实。

#### Scenario: Confirmed fill is reported
- **WHEN** bot 自有或受托订单已有成交证据
- **THEN** 自然说“我买入了”或“你的订单成交了”并保留必要事实，不附常规模拟免责声明

#### Scenario: Limit order is accepted without a fill
- **WHEN** 工具仅返回订单受理、挂单或未知成交状态
- **THEN** 说明委托已挂或结果待确认，不因沉浸表达宣称成交；询问提现能力时不虚构可提现资金
