from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from backend.agent import agent_graph as graph
from backend.agent import chat_graph
from backend.agent.agent_models import AgentState
from backend.utils.spot_portfolio import get_config_symbols, normalize_spot_symbols


def test_symbol_contract_preserves_legacy_and_rejects_mixed_quote():
    assert get_config_symbols({'mode': 'SPOT_DCA', 'symbol': 'btc/usdt'}) == ['BTC/USDT']
    assert normalize_spot_symbols(['btc/usdt', 'BTC/USDT', ' ETH/USDT ']) == ['BTC/USDT', 'ETH/USDT']
    for symbols in (['BTC/USDT', 'ETH/USDC'], ['BTC/USDT:USDT'], ['USDT/USDT'], 'BTC/USDT'):
        with pytest.raises(ValueError):
            normalize_spot_symbols(symbols)


def test_spot_defaults_ignore_global_intraday_but_keep_explicit_overrides(monkeypatch):
    monkeypatch.setattr(graph.global_config, 'market_timeframes', ['15m', '1h'])
    assert graph.resolve_market_timeframes({'mode': 'SPOT_DCA'}) == ['4h', '1d', '1w']
    assert graph.resolve_market_timeframes({'mode': 'SPOT_DCA', 'market_timeframes': ['1d']}) == ['1d']
    assert graph.resolve_market_timeframes({'mode': 'REAL'}) == ['15m', '1h']


@pytest.fixture
def portfolio(monkeypatch, tmp_path):
    from backend import database
    from backend.database_schema import initialize_schema
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'portfolio.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    cfg = {'config_id': 'portfolio', 'symbol': 'BTC/USDT', 'symbols': ['BTC/USDT', 'ETH/USDT'],
           'mode': 'SPOT_DCA', 'model': 'test', 'dca_amount': 100, 'dca_budget': 1000}
    market = Mock()

    def analysis(symbol, **kwargs):
        price = 60000 if symbol == 'BTC/USDT' else 3000
        return {'analysis': {tf: {'price': price, 'atr': price * .02,
                                 'ema': {}, 'macd': {}, 'bollinger': {}}
                             for tf in kwargs['timeframes']}, 'sentiment': {}}

    def account(symbol, **kwargs):
        return {'balance': 500, 'available_balance': 450,
                'real_positions': [{'symbol': symbol, 'amount': 1, 'entry_price': 100, 'side': 'LONG'}],
                'real_open_orders': [{'id': symbol[:3] + '-order', 'symbol': symbol,
                                      'price': 100, 'amount': .1, 'side': 'buy'}]}

    market.get_market_analysis.side_effect = analysis
    market.get_account_status.side_effect = account
    market.fetch_recent_trades.return_value = []
    for asset in cfg['symbols']:
        database.save_order_log(asset[:3] + '-order', asset, 'test', 'buy', 100, 0, 0,
                                'owned order', trade_mode='SPOT_DCA', config_id='portfolio', amount=.1,
                                event_type='ORDER_CREATED')
    monkeypatch.setattr(graph, 'MarketTool', Mock(return_value=market))
    monkeypatch.setattr(graph, 'fetch_news_risk_context', lambda symbol: {})
    monkeypatch.setattr(graph, '_load_decision_memory', lambda _: ([], 'short', 'recent', 'rules'))
    for name in ('save_news_snapshot', 'save_balance_snapshot', 'sync_open_position_history'):
        monkeypatch.setattr(graph.database, name, Mock())
    monkeypatch.setattr(graph.global_config, 'get_leverage', lambda _: 1)
    state = AgentState(symbol='BTC/USDT', messages=[], market_context={}, account_context={}, history_context=[])
    return cfg, market, state


def test_one_decision_sees_all_assets_and_one_shared_wallet(portfolio):
    cfg, market, state = portfolio
    result = graph.start_node(state, {'configurable': {'config_id': 'portfolio', 'agent_config': cfg}})
    assert [call.args[0] for call in market.get_market_analysis.call_args_list] == cfg['symbols']
    assert all(call.kwargs['timeframes'] == ['4h', '1d', '1w'] for call in market.get_market_analysis.call_args_list)
    assert set(result.market_context['by_symbol']) == set(cfg['symbols'])
    assert result.account_context['available_balance'] == 450
    assert result.account_context['balance'] == 500
    assert len(result.account_context['real_open_orders']) == 2
    prompt = result.messages[0].content
    for symbol in cfg['symbols']:
        assert f'## {symbol}' in prompt
    assert '100' in prompt and '由所有标的共享' in prompt
    assert '本周期已成交及挂单占用 0 USDT' in prompt
    assert '任务累计已占用 20 USDT' in prompt
    assert '预算允许新增买入上限 0 USDT' in prompt  # No registered trading run in this read-only context.
    assert 'BTC-order' in prompt and 'ETH-order' in prompt
    orders_block = prompt.split('【当前挂单】', 1)[1].split('【市场数据', 1)[0]
    assert 'Symbol: BTC/USDT' in orders_block and 'Symbol: ETH/USDT' in orders_block
    graph.database.sync_open_position_history.assert_not_called()


