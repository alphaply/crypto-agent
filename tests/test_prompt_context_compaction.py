import json
from unittest.mock import Mock

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

import backend.database as database
from backend.agent import agent_graph
from backend.agent.agent_models import AgentState
from backend.agent.tool_registry import get_trade_tools_for_mode
from backend.database_schema import initialize_schema
from backend.utils.order_context import load_protection_plans
from backend.utils.formatters import format_orders_to_agent_friendly


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'test.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)


def save_plan(config_id, side, payload):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                     (config_id, 'ETH/USDT:USDT', side, json.dumps(payload)))
        conn.commit()


def test_protection_is_compact_but_preserves_actionable_uncertainty(local_db):
    save_plan('cfg', 'LONG', {
        'state': 'ACTIVE', 'stop_loss': 2500, 'take_profit': 2700, 'verified_at': 1790557200,
        'error': 'replacement pending',
        'legs': [{'kind': 'sl', 'id': 'opaque-long-protection-id', 'status': 'open', 'trigger_price': 2490},
                 {'kind': 'tp', 'id': 'cancelled-id', 'status': 'canceled', 'trigger_price': 2800}],
        'entries': [{'id': 'entry-1', 'amendment': {'state': 'pending'}}],
    })
    save_plan('cfg', 'SHORT', {'state': 'WAITING', 'stop_loss': None, 'take_profit': 2300})
    save_plan('other', 'LONG', {'state': 'ACTIVE', 'stop_loss': 12345})
    text = format_orders_to_agent_friendly([], load_protection_plans('cfg'), 'ETH/USDT')
    assert '非实时成交证明' in text
    assert 'ETH/USDT:USDT LONG' in text
    assert '同向本地计划=ACTIVE | SL=2500 TP=2700' in text
    assert '最近核验=' in text and 'UTC' in text
    assert 'SL@2490:open' in text  # retained target/observed-price mismatch
    assert '异常=replacement pending' in text
    assert '入场改单待确认=entry-1（不可重复提交）' in text
    assert '同向本地计划=WAITING | SL=未设置 TP=2300 | 最近核验=未核验' in text
    assert '无已记录保护单' in text
    assert 'opaque-long-protection-id' not in text and 'cancelled-id' not in text
    assert '12345' not in text and '"protection_orders"' not in text


def test_done_and_malformed_protection_do_not_imply_current_coverage(local_db):
    save_plan('cfg', 'LONG', {'state': 'DONE', 'stop_loss': 100})
    assert load_protection_plans('cfg') == []
    assert '无活跃保护计划；不代表交易所没有保护单' in format_orders_to_agent_friendly([], [], 'ETH/USDT')
    with database.get_db_conn() as conn:
        conn.execute("UPDATE real_protection_plans SET payload='broken' WHERE config_id='cfg'")
        conn.commit()
    assert '保护记录无法解析，不能假设已有保护' in format_orders_to_agent_friendly([], load_protection_plans('cfg'), 'ETH/USDT')


def test_order_rows_merge_targets_and_verification_without_duplicate_protection_block(local_db):
    save_plan('cfg', 'LONG', {
        'state': 'ACTIVE', 'stop_loss': 2619.0, 'take_profit': 2712.0, 'verified_at': 1790566221,
        'legs': [{'kind': 'sl', 'id': 'sl-order', 'status': 'open', 'trigger_price': 2619.0},
                 {'kind': 'tp', 'id': 'tp-order', 'status': 'open', 'trigger_price': 2712.0}],
    })
    save_plan('cfg', 'SHORT', {'state': 'WAITING', 'stop_loss': 2821.0, 'take_profit': 2717.0})
    orders = [
        {'id': 'entry-order', 'side': 'SELL', 'pos_side': 'SHORT', 'amount': .2, 'price': 2779.0, 'type': 'LIMIT'},
        {'id': 'sl-order', 'side': 'SELL', 'pos_side': 'LONG', 'amount': .2, 'price': 2619.0, 'type': 'STOP'},
        {'id': 'tp-order', 'side': 'SELL', 'pos_side': 'LONG', 'amount': .2, 'price': 2712.0, 'type': 'TP'},
    ]
    text = format_orders_to_agent_friendly(orders, load_protection_plans('cfg'), 'ETH/USDT')
    rows = text.splitlines()
    assert '[OPEN SHORT]' in rows[0] and 'SL=2821.0 TP=2717.0' in rows[0] and 'WAITING' in rows[0]
    assert '[CLOSE LONG]' in rows[1] and 'ACTIVE' in rows[1] and '最近核验=' in rows[1]
    assert '本地SL=open' in rows[1] and '本地TP=open' in rows[2]
    assert text.count('2619.0') == text.count('2712.0') == 1
    assert text.count('同向本地计划=ACTIVE') == 1
    assert '当前保护摘要' not in text and '挂单快照未见' not in text


