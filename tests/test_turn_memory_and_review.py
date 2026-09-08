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
    assert '最多600字' not in source
    assert '分为【当前假设】' not in source
    assert source.startswith('更新时间：')
    assert 'open_position_real: Order rejected' in source
    assert database.get_short_memories('cfg', 1)[0]['market_summary'] == 'new plan; entry rejected'


def test_memory_failure_still_advances_latest_strategy(local_db):
    with patch.object(agent_graph, 'summarize_content', return_value=''):
        assert agent_graph.update_turn_memory('cfg', {}, 'old trade invalidated', [])
    latest = database.get_short_memories('cfg', 1)[0]['market_summary']
    assert 'old trade invalidated' in latest and '待核实' in latest


def test_short_memory_includes_local_7d_closed_positions_and_stats(local_db):
    now = database.datetime.now(database.TZ_CN)
    opened = now - database.timedelta(hours=2)
    with database.get_db_conn() as conn:
        conn.execute(
            """INSERT INTO execution_position_history(
                   position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,
                   entry_price,close_price,amount,take_profit,stop_loss,realized_pnl,
                   fees_json,exit_reason,updated_at_ms,payload
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                'test_pos_1', 'scope', 'cfg', 'ETH/USDT:USDT', 'SHORT',
                int(opened.timestamp() * 1000), int(now.timestamp() * 1000),
                2500.0, 2450.0, 0.1, 2440.0, 2520.0, 5.0,
                '{}', 'take_profit', int(now.timestamp() * 1000), '{}',
            ),
        )
        conn.commit()

    with patch.object(agent_graph, 'summarize_content', return_value='updated memory') as summarize:
        assert agent_graph.update_turn_memory('cfg', {'mode': 'REAL', 'symbol': 'ETH/USDT'}, 'wait for pullbacks', [])

    source = summarize.call_args.args[0]
    assert '【过去7天平仓记录（本地成交账本）】' in source
    assert '开仓:' in source and '平仓:' in source
    assert '2500.00' in source and '2450.00' in source
    assert 'TP: 2440.00' in source and 'SL: 2520.00' in source
    assert '盈利: +5.00 USDT' in source
    assert '【7天战绩统计】' in source
    assert '完整平仓周期: 1 笔' in source
    assert '胜率: 100.0%' in source
    assert '已确认累计盈亏（手续费前）: +5.00 USDT' in source


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


def test_parse_execution_facts_to_text_formatting():
    data = {
        "position_cycles": {
            "completed": [
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "SHORT",
                    "opened_at_ms": 1757221561000,
                    "closed_at_ms": 1757223361000,
                    "entered_base": 0.12,
                    "exited_base": 0.12,
                    "entry_vwap": 2504.5,
                    "exit_vwap": 2505.5,
                    "realized_pnl_before_fees": -0.12,
                    "exit_reasons": ["stop_loss"]
                }
            ],
            "open_cycles": [
                {
                    "symbol": "ETH/USDT:USDT",
                    "side": "LONG",
                    "opened_at_ms": 1757276417000,
                    "remaining_base": 0.161,
                    "entry_cost": 399.04655
                }
            ]
        },
        "recent_fills": [
            {
                "timestamp": 1757276417000,
                "symbol": "ETH/USDT:USDT",
                "position_side": "LONG",
                "role": "entry",
                "side": "buy",
                "price": 2478.55,
                "amount": 0.161,
                "realized_pnl": None
            }
        ],
        "known_realized_pnl_before_fees": -0.12,
        "fees_by_currency": {"USDT": 0.0601}
    }
    raw_text = json.dumps(data)
    parsed = agent_graph.parse_execution_facts_to_text(raw_text)

    # 验证各区块存在且由双换行分开，避免粘连
    assert "【已平仓交易周期】" in parsed
    assert "止损出场" in parsed
    assert "手续费前盈亏 -0.12 USDT" in parsed
    assert "【未闭合持仓周期 (实际持仓以实时账户快照为准)】" in parsed
    assert "剩余数量: 0.1610" in parsed
    assert "【近期成交明细】" in parsed
    assert "2478.55" in parsed
    assert "【账本统计】" in parsed
    assert "已确认实现盈亏: -0.12 USDT" in parsed
    assert "手续费: 0.0601 USDT" in parsed

    # 验证章节之间有空行分隔
    sections = parsed.split("\n\n")
    assert len(sections) == 4
    for s in sections:
        assert s.startswith("【")
