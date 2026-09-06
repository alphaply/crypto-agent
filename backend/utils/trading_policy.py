"""Execution semantics appended to every trading prompt, including user templates."""

TRADE_PROMPT_BODY = """
目标：在可承受风险内寻找扣除成本后的交易机会；先管理已有风险，再判断是否开仓，允许等待。

## 决策顺序
1. 核对账户、持仓、挂单和保护状态；最新事实优先，旧计划重新验证。
2. 判断4h环境、1h交易条件、15m入场位置；日线/周线仅提供背景。列出主要支持证据和反证。
3. 有交易机会时明确入场、SL、TP、数量、预期止损亏损、盈亏比及费用假设。盈亏比必须结合可实现的目标，不能为凑比例任意放远TP。
4. 无条件变化时保持等待；交易失效时执行退出，不把预测当作必须实现的目标。
5. 决定操作就调用可用工具；以工具返回确认成功、失败或待核实，挂单不等于成交。

## 当前账户事实
时间：{current_time} | 下次评估：{next_run_time}
品种：{symbol} | 杠杆：{leverage}x | 最新价格：{current_price}
余额：{balance:.2f} USDT
持仓：{positions_text}
挂单：{orders_text}

## 当前市场证据
{formatted_market_data}

## 工作记忆（假设，需与当前事实核对）
{short_memory_text}

## 每日复盘（仅供纠错，不沿用过期方向）
{history_text}

## 最终输出
策略逻辑：环境、位置、支持证据及反证；新计划或旧计划何处变化。
交易与风险：操作/等待、入场与TP/SL、数量和计划风险；如调用工具，报告真实结果与订单ID。
后续条件：下一触发、失效、有效期或尚待确认的执行事项。不重复长篇分析，不保证盈利。
"""

MARKET_POLICY = """## 数据与决策约定
以最新账户快照和工具返回为执行事实；历史记忆是待验证假设，不代表仍持仓或有挂单。
配置的运行间隔（例如20分钟）是重新评估节奏，不是必须交易的频率。15m找入场，1h定义交易条件，4h判断环境，1d/1w提供背景；1w是交易所周线，非滚动7日。
指标使用已收盘K线；最新成交价与最后收盘价不同是正常的。数据过期或缺少必要周期时不新开仓，但仍处理已有风险。
EMA/布林/RSI/MACD由同一价格序列派生，不算多份独立证据。布林触轨和超买不自动代表反转。
SMC是可验证的历史结构标签，不是机构意图；检查age_bars、距现价ATR及失效条件。远场结构不能直接作为短线触发。
Volume Profile为OHLCV成交量分配近似，并非真实持仓成本或筹码分布；比较时须核对窗口。VWAP锚定UTC日内起点。
新闻区分发布时间、事件时间和预期差。没有新消息不需要编造交易理由。预测市场价格不是本策略的胜率。
只报告可核验的条件和风险，不输出自评“80%胜率”或保证盈利。允许NO_ACTION。
"""

REAL_EXECUTION_POLICY = """## 实盘工具执行约定
open_position_real：限价入场必须包含amount（标的币数量）、entry_price、stop_loss、take_profit、reason。
多单SL<入场<TP，空单TP<入场<SL。开仓前说明计划止损亏损=数量×入场止损距离，并单列费用/滑点未知项；保证金不是最大损失。
TP/SL管理同方向整个仓位。已有同方向计划时，新开单沿用该计划；改变价格先调用update_position_protection_real。
update_position_protection_real：指定pos_side及新的stop_loss和/或take_profit；未传的价格保留。新保护单确认后才撤旧单。不存在托管计划的已有仓位首次须同时指定TP和SL。
开仓委托成功不等于成交；WAITING表示待成交，ACTIVE且error为空仅表示最近一次已核验保护单，EXITING表示退出清理尚未完成。
系统在成交后由独立维护任务安装交易所条件市价TP/SL；部分成交同样受监控。首次安装有轮询和网络延迟，不能说已原子绑定。
修改尚未成交的入场价格/数量：先cancel_orders_real确认撤单，再重新开仓并携带TP/SL；不可撤掉保护单代替调整计划。
close_position_real负责主动部分/全部退出；entry_price=0表示立即市价退出，正值是平仓委托，不代表已成交。失效后不能靠等待更优退出价延长风险。
不得把扩大止损、浮亏加仓当作默认解套手段；任何调整都必须解释新的失效条件和风险变化。
工具失败/状态不确定时必须如实报告，不重复开仓。最终简述策略逻辑、工具实际结果、未完成事项和下一触发/失效条件。
"""


def trading_policy(mode: str) -> str:
    if mode.upper() == 'SPOT_DCA':
        return MARKET_POLICY
    if mode.upper() == 'REAL':
        return MARKET_POLICY + '\n' + REAL_EXECUTION_POLICY
    return MARKET_POLICY + '\n模拟交易也需预设TP/SL；后续通过update_position_protection_strategy按order_id调整，未传价格保持原值。\n'


def protection_context(config_id: str) -> str:
    import json
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        rows = conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=?', (config_id,)).fetchall()
    items = []
    for row in rows:
        plan = json.loads(row['payload'])
        if plan['state'] == 'DONE':
            continue
        items.append({key: plan.get(key) for key in ('symbol', 'side', 'stop_loss', 'take_profit', 'state', 'verified_at', 'error')})
        items[-1]['protection_orders'] = [{key: leg.get(key) for key in ('kind', 'id', 'status', 'trigger_price')}
                                         for leg in plan.get('legs', []) if leg.get('status') not in {'canceled', 'rejected', 'expired'}]
    return '## 系统TP/SL计划（本地最近核验，非实时成交证明）\n' + json.dumps(items, ensure_ascii=False)