def test_order_merge_preserves_target_mismatch_and_does_not_cross_symbol_or_side(local_db):
    save_plan('cfg', 'LONG', {'state': 'ACTIVE', 'stop_loss': 2500, 'take_profit': 2700,
                            'legs': [{'kind': 'sl', 'id': 'old-sl', 'status': 'open', 'trigger_price': 2490}]})
    orders = [{'id': 'old-sl', 'symbol': 'ETH/USDT', 'side': 'SELL', 'pos_side': 'LONG', 'price': 2480},
              {'id': 'different', 'symbol': 'BTC/USDT', 'side': 'BUY', 'pos_side': 'LONG', 'price': 123}]
    text = format_orders_to_agent_friendly(orders, load_protection_plans('cfg'), 'ETH/USDT')
    assert 'SL=2500' in text and 'open@2490（与快照价格待核对）' in text
    assert '同向本地计划' not in text.splitlines()[1]
    orders[0]['pos_side'] = 'SHORT'
    text = format_orders_to_agent_friendly(orders, load_protection_plans('cfg'), 'ETH/USDT')
    assert '同向本地计划' not in text.splitlines()[0]
    assert '无同向挂单快照' in text and '挂单快照未见=SL@2490:open' in text
    orders[0]['pos_side'] = 'NET'
    text = format_orders_to_agent_friendly(orders, load_protection_plans('cfg'), 'ETH/USDT')
    assert 'LONG，订单ID匹配' in text.splitlines()[0]
    orders[0]['symbol'] = 'ETH/USDT:USDC'
    text = format_orders_to_agent_friendly(orders, load_protection_plans('cfg'), 'ETH/USDT')
    assert '同向本地计划' not in text.splitlines()[0]


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY'])
@pytest.mark.parametrize('exit_mode', ['attached_required', 'attached_optional', 'independent_exits'])
@pytest.mark.parametrize('prompt_role', ['system', 'user'])
@pytest.mark.parametrize('template', ['Custom decision prompt: {symbol}', 'Custom decision prompt: {symbol}\n挂单：{orders_text}\n{formatted_market_data}'])
def test_decision_prompt_only_loads_compressed_history_and_current_protection(local_db, monkeypatch, mode, exit_mode, prompt_role, template):
    import backend.utils.performance_context as performance

    performance_reader = Mock(side_effect=AssertionError('historical evidence belongs in memory update'))
    monkeypatch.setattr(performance, 'performance_context', performance_reader)
    market = Mock()
    market.get_market_analysis.return_value = {'analysis': {'15m': {'price': 2600, 'atr': 15,
        'decision_context': {'regime': {'chop': {'value': 67.8}, 'cmf': {'value': .12},
                                         'squeeze': {'state': 'on', 'on_bars': 3}}}}}}
    market.get_account_status.return_value = {'balance': 1000, 'available_balance': 900,
                                             'real_positions': [], 'real_open_orders': [], 'mock_open_orders': []}
    market.fetch_recent_trades.return_value = []
    monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
    monkeypatch.setattr(agent_graph, 'fetch_news_risk_context', lambda *_: {})
    monkeypatch.setattr(agent_graph, 'resolve_prompt_template', lambda *_: template)
    monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda *_: 2)
    for name in ('save_news_snapshot', 'save_balance_snapshot', 'sync_open_position_history'):
        monkeypatch.setattr(database, name, lambda *_, **__: None)
    database.save_short_memory('2026-01-01', '2026-01-01', 'ETH/USDT', 'cfg',
                               'compressed lessons only', 'heavy raw ledger must stay stored', 1)
    state = AgentState(symbol='ETH/USDT', messages=[], market_context={}, account_context={},)
    result = agent_graph.start_node(state, {'configurable': {
        'config_id': 'cfg', 'agent_config': {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': mode,
                                           'market_timeframes': ['15m'], 'exit_mode': exit_mode,
                                           'system_prompt_role': prompt_role},
    }})
    prompt = result.messages[0].content
    assert 'compressed lessons only' in prompt
    assert 'heavy raw ledger' not in prompt
    assert 'Agent 收益与回撤' not in prompt and '过去7天平仓记录' not in prompt
    assert '本轮交易工具接口' not in prompt
    assert 'update_entry_order_' not in prompt
    assert '当前保护摘要' not in prompt
    assert ('本地核验非实时成交证明' in prompt) == (mode == 'REAL' and exit_mode != 'independent_exits')
    assert prompt.count('本地核验非实时成交证明') <= 1
    assert f'exit_mode={exit_mode}' in prompt
    if exit_mode == 'independent_exits':
        assert '附带 TP/SL 为空不代表没有独立退出单' in prompt
        assert '本系统独立 SL 未覆盖数量' in prompt
    assert '加仓通过 open' not in prompt and 'close 的 exit_type' not in prompt
    assert '## 决策与复盘要求' not in prompt
    assert '按下方共同复盘要求' not in prompt
    assert result.messages[0].type == ('human' if prompt_role == 'user' else 'system')
    if '{formatted_market_data}' in template:
        assert 'CHOP14=67.8' in prompt and 'CMF20=0.12' in prompt
        assert 'Squeeze(BB20,2σ/KC20,1.5×SMA-TR)=on on_bars=3' in prompt
    performance_reader.assert_not_called()


def test_native_bound_schema_contains_amendment_and_protection_contracts():
    tools = {tool.name: convert_to_openai_tool(tool)['function'] for tool in get_trade_tools_for_mode('REAL')}
    amend = tools['update_entry_order_real']
    protect = tools['update_position_protection_real']
    close = tools['close_position_real']
    assert 'confirmed' in amend['description'] and 'pending不可重复提交' in amend['description']
    assert '不能翻多/翻空' in amend['description']
    assert '两个调用不是原子事务' in amend['description']
    assert '包含已成交部分' in amend['parameters']['properties']['amount']['description']
    assert '当前触发参考价' in protect['description'] and '整个仓位' in protect['description']
    assert 'LONG平多（卖出）' in close['description'] and 'SHORT平空（买入）' in close['description']


@pytest.mark.parametrize('mode', ['REAL', 'STRATEGY'])
def test_open_tool_schema_carries_all_exit_modes_without_prompt_tutorial(mode):
    tools = {tool.name: convert_to_openai_tool(tool)['function'] for tool in get_trade_tools_for_mode(mode)}
    description = tools[f'open_position_{mode.lower()}']['description']
    assert 'attached_required必须同时提供TP和SL' in description
    assert 'attached_optional选填' in description
    assert 'independent_exits禁止附带' in description
    assert f'close_position_{mode.lower()}' in description