@pytest.mark.parametrize('template', [None, 'Custom: {symbol}',
                                     'Custom: {symbol}\n{positions_text}\n{orders_text}'])
def test_spot_prompt_keeps_holdings_and_orders_once_without_forced_review(portfolio, monkeypatch, template):
    cfg, _, state = portfolio
    if template is not None:
        monkeypatch.setattr(graph, 'resolve_prompt_template', lambda *_: template)
    result = graph.start_node(state, {'configurable': {'config_id': 'portfolio', 'agent_config': cfg}})
    prompt = result.messages[0].content
    for symbol in cfg['symbols']:
        assert prompt.count(f'[LONG] {symbol} | Amt: 1.0') == 1
        assert prompt.count(f"ID:'{symbol[:3]}-order'") == 1
    assert '本周期已成交及挂单占用 0 USDT' in prompt
    assert '任务累计已占用 20 USDT' in prompt
    assert '预算允许新增买入上限 0 USDT' in prompt
    assert '## 决策与复盘要求' not in prompt
    assert '最后说明规则复盘结论' not in prompt


def test_foreign_spot_orders_are_not_presented_as_task_actions(portfolio):
    cfg, market, state = portfolio
    original = market.get_account_status.side_effect
    def with_foreign(symbol, **kwargs):
        result = original(symbol, **kwargs)
        result['real_open_orders'].append({'id': 'manual-order', 'symbol': symbol, 'side': 'buy', 'amount': 1, 'price': 100})
        return result
    market.get_account_status.side_effect = with_foreign
    result = graph.start_node(state, {'configurable': {'config_id': 'portfolio', 'agent_config': cfg}})
    assert len(result.account_context['real_open_orders']) == 2
    assert len(result.account_context['external_open_orders']) == 2
    assert 'manual-order' not in result.messages[0].content
    assert '另有 2 笔其他/未归属挂单' in result.messages[0].content


def test_single_spot_task_also_rejects_stale_data(portfolio):
    cfg, market, state = portfolio
    cfg['symbols'] = ['BTC/USDT']
    market.get_market_analysis.side_effect = None
    market.get_market_analysis.return_value = {'analysis': {tf: {'price': 1, 'data_quality': {'stale': True}}
                                                           for tf in ['4h', '1d', '1w']}}
    with pytest.raises(graph.LLMInvocationError):
        graph.start_node(state, {'configurable': {'config_id': 'portfolio', 'agent_config': cfg}})


def test_zero_spot_budget_still_allows_analysis_and_order_review(portfolio):
    cfg, _, state = portfolio
    cfg['dca_amount'] = 0
    result = graph.start_node(state, {'configurable': {'config_id': 'portfolio', 'agent_config': cfg}})
    assert '预算允许新增买入上限 0 USDT' in result.messages[0].content
    assert 'BTC-order' in result.messages[0].content


def test_missing_secondary_asset_stops_portfolio_decision(portfolio):
    cfg, market, state = portfolio
    market.get_market_analysis.side_effect = [{'analysis': {tf: {'price': 1} for tf in ['4h', '1d', '1w']}},
                                            {'analysis': {}, 'error': 'unavailable'}]
    with pytest.raises(graph.LLMInvocationError, match='ETH/USDT'):
        graph.start_node(state, {'configurable': {'config_id': 'portfolio', 'agent_config': cfg}})


def test_tool_rounds_forward_stable_shared_cycle(portfolio, monkeypatch):
    cfg, _, state = portfolio
    from backend.utils.spot_config_guard import spot_execution_fingerprint
    state.spot_config_fingerprint = spot_execution_fingerprint(cfg)
    dispatch = Mock(return_value='success')
    monkeypatch.setattr(graph, 'run_trade_tool', dispatch)
    config = {'configurable': {'config_id': 'portfolio', 'agent_config': cfg, 'spot_cycle_id': 'same-run'}}
    for order_id in ('one', 'two'):
        state.messages = [AIMessage(content='', tool_calls=[{
            'id': order_id, 'name': 'open_position_spot_dca', 'args': {'orders': []}}])]
        graph.tools_node(state, config)
    assert [call.kwargs['cycle_id'] for call in dispatch.call_args_list] == ['same-run', 'same-run']
    assert all(call.kwargs['expected_spot_fingerprint'] == spot_execution_fingerprint(cfg)
               for call in dispatch.call_args_list)


