"""Regression coverage for complete memory text across review and prompt boundaries."""
from backend.agent import memory_service, decision_context

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from backend import database
from backend.agent import agent_graph
from backend.agent.agent_models import AgentState
from backend.database_schema import initialize_schema


CONFIG = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'STRATEGY', 'enabled': True}
NOW = agent_graph.TZ_CN.localize(datetime(2026, 10, 1, 10))


def full_text(label: str, repeats: int = 500) -> str:
    return f'{label}开始\n' + ('关键条件、成交证据和风险限制。' * repeats) + f'\n{label}完整结尾'


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'memory-integrity.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(agent_graph.global_config, 'get_all_symbol_configs', lambda: [CONFIG])
    monkeypatch.setattr(agent_graph, 'invoke_with_retry', lambda operation, **_: operation())


@pytest.mark.parametrize('failure', ['exception', 'empty', 'length', 'max_tokens'])
def test_strategy_summary_failure_preserves_entire_original_text(local_db, monkeypatch, failure):
    source = full_text('策略原文')
    llm = Mock()
    if failure == 'exception':
        llm.invoke.side_effect = RuntimeError('Offline model failure')
    else:
        llm.invoke.return_value = AIMessage(
            content='' if failure == 'empty' else 'partial summary',
            response_metadata={} if failure == 'empty' else {'finish_reason': failure},
        )
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: llm)

    assert agent_graph.summarize_content(source, CONFIG) == agent_graph.STRATEGY_SUMMARY_FAILURE_PREFIX + source


@pytest.mark.parametrize('summary_type', ['strategy'])
def test_completed_long_summary_and_source_are_preserved(local_db, monkeypatch, summary_type):
    source = full_text('输入')
    result = full_text('完整复盘')
    llm = Mock(invoke=Mock(return_value=AIMessage(content=result, response_metadata={'finish_reason': 'stop'})))
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: llm)

    assert agent_graph.summarize_content(source, CONFIG, summary_type) == result
    prompt = llm.invoke.call_args.args[0][0].content
    assert source in prompt
    assert '150字以内' not in prompt and '600字以内' not in prompt


@pytest.mark.parametrize('writer', ['run', 'rolling'])
def test_every_memory_writer_preserves_long_input_and_output(local_db, monkeypatch, writer):
    previous = full_text('旧记忆')
    strategy = full_text('决策')
    execution = full_text('执行结果')
    evidence = full_text('历史证据')
    reviewed = full_text('新记忆')
    database.save_short_memory('2026-10-01 00:00:00', '2026-10-01 04:00:00',
                               CONFIG['symbol'], 'cfg', previous, '', 1)
    with database.get_db_conn() as conn:
        conn.execute(
            'INSERT INTO summaries(timestamp,config_id,strategy_logic) VALUES(?,?,?)',
            ('2026-10-01 09:00:00', 'cfg', strategy),
        )
        conn.commit()
    organizer = Mock(return_value=reviewed)
    monkeypatch.setattr(memory_service, 'organize_memory', organizer)
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *_: evidence)

    if writer == 'run':
        from tests.test_turn_memory_and_review import run_persisted_memory
        messages = [ToolMessage(content=execution, tool_call_id='result', name='check_order')]
        changed = run_persisted_memory('cfg', CONFIG, strategy, messages)
    else:
        changed = memory_service.generate_rolling_short_memory_for_config('cfg', CONFIG, now_cn=NOW)

    assert changed
    source = organizer.call_args.args[0]
    assert previous in source and strategy in source and evidence in source
    if writer == 'run':
        assert __import__('json').dumps(execution, ensure_ascii=False) in source
    saved = database.get_short_memories('cfg', 1)[0]
    assert saved['market_summary'] == reviewed
    assert saved['position_summary'] == evidence
    assert decision_context.format_short_memory_for_llm('cfg') == reviewed


def test_rolling_review_reads_every_record_within_selected_window(local_db, monkeypatch):
    records = [
        ((NOW - timedelta(hours=11, minutes=50) + timedelta(minutes=index * 20)).strftime('%Y-%m-%d %H:%M:%S'),
         'cfg', full_text(f'记录{index:02d}', repeats=50))
        for index in range(30)
    ]
    records.append((NOW.strftime('%Y-%m-%d %H:%M:%S'), 'cfg', 'current-second-evidence'))
    with database.get_db_conn() as conn:
        conn.executemany('INSERT INTO summaries(timestamp,config_id,strategy_logic) VALUES(?,?,?)', records)
        conn.executemany(
            'INSERT INTO summaries(timestamp,config_id,strategy_logic) VALUES(?,?,?)',
            [('2026-09-30 21:59:59', 'cfg', 'too-old'),
             ('2026-10-01 10:00:01', 'cfg', 'future-evidence'),
             ('2026-10-01 09:00:00', 'another-config', 'another-config-evidence')],
        )
        conn.commit()
    organizer = Mock(return_value='Complete window review')
    monkeypatch.setattr(memory_service, 'organize_memory', organizer)
    monkeypatch.setattr(memory_service, 'format_recent_position_history_for_memory', lambda *_: '')

    assert memory_service.generate_rolling_short_memory_for_config('cfg', CONFIG, now_cn=NOW, hours=12)
    source = organizer.call_args.args[0]
    assert all(record[2] in source for record in records)
    assert 'too-old' not in source and 'future-evidence' not in source and 'another-config-evidence' not in source
    assert database.get_short_memories('cfg', 1)[0]['source_count'] == len(records)


def test_complete_memory_and_recent_decisions_reach_trading_prompt(local_db, monkeypatch):
    memory = full_text('长期正文')
    decisions = [full_text(f'决策{index}', repeats=75) for index in range(3)]
    database.save_short_memory('2026-10-01 00:00:00', '2026-10-01 04:00:00',
                               CONFIG['symbol'], 'cfg', memory, '', 1)
    with database.get_db_conn() as conn:
        conn.executemany(
            'INSERT INTO summaries(timestamp,config_id,strategy_logic) VALUES(?,?,?)',
            [(f'2026-10-01 0{index}:00:00', 'cfg', text) for index, text in enumerate(decisions)],
        )
        conn.commit()
    market = Mock()
    market.get_market_analysis.side_effect = RuntimeError('Offline market')
    monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
    monkeypatch.setattr(agent_graph, 'resolve_prompt_template', lambda *_: 'Custom prompt {symbol}')
    monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda *_: 2)
    state = AgentState(symbol=CONFIG['symbol'], messages=[], market_context={}, account_context={},)

    result = agent_graph.start_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': CONFIG}})
    prompt = result.messages[0].content
    assert memory in prompt
    assert all(decision in prompt for decision in decisions)
