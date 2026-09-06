import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
import pytest

import backend.database as database
from backend.database_schema import initialize_schema
from backend.agent import agent_graph
from backend.utils.trade_review import daily_execution_evidence


@pytest.fixture
def local_db(tmp_path):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'test.db')):
        with database.get_db_conn() as conn:
            initialize_schema(conn)
        yield


def test_every_strategy_updates_bounded_memory_with_execution_evidence(local_db):
    database.save_short_memory('2026-01-01', '2026-01-01', 'ETH/USDT', 'cfg', 'old short plan', '', 1)
    messages = [AIMessage(content='new long plan', tool_calls=[{'id': 'c', 'name': 'open_position_real', 'args': {}}]),
                ToolMessage(content='Order rejected: no funds', tool_call_id='c')]
    with patch.object(agent_graph, 'summarize_content', return_value='new plan; entry rejected') as summarize:
        assert agent_graph.update_turn_memory('cfg', {'symbol': 'ETH/USDT'}, 'reverse to long', messages)
    source = summarize.call_args.args[0]
    assert 'old short plan' in source and 'reverse to long' in source
    assert 'open_position_real: Order rejected' in source
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'new plan; entry rejected'


def test_memory_failure_still_advances_latest_strategy(local_db):
    with patch.object(agent_graph, 'summarize_content', return_value=''):
        assert agent_graph.update_turn_memory('cfg', {}, 'old trade invalidated', [])
    latest = database.get_short_memories('cfg', 1)[0]['market_summary']
    assert 'old trade invalidated' in latest and '待核实' in latest


def test_daily_review_uses_only_requested_day_and_config_and_actual_fills(local_db):
    with database.get_db_conn() as conn:
        for cid, day, tid in [('cfg', '2026-09-01', 'a'), ('other', '2026-09-01', 'b'), ('cfg', '2026-09-02', 'c')]:
            conn.execute('INSERT INTO trade_history(trade_id,config_id,timestamp,symbol,side,price,amount,fee,fee_currency,realized_pnl) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (tid, cid, day + ' 12:00:00', 'ETH/USDT', 'sell', 100, 1, .2, 'USDT', 2))
        conn.commit()
    text = daily_execution_evidence('cfg', '2026-09-01')
    payload = json.loads(text[text.index('{'):])
    assert [x['trade_id'] for x in payload['trade_history']] == ['a']
    assert payload['real_fill_totals']['fill_count'] == 1
    assert payload['fees_by_currency'] == [{'fee_currency': 'USDT', 'fee': .2}]
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT'}
    with patch.object(agent_graph.global_config, 'get_all_symbol_configs', return_value=[cfg]), patch.object(agent_graph, 'summarize_content', return_value='actual fills review') as summarize:
        assert agent_graph.generate_manual_daily_summary('cfg', '2026-09-01')
    assert 'trade_history' in summarize.call_args.args[0]
    assert database.get_daily_summaries('cfg')[0]['source_count'] == 0  # no analysis rounds, one real fill