def test_chat_retry_preserves_budget_cycle(portfolio, monkeypatch):
    cfg, _, state = portfolio
    from backend.utils.spot_config_guard import spot_execution_fingerprint
    state.spot_config_fingerprint = spot_execution_fingerprint(cfg)
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: cfg)
    monkeypatch.setattr(chat_graph, 'scheduler_start_node', lambda *a, **kw: state)
    first = chat_graph.start_node({'q': 'analyze'}, {'configurable': {'config_id': 'portfolio'}})
    retry = chat_graph.start_node({**first, 'retry_last': True}, {'configurable': {'config_id': 'portfolio'}})
    assert retry['spot_cycle_id'] == first['spot_cycle_id']
    assert len(first['spot_config_fingerprint']) == 64
    next_turn = chat_graph.start_node({**first, 'q': 'next'}, {'configurable': {'config_id': 'portfolio'}})
    assert next_turn['spot_cycle_id'] != first['spot_cycle_id']


def test_chat_approval_keeps_original_config_fingerprint(portfolio, monkeypatch):
    from backend.utils.spot_config_guard import spot_execution_fingerprint
    cfg, _, _ = portfolio
    fingerprint = spot_execution_fingerprint(cfg)
    updated = {**cfg, 'dca_amount': 500}
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: updated)
    monkeypatch.setattr(chat_graph, 'interrupt', lambda _: True)
    dispatch = Mock(return_value='success')
    monkeypatch.setattr(chat_graph, 'run_trade_tool', dispatch)
    state = {'messages': [AIMessage(content='', tool_calls=[{
        'id': 'approved', 'name': 'open_position_spot_dca', 'args': {'orders': []}}])],
        'spot_config_fingerprint': fingerprint}
    chat_graph.tools_node(state, {'configurable': {'config_id': 'portfolio'}})
    assert dispatch.call_args.kwargs['expected_spot_fingerprint'] == fingerprint
    assert dispatch.call_args.kwargs['expected_spot_fingerprint'] != spot_execution_fingerprint(updated)
    state.pop('spot_config_fingerprint')
    chat_graph.tools_node(state, {'configurable': {'config_id': 'portfolio'}})
    assert dispatch.call_args.kwargs['expected_spot_fingerprint'] == ''


def test_public_workspace_keeps_selected_chart_symbol(portfolio, monkeypatch):
    from backend.app.services import public_service as public
    cfg, _, _ = portfolio
    monkeypatch.setattr(public.global_config, 'get_config_by_id', lambda _: cfg)
    monkeypatch.setattr(public, 'get_dashboard_data', lambda *a, **kw: [{'config_id': 'portfolio'}])
    for name in ('get_position_stats_payload', 'get_recent_order_activity_payload',
                 'get_daily_summaries_payload', 'get_short_memories_payload', 'get_latest_news_snapshot'):
        monkeypatch.setattr(public, name, Mock(return_value={}))
    chart = Mock(return_value={'symbol': 'ETH/USDT'})
    monkeypatch.setattr(public, 'get_kline_payload', chart)
    result = public.build_public_workspace_payload('portfolio', '4h', symbol='ETH/USDT')
    chart.assert_called_once_with('portfolio', '4h', symbol='ETH/USDT')
    assert result['kline']['symbol'] == 'ETH/USDT'
    chart.reset_mock()
    with pytest.raises(ValueError, match='not configured'):
        public.build_public_workspace_payload('portfolio', '4h', symbol='DOGE/USDT')
    chart.assert_not_called()


def test_daily_review_retains_fills_with_same_id_across_symbols(portfolio, monkeypatch):
    import json
    from datetime import datetime
    from backend import database
    from backend.utils import trade_review, market_data
    cfg, _, _ = portfolio
    timestamp = int(database.TZ_CN.localize(datetime(2026, 10, 1, 12)).timestamp() * 1000)
    exchange = Mock()
    exchange.fetch_my_trades.side_effect = lambda symbol, **kw: [
        {'id': 'same-id', 'symbol': symbol, 'timestamp': timestamp, 'side': 'buy', 'cost': 10}]
    monkeypatch.setattr(market_data, 'MarketTool', Mock(return_value=Mock(exchange=exchange)))
    result = trade_review.daily_exchange_evidence(cfg, '2026-10-01')
    evidence = json.loads(result.split('\n', 1)[1])
    assert evidence['returned_fill_count'] == 2
    assert {fill['symbol'] for fill in evidence['fills']} == set(cfg['symbols'])
